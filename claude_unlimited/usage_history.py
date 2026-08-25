"""Per-request usage history: a bounded local JSONL log, same shape as
activity.py (append-only, trimmed on write, thread-safe). One record per
completed request that yielded a captured model and usage (see
usage_tracking.py). Backs the Dashboard's token/day, model-split,
busiest-hours and cost figures, all aggregated from this log on read; there
is no separate rollup table to keep in sync.

Day and hour-of-day bucketing use the machine's LOCAL time, so "today" and
"busiest hours" mean the user's day rather than a UTC one. The stored
timestamp itself stays unambiguous UTC ISO 8601 and is converted to local
time only when aggregating.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from . import pricing
from .config import APP_DIR, ensure_app_dir

USAGE_HISTORY_FILE = APP_DIR / "usage_history.jsonl"
MAX_EVENTS = 20_000  # keeps the local log from growing without bound

_lock = threading.Lock()


@dataclass(frozen=True)
class UsageEvent:
    timestamp: str  # ISO 8601 UTC
    profile_id: str
    project_id: Optional[str]
    model: Optional[str]
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    cost_usd: Optional[float]  # None when the model isn't in pricing.py's table


def record(profile_id: str, project_id: Optional[str], model: Optional[str], usage: dict) -> UsageEvent:
    event = UsageEvent(
        timestamp=datetime.now(timezone.utc).isoformat(),
        profile_id=profile_id,
        project_id=project_id,
        model=model,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        cache_read_input_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cost_usd=pricing.estimate_cost_usd(model, usage),
    )
    ensure_app_dir()
    with _lock:
        with USAGE_HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(event)) + "\n")
        _trim_if_needed()
    return event


def _trim_if_needed() -> None:
    """Caller must hold _lock."""
    if not USAGE_HISTORY_FILE.exists():
        return
    lines = USAGE_HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    if len(lines) > MAX_EVENTS:
        trimmed = lines[-MAX_EVENTS:]
        tmp = USAGE_HISTORY_FILE.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(trimmed) + "\n", encoding="utf-8")
        tmp.replace(USAGE_HISTORY_FILE)


def list_events() -> list[UsageEvent]:
    if not USAGE_HISTORY_FILE.exists():
        return []
    with _lock:
        lines = USAGE_HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    events = []
    for line in lines:
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue  # a corrupt line never breaks the whole log
        events.append(UsageEvent(**data))
    return events


def reset() -> None:
    with _lock:
        ensure_app_dir()
        USAGE_HISTORY_FILE.write_text("")


# ---- pure aggregation helpers (no I/O: take the list, return a shape) ----

def _local(event_timestamp: str) -> datetime:
    return datetime.fromisoformat(event_timestamp).astimezone()


# The Dashboard's "1h/1d/1w/1m/1y" range control on the Usage section.
# "1m"/"1y" are calendar-approximate (30/365 days): this filters by real
# elapsed time, not by calendar month or year boundaries.
RANGE_KEYS = ("1h", "1d", "1w", "1m", "1y")
_RANGE_TIMEDELTA = {
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
    "1w": timedelta(days=7),
    "1m": timedelta(days=30),
    "1y": timedelta(days=365),
}
# How many daily_totals() buckets the "day"-granularity ranges render as.
RANGE_TO_DAYS = {"1h": 1, "1w": 7}

# The Tokens/day chart's bucket size per range. A bar per day only works up
# to a week; "1m" and "1y" would be 30 and 365 bars, too many for the
# chart's width, so they use coarser buckets. "1d" is hourly rather than one
# calendar-day bucket because daily_totals buckets by LOCAL CALENDAR DATE,
# so days=1 would cover only since midnight and drop events from the real
# rolling 24h window.
RANGE_GRANULARITY = {"1h": "day", "1d": "hour", "1w": "day", "1m": "week", "1y": "month"}


def filter_events_since(events: list[UsageEvent], range_key: str) -> list[UsageEvent]:
    """Keep only events within `range_key` of now, by real elapsed time, not
    calendar-day bucketing (daily_totals covers that). An unrecognized
    range_key returns the events unfiltered rather than raising, so a bad
    query param degrades to "all time" instead of a 500."""
    delta = _RANGE_TIMEDELTA.get(range_key)
    if delta is None:
        return events
    cutoff = datetime.now(timezone.utc) - delta
    return [e for e in events if datetime.fromisoformat(e.timestamp) >= cutoff]


def daily_totals(events: list[UsageEvent], days: int = 7) -> list[dict]:
    """Last `days` local calendar days including today, oldest first."""
    today = datetime.now().astimezone().date()
    buckets = {today - timedelta(days=i): 0 for i in range(days)}
    for e in events:
        day = _local(e.timestamp).date()
        if day in buckets:
            buckets[day] += e.input_tokens + e.output_tokens
    ordered_days = sorted(buckets.keys())
    return [{"date": d.isoformat(), "tokens": buckets[d]} for d in ordered_days]


def daily_totals_by_profile(events: list[UsageEvent], days: int = 7) -> list[dict]:
    """Same rolling local-calendar-day window as daily_totals(), split by
    profile_id per day instead of summed. `profiles` lists only ids that
    posted tokens that day, so the frontend never renders a zero-height
    segment for an unused Profile."""
    today = datetime.now().astimezone().date()
    buckets: dict[date, dict[str, int]] = {today - timedelta(days=i): {} for i in range(days)}
    for e in events:
        day = _local(e.timestamp).date()
        bucket = buckets.get(day)
        if bucket is None:
            continue
        bucket[e.profile_id] = bucket.get(e.profile_id, 0) + e.input_tokens + e.output_tokens
    ordered_days = sorted(buckets.keys())
    return [{"date": d.isoformat(), "profiles": buckets[d]} for d in ordered_days]


def hourly_totals(events: list[UsageEvent], hours: int = 24, bucket_hours: int = 1) -> list[dict]:
    """Last `hours` real hours, grouped into rolling `bucket_hours`-wide
    windows ending with the current hour, oldest first. Not
    calendar-day-since-midnight, which would drop events from the real
    last-24h window that landed before local midnight.

    `date` is each bucket's full local ISO datetime, unlike the other
    *_totals helpers, which use a plain date. The Dashboard's "1d" range
    passes bucket_hours=6, giving four bars over the same 24h window."""
    now = datetime.now().astimezone()
    this_hour = now.replace(minute=0, second=0, microsecond=0)
    bucket_count = max(1, hours // bucket_hours)
    starts = [this_hour - timedelta(hours=bucket_hours * i) for i in range(bucket_count)]
    # The oldest bucket's lower edge is anchored to the real `now`, not to
    # `this_hour`, which is rounded down and so up to bucket_hours short of
    # a full window. Anchoring to the rounded hour would shrink the
    # guaranteed lookback and drop events right at the edge.
    oldest_lower_bound = now - timedelta(hours=bucket_hours * bucket_count)
    buckets = {s: 0 for s in starts}
    for e in events:
        # Compared as the exact timestamp; truncating to the hour first
        # would lose events on the bucket edges.
        t = _local(e.timestamp)
        for i, s in enumerate(starts):
            upper = s + timedelta(hours=bucket_hours)
            lower = oldest_lower_bound if i == len(starts) - 1 else s
            if lower <= t < upper:
                buckets[s] += e.input_tokens + e.output_tokens
                break
    ordered = sorted(buckets.keys())
    return [{"date": d.isoformat(), "tokens": buckets[d]} for d in ordered]


def weekly_totals(events: list[UsageEvent], weeks: int = 5) -> list[dict]:
    """Last `weeks` rolling 7-day windows ending today, oldest first: the
    weekly analogue of daily_totals's rolling window, not Monday-aligned
    calendar weeks, since a ragged first or last week reads badly on a small
    bar chart. `date` is each bucket's first day."""
    today = datetime.now().astimezone().date()
    starts = [today - timedelta(days=7 * i + 6) for i in range(weeks)]
    buckets = {s: 0 for s in starts}
    for e in events:
        day = _local(e.timestamp).date()
        for s in starts:
            if s <= day <= s + timedelta(days=6):
                buckets[s] += e.input_tokens + e.output_tokens
                break
    ordered = sorted(buckets.keys())
    return [{"date": d.isoformat(), "tokens": buckets[d]} for d in ordered]


