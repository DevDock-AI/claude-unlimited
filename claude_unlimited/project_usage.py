"""Per-project request counters — the data backing the Dashboard's
"Usage by project (Experimental)" section (see project_attribution.py for
how a request is attributed to a project).

Deliberately counts REQUESTS, not tokens or cost: the daemon has no
per-request token/cost tracking, and a request count is data it already has
for free rather than a figure this module would have to invent.
"""

from __future__ import annotations

from . import db
from .config import APP_DIR

# Kept for db.import_legacy_logs(), the only thing that still reads this file.
USAGE_FILE = APP_DIR / "project_usage.json"


def record_request(project_id: str) -> None:
    """One UPSERT rather than a read-modify-write: SQLite serializes it, so
    concurrent proxy threads can no longer lose increments."""
    db.execute("INSERT INTO project_request (project_id, count) VALUES (?, 1)"
               " ON CONFLICT(project_id) DO UPDATE SET count = count + 1", (project_id,))


def get_counts() -> dict:
    return {r["project_id"]: r["count"] for r in db.query("SELECT project_id, count FROM project_request")}


def reset() -> None:
    db.execute("DELETE FROM project_request")
