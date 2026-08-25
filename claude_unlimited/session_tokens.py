"""Per-terminal-session credentials for `claude-unlimited code --profile`.

The shared placeholder_token only proves the request came from a process on
this machine, and every invocation uses the same one, so it cannot carry any
extra meaning. Pinning one terminal to one specific Profile, without
affecting other concurrent terminals, needs a distinct credential per pinned
session; that is what this module mints.

get_or_create() reuses a live token for the same profile_id instead of
minting a fresh one per call, so repeated invocations don't grow the file
unboundedly. Tokens expire after SESSION_TOKEN_TTL: long enough that a
long-running terminal session isn't invalidated under itself, short enough
that dead entries don't accumulate indefinitely.
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import APP_DIR, ensure_app_dir

SESSION_TOKENS_FILE = APP_DIR / "session_tokens.json"
SESSION_TOKEN_TTL = timedelta(days=30)
_lock = threading.Lock()


def _load() -> dict:
    if not SESSION_TOKENS_FILE.exists():
        return {}
    try:
        data = json.loads(SESSION_TOKENS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    ensure_app_dir()
    tmp = SESSION_TOKENS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(SESSION_TOKENS_FILE)


def _is_expired(entry: dict, now: datetime) -> bool:
    try:
        created_at = datetime.fromisoformat(entry["created_at"])
    except (KeyError, ValueError):
        return True
    return now - created_at > SESSION_TOKEN_TTL


def get_or_create(profile_id: str) -> str:
    """Returns a token that resolve() will map back to `profile_id`. Reuses
    an existing live one for the same profile_id if there is one, instead
    of minting a new one on every call."""
    now = datetime.now(timezone.utc)
    with _lock:
        data = _load()
        # Prune expired entries and look for a reusable one for this
        # profile_id in the same pass.
        alive = {tok: entry for tok, entry in data.items() if not _is_expired(entry, now)}
        for tok, entry in alive.items():
            if entry.get("profile_id") == profile_id:
                if len(alive) != len(data):
                    _save(alive)
                return tok
        token = secrets.token_urlsafe(32)
        alive[token] = {"profile_id": profile_id, "created_at": now.isoformat()}
        _save(alive)
        return token


def resolve(token: str) -> Optional[str]:
    """The profile_id this token is pinned to, or None if the token is
    unknown or expired. Not a security check on its own — it is an equality
    comparison against a large random token, the same trust model as
    placeholder_token — and the caller decides what "not found" means."""
    now = datetime.now(timezone.utc)
    with _lock:
        data = _load()
    entry = data.get(token)
    if entry is None or _is_expired(entry, now):
        return None
    return entry.get("profile_id")
