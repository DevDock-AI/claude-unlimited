"""Per-terminal-session credentials for `claude-unlimited code --profile`.

The shared placeholder_token only proves the request came from a process on
this machine, and every invocation uses the same one, so it cannot carry any
extra meaning. Pinning one terminal to one specific Profile, without
affecting other concurrent terminals, needs a distinct credential per pinned
session; that is what this module mints.

get_or_create() reuses a live token for the same profile_id instead of
minting a fresh one per call, so repeated invocations don't grow the file
unboundedly. Tokens expire after SESSION_TOKEN_TTL without use: the clock
restarts whenever a token is reused or presented, so a long-running terminal
session (or one relaunched onto a reused token) is never invalidated under
itself, while dead entries still don't accumulate indefinitely.
"""

from __future__ import annotations

import json
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import APP_DIR, ensure_app_dir

SESSION_TOKENS_FILE = APP_DIR / "session_tokens.json"
SESSION_TOKEN_TTL = timedelta(days=30)
# How stale last_used_at may get before a request rewrites it, so a busy
# session doesn't rewrite the file on every request.
_TOUCH_INTERVAL = timedelta(hours=1)
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


def _last_active(entry: dict) -> Optional[datetime]:
    """When the token was last minted, reused or presented. Entries written
    before last_used_at existed fall back to created_at."""
    try:
        stamps = [datetime.fromisoformat(entry[k]) for k in ("created_at", "last_used_at") if entry.get(k)]
    except (TypeError, ValueError):
        return None
    return max(stamps) if stamps else None


def _is_expired(entry: dict, now: datetime) -> bool:
    last = _last_active(entry)
    return last is None or now - last > SESSION_TOKEN_TTL


def _touch(data: dict, token: str, now: datetime) -> bool:
    """Restart the token's expiry clock; True if the entry changed."""
    last = _last_active(data[token])
    if last is not None and now - last < _TOUCH_INTERVAL:
        return False
    data[token] = {**data[token], "last_used_at": now.isoformat()}
    return True


@dataclass(frozen=True)
class SessionGrant:
    """What a session token entitles this terminal to.

    Exactly one of the two is ever meaningful: `forced_profile_id` pins every
    request to one Profile (`--profile`), while `distribute` spreads a
    session's branches — its main agent and each subagent — across accounts
    (`--distribute`). They contradict each other, so the CLI refuses both and
    a forced pin wins if an old entry ever carried both."""

    forced_profile_id: Optional[str] = None
    distribute: bool = False


def get_or_create(profile_id: str) -> str:
    """Returns a token that resolve() will map back to `profile_id`. Reuses
    an existing live one for the same profile_id if there is one, instead
    of minting a new one on every call."""
    return _get_or_create_entry({"profile_id": profile_id},
                                lambda e: e.get("profile_id") == profile_id)


def get_or_create_distribute() -> str:
    """A token meaning "distribute this session's branches across accounts"
    rather than "pin to one Profile". Reused across launches like the pinned
    kind, so repeated `cu code --distribute` runs don't grow the file."""
    return _get_or_create_entry({"mode": "distribute"},
                                lambda e: e.get("mode") == "distribute" and not e.get("profile_id"))


def _get_or_create_entry(fields: dict, matches) -> str:
    now = datetime.now(timezone.utc)
    with _lock:
        data = _load()
        # Prune expired entries and look for a reusable one in the same pass.
        alive = {tok: entry for tok, entry in data.items() if not _is_expired(entry, now)}
        for tok, entry in alive.items():
            if matches(entry):
                # A relaunch reuses this token, so its 30 days start again
                # rather than running out mid-session.
                if _touch(alive, tok, now) or len(alive) != len(data):
                    _save(alive)
                return tok
        token = secrets.token_urlsafe(32)
        alive[token] = {**fields, "created_at": now.isoformat()}
        _save(alive)
        return token


def resolve(token: str) -> Optional[str]:
    """The profile_id this token is pinned to, or None. Kept for callers that
    only care about pinning; `resolve_grant` is the fuller answer."""
    return resolve_grant(token).forced_profile_id


def resolve_grant(token: str) -> SessionGrant:
    """What this token grants, or an empty grant if it is unknown or expired.
    Not a security check on its own — it is an equality comparison against a
    large random token, the same trust model as placeholder_token — and the
    caller decides what "not found" means."""
    now = datetime.now(timezone.utc)
    with _lock:
        data = _load()
        entry = data.get(token)
        if entry is None or _is_expired(entry, now):
            return SessionGrant()
        # Presenting the token keeps it alive, so a session that is in use
        # never expires; only abandoned ones do.
        if _touch(data, token, now):
            _save(data)
            entry = data[token]
    profile_id = entry.get("profile_id")
    if profile_id:
        return SessionGrant(forced_profile_id=profile_id)  # a pin always wins
    return SessionGrant(distribute=entry.get("mode") == "distribute")