def _shift_month(d: date, months_back: int) -> date:
    total = d.year * 12 + (d.month - 1) - months_back
    year, month0 = divmod(total, 12)
    return date(year, month0 + 1, 1)


def monthly_totals(events: list[UsageEvent], months: int = 12) -> list[dict]:
    """Last `months` calendar months (1st of month, local time) including
    the current month, oldest first. Calendar-aligned rather than a rolling
    30-day window, since a year view reads as "Jan, Feb, Mar..."."""
    today = datetime.now().astimezone().date()
    starts = [_shift_month(today, i) for i in range(months)]
    buckets = {s: 0 for s in starts}
    for e in events:
        day = _local(e.timestamp).date()
        month_start = date(day.year, day.month, 1)
        if month_start in buckets:
            buckets[month_start] += e.input_tokens + e.output_tokens
    ordered = sorted(buckets.keys())
    return [{"date": d.isoformat(), "tokens": buckets[d]} for d in ordered]


def model_split(events: list[UsageEvent]) -> list[dict]:
    totals: dict[str, int] = {}
    for e in events:
        if not e.model:
            continue
        totals[e.model] = totals.get(e.model, 0) + e.input_tokens + e.output_tokens
    total = sum(totals.values())
    return [
        {"model": m, "tokens": t, "percent": round(t / total * 100, 1) if total else 0}
        for m, t in sorted(totals.items(), key=lambda kv: -kv[1])
    ]


