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

import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from . import db, pricing
from .config import APP_DIR

# Kept for db.import_legacy_logs(), which is the only thing that still reads
# this file: rows live in the SQLite store now.
USAGE_HISTORY_FILE = APP_DIR / "usage_history.jsonl"
# No longer trims anything — the store keeps every event, which is the whole
# point of moving off the log. Retained because callers use it as a read cap.
MAX_EVENTS = 20_000

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
    # What the CLIENT asked for, when that differs from what actually served
    # the request. A codex-kind Profile records the OpenAI model it ran
    # (gpt-6-astra), which on its own cannot be told apart from a request that
    # asked for Opus — the mapping is what differs. None means "same as
    # `model`", which is the ordinary case. Defaulted so logs written before
    # this field, and callers that don't know it, keep working.
    requested_model: Optional[str] = None
    # What ECO saved on THIS request. bytes_saved is ground truth and never
    # needs recomputing; tokens_saved is derived (see calibration below), so
    # it can be recomputed from bytes if the method improves. eco_mode records
    # which tier produced the saving, because a number without its tier cannot
    # be compared across a settings change.
    eco_bytes_saved: Optional[int] = None
    eco_tokens_saved: Optional[int] = None
    eco_mode: Optional[str] = None
    # The primitive speech level the reply was written under; None = off.
    speech_mode: Optional[str] = None
    # Codex only: reasoning tokens (inside output_tokens) and the 5h window
    # percentage the backend reported as this request started.
    reasoning_tokens: Optional[int] = None
    quota_5h_percent: Optional[float] = None
    # How many stored requests this row stands for. Always 1 for a real
    # event; more only for a row the store pre-aggregated (see
    # aggregated_since), where several requests that share a minute and every
    # field a chart groups by are summed into one. Every helper below that
    # counts requests counts THIS, so a chart drawn from aggregated rows
    # reports the same numbers as one drawn from the events themselves.
    merged_requests: int = 1


# A request's own bytes-per-token ratio, clamped to a sane band. Outside it the
# calibration is not believable (a malformed body, a tiny request dominated by
# fixed overhead) and only exact bytes are stored.
MIN_BYTES_PER_TOKEN = 1.0
MAX_BYTES_PER_TOKEN = 20.0


def calibrated_tokens_saved(saved_bytes: int, sent_bytes: int, billed_tokens: int) -> Optional[int]:
    """Estimate tokens saved from bytes, calibrated against what was billed.

    We cannot know the counterfactual without sending the request twice, and
    spending tokens to measure token savings is self-defeating. So the ratio
    comes from THIS request's real billed total, which self-corrects per model
    and per content type — unlike a fixed "4 chars per token", which is what
    made the reference project ship a phantom-savings detector.

    Returns None when the ratio cannot be trusted; the caller then stores
    bytes only rather than a number that reads as measured but is not.
    """
    if saved_bytes <= 0 or sent_bytes <= 0 or billed_tokens <= 0:
        return None
    bytes_per_token = sent_bytes / billed_tokens
    if not (MIN_BYTES_PER_TOKEN <= bytes_per_token <= MAX_BYTES_PER_TOKEN):
        return None
    return int(saved_bytes / bytes_per_token)


