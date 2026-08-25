"""Persists the Dashboard-visible slice of Gateway's live runtime state
(usage %, reset times, which Profile is current) across daemon restarts.
A display cache only, never a source of truth for Rotation decisions.

Deliberately NOT persisted: AUTH_INVALID/EXHAUSTED/COOLDOWN/DRAINING states
or cooldown_until — a restart should give every enabled Profile a fresh try.
Only the last-observed usage numbers are restored, so the Dashboard doesn't
show a wall of "not yet observed" after every restart. A number whose
resets_at has already passed at load time is dropped rather than restored:
a stale percentage past its own reset is actively wrong, and the next
request produces a fresh one anyway.
"""

from __future__ import annotations

import json
import threading
from typing import Optional

from .config import APP_DIR, ensure_app_dir

RUNTIME_STATE_FILE = APP_DIR / "runtime_state.json"
_lock = threading.Lock()


def load() -> dict:
    """{"current_profile_id": str|None, "profiles": {profile_id: {...}}}.

    Never raises: a missing or corrupt file means nothing to restore,
    exactly like a first run."""
    empty = {"current_profile_id": None, "profiles": {}}
    if not RUNTIME_STATE_FILE.exists():
        return empty
    try:
        data = json.loads(RUNTIME_STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(data, dict):
        return empty
    profiles = data.get("profiles")
    return {
        "current_profile_id": data.get("current_profile_id"),
        "profiles": profiles if isinstance(profiles, dict) else {},
    }


def save(current_profile_id: Optional[str], profiles: dict) -> None:
    """Best-effort: called after every observation, so it must never raise
    into the request path. Callers wrap this."""
    ensure_app_dir()
    payload = {"current_profile_id": current_profile_id, "profiles": profiles}
    tmp = RUNTIME_STATE_FILE.with_suffix(".json.tmp")
    with _lock:
        tmp.write_text(json.dumps(payload))
        tmp.replace(RUNTIME_STATE_FILE)
