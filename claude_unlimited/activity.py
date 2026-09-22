"""An append-only, bounded local activity log — what actually happened,
for the Activity page and for deciding which desktop notifications to fire.

Structural/metadata only, the same boundary as everywhere else here: an
event's `text` is a locally generated description (e.g. "Rotated Work Max →
Personal Max"), never raw request/response content.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from . import db
from .config import APP_DIR

# Kept for db.import_legacy_logs(), the only thing that still reads this file.
ACTIVITY_FILE = APP_DIR / "activity.jsonl"
# No longer trims: the store keeps every event. Retained as the export cap.
MAX_EVENTS = 2000

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
    # Swallowed exactly as the file write was: record() runs inside the live
    # request path, and losing an audit line is the lesser failure (db.execute
    # returns None rather than raising). The category check above still raises
    # — that is a programming error, not an environmental one.
    db.execute("INSERT INTO activity_event (ts, category, text, meta) VALUES (?, ?, ?, ?)",
               (event.timestamp, event.category, event.text, event.meta))
    return event


def list_events(
    limit: int = 200,
    category: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> list[ActivityEvent]:
    """Newest first. `since`/`until` are inclusive ISO 8601 timestamps compared
    as strings — safe because every stored timestamp is ISO 8601 UTC, which
    sorts identically as a string and as a datetime."""
    clauses, params = [], []
    if category:
        clauses.append("category = ?")
        params.append(category)
    if since:
        clauses.append("ts >= ?")
        params.append(since)
    if until:
        clauses.append("ts <= ?")
        params.append(until)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))
    return [ActivityEvent(timestamp=r["ts"], category=r["category"], text=r["text"], meta=r["meta"])
            for r in db.query(f"SELECT * FROM activity_event{where} ORDER BY id DESC LIMIT ?", params)]