def record(profile_id: str, project_id: Optional[str], model: Optional[str], usage: dict,
           requested_model: Optional[str] = None,
           eco_bytes_saved: Optional[int] = None,
           eco_tokens_saved: Optional[int] = None,
           eco_mode: Optional[str] = None,
           speech_mode: Optional[str] = None,
           quota_5h_percent: Optional[float] = None) -> UsageEvent:
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
        # Recorded only when it actually differs, so the common case stays null
        # and "was this request translated?" is answerable from the log alone.
        requested_model=requested_model if requested_model and requested_model != model else None,
        eco_bytes_saved=eco_bytes_saved or None,
        eco_tokens_saved=eco_tokens_saved or None,
        eco_mode=eco_mode if eco_bytes_saved else None,
        speech_mode=speech_mode or None,
        reasoning_tokens=int(usage["reasoning_tokens"]) if usage.get("reasoning_tokens") is not None else None,
        quota_5h_percent=quota_5h_percent,
    )
    # Best-effort, exactly as the JSONL write was: a lost usage row must never
    # surface as an exception in the request path (db.execute swallows).
    db.execute(
        """INSERT INTO usage_event (ts, profile_id, project_id, model, input_tokens, output_tokens,
                                    cache_creation_input_tokens, cache_read_input_tokens, cost_usd,
                                    requested_model, eco_bytes_saved, eco_tokens_saved, eco_mode,
                                    speech_mode, reasoning_tokens, quota_5h_percent)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (event.timestamp, event.profile_id, event.project_id, event.model,
         event.input_tokens, event.output_tokens, event.cache_creation_input_tokens,
         event.cache_read_input_tokens, event.cost_usd, event.requested_model,
         event.eco_bytes_saved, event.eco_tokens_saved, event.eco_mode, event.speech_mode,
         event.reasoning_tokens, event.quota_5h_percent))
    return event


def _event_from_row(r) -> UsageEvent:
    return UsageEvent(timestamp=r["ts"], profile_id=r["profile_id"], project_id=r["project_id"],
                      model=r["model"], input_tokens=r["input_tokens"], output_tokens=r["output_tokens"],
                      cache_creation_input_tokens=r["cache_creation_input_tokens"],
                      cache_read_input_tokens=r["cache_read_input_tokens"],
                      eco_bytes_saved=r["eco_bytes_saved"], eco_tokens_saved=r["eco_tokens_saved"],
                      eco_mode=r["eco_mode"], speech_mode=r["speech_mode"],
                      reasoning_tokens=r["reasoning_tokens"], quota_5h_percent=r["quota_5h_percent"],
                      cost_usd=r["cost_usd"], requested_model=r["requested_model"])


def list_events() -> list[UsageEvent]:
    """Oldest first, the order the append-only log produced and every
    aggregation helper below assumes.

    Every row, as objects. That is what a Statistics page over "all time"
    needs and what the pure helpers below take — but it is the wrong thing to
    call on a path that runs once a second (see the store-backed aggregates
    further down, which do the same work in SQL and allocate nothing)."""
    return [_event_from_row(r) for r in db.query("SELECT * FROM usage_event ORDER BY id")]


def reset() -> None:
    db.execute("DELETE FROM usage_event")


# ---- store-backed aggregates (for paths that run once a second) -----------
#
# The Dashboard's overview polls four endpoints every second, and routing
# consults lifetime tokens on every proxied request. Each of those used to
# build a UsageEvent for every row in the table first — 6,000 objects a tick
# on one real install, growing forever, which showed up as constant
# garbage collection and a Dashboard that took seconds to answer. The
# aggregation is SQL's job; the pure helpers above stay for callers that
# genuinely need the events themselves.

def totals_by_profile() -> dict[str, dict]:
    """usage_by_profile() computed in SQL. Same shape, same rules: tokens
    always counted, cost None unless at least one priced event exists."""
    return {
        r["profile_id"]: {
            "tokens": int(r["tokens"] or 0),
            "cost_usd": round(r["cost"], 4) if r["priced"] else None,
        }
        for r in db.query(
            """SELECT profile_id,
                      SUM(input_tokens + output_tokens) AS tokens,
                      COALESCE(SUM(cost_usd), 0.0)      AS cost,
                      COUNT(cost_usd)                   AS priced
                 FROM usage_event GROUP BY profile_id""")
    }


def totals_by_project() -> dict[str, dict]:
    """tokens_by_project() computed in SQL, including its "skip events with no
    resolved project" rule."""
    return {
        r["project_id"]: {
            "tokens": int(r["tokens"] or 0),
            "cost_usd": round(r["cost"], 4) if r["priced"] else None,
        }
        for r in db.query(
            """SELECT project_id,
                      SUM(input_tokens + output_tokens) AS tokens,
                      COALESCE(SUM(cost_usd), 0.0)      AS cost,
                      COUNT(cost_usd)                   AS priced
                 FROM usage_event WHERE project_id IS NOT NULL AND project_id != ''
                GROUP BY project_id""")
    }


def latest_by_profile() -> dict[str, dict]:
    """last_use_by_profile() computed in SQL.

    Ordered by `datetime(ts)`, not by the raw string: SQLite normalizes an
    ISO-8601 timestamp's offset to UTC there, so a row imported from the old
    JSONL log with a local offset cannot sort ahead of a newer UTC one."""
    return {
        r["profile_id"]: {"at": r["ts"], "model": r["model"],
                          "requested_model": r["requested_model"], "project_id": r["project_id"]}
        for r in db.query(
            """SELECT profile_id, ts, model, requested_model, project_id FROM (
                   SELECT profile_id, ts, model, requested_model, project_id,
                          ROW_NUMBER() OVER (PARTITION BY profile_id
                                             ORDER BY datetime(ts) DESC, id DESC) AS rn
                     FROM usage_event
               ) WHERE rn = 1""")
    }


# How far before a range's cutoff the SQL prefilter reaches. The column stores
# whatever offset the writer used, and `ts >= cutoff` compares text, so a row
# written with a non-UTC offset could fall the wrong side of the boundary by
# up to a day. The margin keeps those rows in the candidate set; the exact
# comparison then happens in filter_events_since(), on datetimes.
_RANGE_PREFILTER_MARGIN = timedelta(hours=26)


def event_count() -> int:
    """How many events the store holds, counted in SQL."""
    rows = db.query("SELECT COUNT(*) AS n FROM usage_event")
    return int(rows[0]["n"]) if rows else 0


def history_begins_at() -> Optional[str]:
    """history_begins() without reading the table: the oldest event's
    timestamp, or None when nothing is recorded."""
    rows = db.query("SELECT ts FROM usage_event ORDER BY datetime(ts) ASC, id ASC LIMIT 1")
    return rows[0]["ts"] if rows else None


def events_in_last_days(days: int) -> list[UsageEvent]:
    """Events from the last `days` local calendar days (today counts as one),
    which is what daily_totals_by_profile() buckets. Read with the same
    margin-then-exact rule as events_since()."""
    start = datetime.now().astimezone().date() - timedelta(days=max(0, days - 1))
    cutoff = (datetime.combine(start, datetime.min.time()).astimezone()
              - _RANGE_PREFILTER_MARGIN).astimezone(timezone.utc).isoformat()
    return [_event_from_row(r) for r in
            db.query("SELECT * FROM usage_event WHERE ts >= ? ORDER BY id", (cutoff,))]


# The columns a pre-aggregated row must agree on before two requests can be
# folded together. They are exactly the fields the reporting helpers group by,
# plus the two "is it set at all" flags two of them branch on — fold rows that
# differ in any of these and a breakdown would move numbers into the wrong
# bucket. The minute is what bounds the time error: every bucket edge the
# helpers use (hour, local day, month) falls on a minute boundary.
_AGGREGATE_KEY = """strftime('%Y-%m-%dT%H:%M:00+00:00', ts, 'utc'),
                    profile_id, project_id, model, requested_model,
                    eco_mode, speech_mode,
                    (cost_usd IS NULL), (COALESCE(eco_bytes_saved, 0) = 0)"""


def _aggregated_rows(cutoff_iso: str, exact_cutoff_iso: Optional[str] = None) -> list[UsageEvent]:
    """`cutoff_iso` narrows using the index on `ts` (text compare, hence the
    margin its callers add); `exact_cutoff_iso` then decides membership on
    real timestamps, inside SQL, BEFORE the fold. Filtering after the fold
    would judge a whole minute by its rounded edge."""
    exact = " AND datetime(ts) >= datetime(?)" if exact_cutoff_iso else ""
    params = (cutoff_iso, exact_cutoff_iso) if exact_cutoff_iso else (cutoff_iso,)
    return [
        UsageEvent(
            timestamp=r["bucket_ts"], profile_id=r["profile_id"], project_id=r["project_id"],
            model=r["model"], requested_model=r["requested_model"],
            input_tokens=int(r["input_tokens"] or 0), output_tokens=int(r["output_tokens"] or 0),
            cache_creation_input_tokens=int(r["cache_creation"] or 0),
            cache_read_input_tokens=int(r["cache_read"] or 0),
            # NULL when no request in the group was priced, which is what the
            # "uncosted" figures count — hence (cost_usd IS NULL) in the key.
            cost_usd=r["cost_usd"],
            eco_bytes_saved=r["eco_bytes_saved"], eco_tokens_saved=r["eco_tokens_saved"],
            eco_mode=r["eco_mode"], speech_mode=r["speech_mode"],
            merged_requests=int(r["merged_requests"]),
        )
        for r in db.query(
            f"""SELECT strftime('%Y-%m-%dT%H:%M:00+00:00', ts, 'utc') AS bucket_ts,
                       profile_id, project_id, model, requested_model, eco_mode, speech_mode,
                       SUM(input_tokens)  AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       SUM(cache_creation_input_tokens) AS cache_creation,
                       SUM(cache_read_input_tokens)     AS cache_read,
                       SUM(cost_usd)          AS cost_usd,
                       SUM(eco_bytes_saved)   AS eco_bytes_saved,
                       SUM(eco_tokens_saved)  AS eco_tokens_saved,
                       COUNT(*)               AS merged_requests
                  FROM usage_event WHERE ts >= ?{exact}
                 GROUP BY {_AGGREGATE_KEY}
                 ORDER BY bucket_ts""", params)
    ]


def aggregated_since(range_key: str) -> list[UsageEvent]:
    """The events of `range_key`, folded in SQL into one row per minute per
    breakdown key — what every chart and breakdown on the Statistics and
    overview pages is built from.

    The reporting endpoints run a dozen passes over this list, and the
    overview polls one of them every few seconds. Read as raw events, that
    cost grows with the user's whole history: a busy day writes six figures of
    rows, and a week of them made the summary take seconds. Folding first
    bounds the list by TIME (minutes in the range x distinct keys) instead,
    and every helper reports the same numbers either way — `merged_requests`
    is what carries the request counts through.
    """
    delta = _RANGE_TIMEDELTA.get(range_key)
    if delta is None:
        return _aggregated_rows("")
    exact = datetime.now(timezone.utc) - delta
    return _aggregated_rows((exact - _RANGE_PREFILTER_MARGIN).isoformat(), exact.isoformat())


def aggregated_in_last_days(days: int) -> list[UsageEvent]:
    """aggregated_since() for the day-count windows the bar charts use."""
    start = datetime.now().astimezone().date() - timedelta(days=max(0, days - 1))
    cutoff = (datetime.combine(start, datetime.min.time()).astimezone()
              - _RANGE_PREFILTER_MARGIN).astimezone(timezone.utc).isoformat()
    return _aggregated_rows(cutoff)


def events_since(range_key: str) -> list[UsageEvent]:
    """The events filter_events_since() would keep, read from the store
    instead of filtering the whole table in Python. Exact: SQL narrows, the
    pure filter decides."""
    delta = _RANGE_TIMEDELTA.get(range_key)
    if delta is None:
        return list_events()
    cutoff = (datetime.now(timezone.utc) - delta - _RANGE_PREFILTER_MARGIN).isoformat()
    candidates = [_event_from_row(r) for r in
                  db.query("SELECT * FROM usage_event WHERE ts >= ? ORDER BY id", (cutoff,))]
    return filter_events_since(candidates, range_key)


# ---- pure aggregation helpers (no I/O: take the list, return a shape) ----

def _local(event_timestamp: str) -> datetime:
    return datetime.fromisoformat(event_timestamp).astimezone()


# The Dashboard's "1h/1d/1w/1m/1y" range control on the Usage section.
# "1m"/"1y" are calendar-approximate (30/365 days): this filters by real
# elapsed time, not by calendar month or year boundaries.
RANGE_KEYS = ("1h", "1d", "1w", "1m", "3m", "6m", "1y", "all")
_RANGE_TIMEDELTA = {
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
    "1w": timedelta(days=7),
    "1m": timedelta(days=30),
    "3m": timedelta(days=91),
    "6m": timedelta(days=182),
    "1y": timedelta(days=365),
    # "all" is deliberately absent: filter_events_since returns everything for
    # a key it does not know, which is exactly what "all time" means.
}
# How many daily_totals() buckets the "day"-granularity ranges render as.
RANGE_TO_DAYS = {"1h": 1, "1w": 7}

# The Tokens/day chart's bucket size per range. A bar per day only works up
# to a week; "1m" and "1y" would be 30 and 365 bars, too many for the
# chart's width, so they use coarser buckets. "1d" is hourly rather than one
# calendar-day bucket because daily_totals buckets by LOCAL CALENDAR DATE,
# so days=1 would cover only since midnight and drop events from the real
# rolling 24h window.
RANGE_GRANULARITY = {"1h": "day", "1d": "hour", "1w": "day", "1m": "week",
                     "3m": "week", "6m": "week", "1y": "month", "all": "month"}

# The Statistics line chart can carry far more points than a bar chart, so
# it renders finer buckets than the bar-based /api/usage/summary does.
CHART_GRANULARITY = {**RANGE_GRANULARITY, "1m": "day", "3m": "day", "6m": "week"}

# Day counts for the LINE chart only. Kept apart from RANGE_TO_DAYS because
# that one bounds the bar chart, where 91 bars would be unreadable; a line
# with 91 points is fine.
CHART_TO_DAYS = {**RANGE_TO_DAYS, "1m": 30, "3m": 91}


# How many days each range actually covers, used to size chart windows so the
# plotted period matches the total shown beside it.
RANGE_TO_SPAN_DAYS = {"1h": 1, "1d": 1, "1w": 7, "1m": 30, "3m": 91, "6m": 182, "1y": 365}


def months_to_cover(events: list["UsageEvent"], range_key: str) -> int:
    """Months needed to span `range_key` — or all of history for "all".

    A fixed 12 made the "all time" chart start 12 months ago regardless of how
    far back the data went, quietly cropping the very range it claims to show.
    """
    span = RANGE_TO_SPAN_DAYS.get(range_key)
    if span is not None:
        return max(1, -(-span // 30))
    if not events:
        return 1
    oldest = min(_local(e.timestamp) for e in events)
    now = datetime.now().astimezone()
    return max(1, (now.year - oldest.year) * 12 + (now.month - oldest.month) + 1)


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
    calls = {day: 0 for day in buckets}
    for e in events:
        day = _local(e.timestamp).date()
        if day in buckets:
            buckets[day] += e.input_tokens + e.output_tokens
            calls[day] += e.merged_requests
    ordered_days = sorted(buckets.keys())
    return [{"date": d.isoformat(), "tokens": buckets[d], "requests": calls[d]}
            for d in ordered_days]


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


def series_by_kind(events: list[UsageEvent], buckets: list[dict],
                   kind_of: dict[str, str]) -> list[dict]:
    """The bucket run split into one cost series per provider kind.

    The flat `buckets` list can only draw a single line. Charting Codex
    against Claude needs each bucket's cost attributed to the kind that
    served it, keyed to the SAME buckets so the lines share an x-axis.

    Events are placed in the bucket that CONTAINS them rather than by an
    exact key match: week buckets are keyed by the window's first day and
    month buckets by the 1st, so exact matching silently dropped every event
    that did not land on a boundary — which emptied the 1y and 3m series.
    """
    if not buckets:
        return []
    starts = []
    for b in buckets:
        raw = b.get("date")
        if not raw:
            return []
        try:
            starts.append(datetime.fromisoformat(raw))
        except ValueError:
            return []
    # Naive vs aware must not be mixed when comparing below.
    tz = starts[0].tzinfo
    totals: dict[str, list[float]] = {}
    for e in events:
        stamp = _local(e.timestamp)
        stamp = stamp.replace(tzinfo=tz) if tz is None else stamp.astimezone(tz)
        # Last bucket whose start is <= the event: its containing bucket.
        index = None
        for i, start in enumerate(starts):
            if stamp >= start:
                index = i
            else:
                break
        if index is None:
            continue
        kind = kind_of.get(e.profile_id or "", "other")
        totals.setdefault(kind, [0.0] * len(starts))[index] += (e.cost_usd or 0.0)
    return [{"kind": kind, "points": [round(v, 6) for v in values]}
            for kind, values in sorted(totals.items())]


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


def eco_totals(events: list[UsageEvent]) -> dict:
    """What ECO saved over `events`.

    Three distinct states the UI must be able to tell apart, because "0" and
    "never ran" mean very different things to someone deciding whether to
    turn it on:
      * `requests == 0`  -> ECO has never compacted anything in this range
      * `requests > 0, bytes == 0` -> it ran and found nothing worth doing
      * otherwise the real figures

    `bytes` is ground truth. `tokens` is the sum of per-request calibrated
    estimates. `cost_usd` is an estimate OF an estimate — saved input tokens
    priced at each model's input rate — so it is None unless at least one
    row could be priced, and the UI must mark it approximate.
    """
    saved_bytes = 0
    saved_tokens = 0
    requests = 0
    cost = 0.0
    priced_any = False
    modes: dict[str, int] = {}
    for e in events:
        if not e.eco_bytes_saved:
            continue
        requests += e.merged_requests
        saved_bytes += e.eco_bytes_saved
        if e.eco_mode:
            modes[e.eco_mode] = modes.get(e.eco_mode, 0) + e.merged_requests
        if e.eco_tokens_saved:
            saved_tokens += e.eco_tokens_saved
            price = pricing.find_price(e.model)
            if price is not None:
                # Saved tokens are INPUT tokens (ECO only shortens what is
                # sent), and prices are per MILLION tokens.
                cost += e.eco_tokens_saved * price.input_per_mtok / 1_000_000
                priced_any = True
    return {
        "requests": requests,
        "bytes": saved_bytes,
        "tokens": saved_tokens,
        "cost_usd": round(cost, 4) if priced_any else None,
        # sorted(): determinism, same rule as everywhere else in this codebase.
        "modes": dict(sorted(modes.items())),
    }


def speech_totals(events: list[UsageEvent]) -> dict:
    """Output tokens per reply, with primitive speech and without.

    Real billed output tokens, grouped by the level each reply was written
    under. It is NOT a controlled comparison — the replies answer different
    requests, and helper calls (which never get the instruction) land in
    `without` — so the UI labels it as rough. Levels never used are absent.
    """
    # Kept as (requests, output tokens) rather than a list of per-reply values:
    # the average is all that is reported, and a pre-aggregated row stands for
    # several replies at once (see UsageEvent.merged_requests).
    groups: dict[str, list[int]] = {}
    without = [0, 0]
    for e in events:
        bucket = groups.setdefault(e.speech_mode, [0, 0]) if e.speech_mode else without
        bucket[0] += e.merged_requests
        bucket[1] += e.output_tokens

    def summary(pair: list[int]) -> dict:
        requests, output_tokens = pair
        return {"requests": requests,
                "avg_output_tokens": round(output_tokens / requests) if requests else 0}

    return {
        "levels": {level: summary(pair) for level, pair in sorted(groups.items())},
        "without": summary(without),
    }


def totals(events: list[UsageEvent]) -> dict:
    """The headline figures for a period. `uncosted` is reported rather than
    hidden: a model with no published price contributes tokens but no cost,
    and a total that silently omits it reads as cheaper than it was."""
    tokens_in = sum(e.input_tokens for e in events)
    tokens_out = sum(e.output_tokens for e in events)
    cache_write = sum(e.cache_creation_input_tokens for e in events)
    cache_read = sum(e.cache_read_input_tokens for e in events)
    billable = tokens_in + cache_write + cache_read
    return {
        "cost_usd": round(sum(e.cost_usd or 0.0 for e in events), 4),
        "uncosted_events": sum(e.merged_requests for e in events if e.cost_usd is None),
        "requests": sum(e.merged_requests for e in events),
        "tokens": tokens_in + tokens_out + cache_write + cache_read,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cache_write": cache_write,
        "cache_read": cache_read,
        # Share of billable input that came from cache rather than being re-sent.
        "cache_hit_percent": round(cache_read / billable * 100, 1) if billable else None,
    }


def split_by(events: list[UsageEvent], dimension: str, top: int = 6) -> list[dict]:
    """Ranked cost/token/request breakdown along one dimension, top-N plus a
    folded "Other" row. One shape for every dimension so the frontend renders
    them all with a single helper."""
    keys = {"model": lambda e: e.model, "project": lambda e: e.project_id,
            "profile": lambda e: e.profile_id, "requested_model": lambda e: e.requested_model or e.model}
    pick = keys.get(dimension)
    if pick is None:
        return []
    rows: dict = {}
    for event in events:
        key = pick(event) or ""
        row = rows.setdefault(key, {"key": key, "cost_usd": 0.0, "tokens": 0, "requests": 0})
        row["cost_usd"] += event.cost_usd or 0.0
        row["tokens"] += event.input_tokens + event.output_tokens
        row["requests"] += event.merged_requests
    ordered = sorted(rows.values(), key=lambda r: (-r["cost_usd"], -r["tokens"], r["key"]))
    head, tail = ordered[:top], ordered[top:]
    if tail:
        head.append({"key": "", "other": True,
                     "cost_usd": sum(r["cost_usd"] for r in tail),
                     "tokens": sum(r["tokens"] for r in tail),
                     "requests": sum(r["requests"] for r in tail)})
    # Rounded first, and the total taken from the rounded figures: `share` is
    # the percentage of the costs actually SHOWN, so the column adds up to what
    # it says it does. Taking it from the unrounded sum also made the figure
    # depend on the order the costs were added, which differs between reading
    # events and reading pre-aggregated rows.
    for row in head:
        row["cost_usd"] = round(row["cost_usd"], 4)
    total = sum(r["cost_usd"] for r in head) or 0.0
    for row in head:
        row["share"] = round(row["cost_usd"] / total * 100, 1) if total else 0.0
    return head


def history_begins(events: list[UsageEvent]) -> Optional[str]:
    """Timestamp of the oldest event, so a period that starts before we have
    any data can say so instead of showing a truncated chart as if it were
    the whole story."""
    return events[0].timestamp if events else None


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
        buckets[_local(e.timestamp).hour] += e.merged_requests
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


def last_use_by_profile(events: list[UsageEvent]) -> dict[str, dict]:
    """{profile_id: {"at", "model", "requested_model", "project_id"}} from each
    Profile's most recent recorded request.

    Backs the widget's "serving now" block: what an account last ran, and for
    which project. It is the last COMPLETED request, not a live stream — a
    request is only recorded once its usage is known — so callers label it as
    the latest, never as "currently running"."""
    latest: dict[str, UsageEvent] = {}
    for e in events:
        # By timestamp, not position: the startup import of the old JSONL logs
        # can append older rows after newer ones.
        current = latest.get(e.profile_id)
        if current is None or _local(e.timestamp) >= _local(current.timestamp):
            latest[e.profile_id] = e
    return {pid: {"at": e.timestamp, "model": e.model, "requested_model": e.requested_model,
                  "project_id": e.project_id}
            for pid, e in latest.items()}


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
