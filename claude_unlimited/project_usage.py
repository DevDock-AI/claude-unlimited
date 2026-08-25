"""Per-project request counters — the data backing the Dashboard's
"Usage by project (Experimental)" section (see project_attribution.py for
how a request is attributed to a project).

Deliberately counts REQUESTS, not tokens or cost: the daemon has no
per-request token/cost tracking, and a request count is data it already has
for free rather than a figure this module would have to invent.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .config import APP_DIR, ensure_app_dir

USAGE_FILE = APP_DIR / "project_usage.json"

# The daemon serves requests on a thread per connection and this runs in the
# proxy hot path, so the read-modify-write below needs a lock or concurrent
# requests lose increments.
_lock = threading.Lock()


def record_request(project_id: str) -> None:
    ensure_app_dir()
    with _lock:
        data = _load()
        data[project_id] = data.get(project_id, 0) + 1
        _save(data)


def get_counts() -> dict:
    with _lock:
        return _load()


def reset() -> None:
    with _lock:
        _save({})


def _load() -> dict:
    if not USAGE_FILE.exists():
        return {}
    try:
        return json.loads(USAGE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    ensure_app_dir()
    tmp = USAGE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(USAGE_FILE)
