"""Encodes/decodes what's actually stored in secret_store for an OAuth
Profile.

Two on-disk shapes exist:

  * a plain access token string, used for a manually-pasted token or an
    `api` kind Profile;
  * a small JSON blob holding access_token, refresh_token and expires_at,
    used once a refresh token is available (CLI `login`, "Import current
    login"), so gateway.py can proactively refresh an expiring token before
    sending a doomed request upstream.

decode() must keep accepting both: nothing migrates a stored plain string,
so a Profile keeps that shape (and simply never self-refreshes) until it is
re-authenticated.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional

# How long before a token's real expiry it's treated as "expiring soon" and
# refreshed proactively.
#
# This MUST stay larger than Gateway._RATE_LIMIT_BACKOFF_SECONDS. A refresh
# that gets a 429 puts the Profile into that backoff, during which no refresh
# is attempted at all; if the buffer were shorter than the backoff there would
# be a window where the access token has already expired but refreshing is
# still blocked, so the next request goes out with a dead token, takes a 401,
# and the Profile lands on AUTH_INVALID needing manual re-auth.
# tests/test_oauth_credential.py locks the invariant.
EXPIRING_SOON_BUFFER_MS = 20 * 60 * 1000


@dataclass(frozen=True)
class StoredOAuthCredential:
    access_token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[int] = None  # epoch milliseconds — matches Claude Code's own on-disk unit


def encode(cred: StoredOAuthCredential) -> str:
    if cred.refresh_token is None and cred.expires_at is None:
        # Nothing to remember beyond the token itself, so keep the plain
        # string shape rather than wrapping it trivially.
        return cred.access_token
    return json.dumps({
        "access_token": cred.access_token,
        "refresh_token": cred.refresh_token,
        "expires_at": cred.expires_at,
    })


def decode(raw: str) -> StoredOAuthCredential:
    stripped = raw.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict) and "access_token" in data:
                return StoredOAuthCredential(
                    access_token=data["access_token"],
                    refresh_token=data.get("refresh_token"),
                    expires_at=data.get("expires_at"),
                )
        except json.JSONDecodeError:
            pass
    # Plain-string shape (or anything that didn't parse as the JSON blob):
    # an access token with no known refresh capability or expiry. A real
    # access token never starts with "{".
    return StoredOAuthCredential(access_token=raw)


def is_expiring_soon(cred: StoredOAuthCredential, now_ms: Optional[float] = None) -> bool:
    """True inside EXPIRING_SOON_BUFFER_MS of the known expiry, or already
    past it. False whenever expires_at isn't known, so a refresh is never
    forced on a guess."""
    if cred.expires_at is None:
        return False
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    return now_ms >= (cred.expires_at - EXPIRING_SOON_BUFFER_MS)