def hourly_histogram(events: list[UsageEvent]) -> list[int]:
    """24 buckets (0-23, local hour-of-day), request counts across all
    retained history (not just the daily_totals window)."""
    buckets = [0] * 24
    for e in events:
        buckets[_local(e.timestamp).hour] += 1
    return buckets


def cost_by_profile(events: list[UsageEvent]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for e in events:
        if e.cost_usd is None:
            continue
        totals[e.profile_id] = totals.get(e.profile_id, 0.0) + e.cost_usd
    return {k: round(v, 4) for k, v in totals.items()}


def usage_by_profile(events: list[UsageEvent]) -> dict[str, dict]:
    """{profile_id: {"tokens": N, "cost_usd": N or None}}.

    Tokens are always counted, unlike cost_by_profile(), which skips an
    event whose model has no published price. Backs the Dashboard's
    api-kind Profile display: an API key has no session-based rate-limit
    window like an OAuth subscription, so tokens plus cost is the only
    meaningful usage figure for one."""
    totals: dict[str, dict] = {}
    for e in events:
        bucket = totals.setdefault(e.profile_id, {"tokens": 0, "cost_usd": 0.0, "has_cost": False})
        bucket["tokens"] += e.input_tokens + e.output_tokens
        if e.cost_usd is not None:
            bucket["cost_usd"] += e.cost_usd
            bucket["has_cost"] = True
    return {
        pid: {"tokens": b["tokens"], "cost_usd": round(b["cost_usd"], 4) if b["has_cost"] else None}
        for pid, b in totals.items()
    }


def tokens_by_project(events: list[UsageEvent]) -> dict[str, dict]:
    """{project_id: {"tokens": N, "cost_usd": N or None}} for events with a
    resolved project_id. cost_usd is None only when every event for that
    project used a model outside pricing.py's table, never when the cost is
    simply zero."""
    totals: dict[str, dict] = {}
    for e in events:
        if not e.project_id:
            continue
        bucket = totals.setdefault(e.project_id, {"tokens": 0, "cost_usd": 0.0, "has_cost": False})
        bucket["tokens"] += e.input_tokens + e.output_tokens
        if e.cost_usd is not None:
            bucket["cost_usd"] += e.cost_usd
            bucket["has_cost"] = True
    return {
        pid: {"tokens": b["tokens"], "cost_usd": round(b["cost_usd"], 4) if b["has_cost"] else None}
        for pid, b in totals.items()
    }
