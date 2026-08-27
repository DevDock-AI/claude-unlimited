"""An append-only, bounded local activity log — what actually happened,
for the Activity page and for deciding which desktop notifications to fire.

Structural/metadata only, the same boundary as everywhere else here: an
event's `text` is a locally generated description (e.g. "Rotated Work Max →
Personal Max"), never raw request/response content.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import APP_DIR, ensure_app_dir

ACTIVITY_FILE = APP_DIR / "activity.jsonl"
MAX_EVENTS = 2000  # bounded — no unbounded local log growth

_lock = threading.Lock()

CATEGORIES = ("rotation", "session", "config", "error")


@dataclass(frozen=True)
class ActivityEvent:
    timestamp: str  # ISO 8601 UTC
    category: str  # one of CATEGORIES
    text: str
    meta: Optional[str] = None


def record(category: str, text: str, meta: Optional[str] = None) -> ActivityEvent:
    if category not in CATEGORIES:
        raise ValueError(f"Unknown activity category {category!r}; must be one of {CATEGORIES}")

    event = ActivityEvent(timestamp=datetime.now(timezone.utc).isoformat(), category=category, text=text, meta=meta)
    # An unwritable log must never break the request that was being logged.
    # record() is called from inside the live request path — a rotation, a
    # rejected credential — after the upstream response has already come back,
    # so a full disk or a removed app directory would have turned a request
    # that genuinely succeeded into a dropped connection, and leaked the
    # profile's in-flight slot on the way out. Losing an audit line is the
    # lesser failure, and the same trade-off notifications and runtime-state
    # persistence already make. The category check above still raises: that is
    # a programming error, not an environmental one.
    try:
        ensure_app_dir()
        with _lock:
            with ACTIVITY_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(event)) + "\n")
            _trim_if_needed()
    except OSError:
        pass
    return event


def _trim_if_needed() -> None:
    """Caller must hold _lock. Keeps the file bounded without a database;
    cheap enough at MAX_EVENTS scale to check on every write."""
    if not ACTIVITY_FILE.exists():
        return
    lines = ACTIVITY_FILE.read_text(encoding="utf-8").splitlines()
    if len(lines) > MAX_EVENTS:
        trimmed = lines[-MAX_EVENTS:]
        tmp = ACTIVITY_FILE.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(trimmed) + "\n", encoding="utf-8")
        tmp.replace(ACTIVITY_FILE)


def list_events(
    limit: int = 200,
    category: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> list[ActivityEvent]:
    """`since`/`until` are inclusive ISO 8601 timestamps, compared as
    strings. Safe because every stored timestamp is ISO 8601 UTC, which
    sorts identically as a string and as a datetime."""
    if not ACTIVITY_FILE.exists():
        return []
    events = []
    with _lock:
        lines = ACTIVITY_FILE.read_text(encoding="utf-8").splitlines()
    for line in reversed(lines):  # newest first
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue  # a corrupt line never breaks the whole log
        if not isinstance(data, dict):
            continue
        if category and data.get("category") != category:
            continue
        ts = data.get("timestamp", "")
        if since and ts < since:
            continue
        if until and ts > until:
            continue
        try:
            events.append(ActivityEvent(**data))
        except TypeError:
            # Valid JSON, wrong shape: a field this build does not know
            # (written by a newer version, then downgraded) or one missing.
            # "A corrupt line never breaks the whole log" has to mean this
            # too, or one such line 500s the whole Activity page.
            continue
        if len(events) >= limit:
            break
    return events
