"""Encodes/decodes what's stored for a ChatGPT/Codex-subscription credential,
and the JWT-decoding helpers needed to read its claims (expiry, account id,
plan) without a network call. Mirrors oauth_credential.py's role for
Anthropic OAuth Profiles, but for the shape Codex's own auth.json uses:
id_token, access_token, refresh_token, account_id.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Optional

# Refresh shortly before the access token expires rather than at the
# deadline itself.
EXPIRING_SOON_BUFFER_SECONDS = 5 * 60


@dataclass(frozen=True)
class StoredOpenAICredential:
    access_token: str
    refresh_token: Optional[str]
    account_id: Optional[str]  # ChatGPT-Account-ID header value
    id_token: Optional[str] = None  # kept for email/plan re-resolution on reauth, not sent on every request


def decode_jwt_claims(token: str) -> dict:
    """Reads a JWT's payload claims WITHOUT verifying the signature.

    Only used to read an already-trusted local token's own claims (expiry,
    account id) for scheduling decisions, never to validate a token
    presented by a third party. Returns {} for anything that doesn't parse
    as a 3-part JWT, so a malformed token fails open into "unknown expiry"
    instead of crashing the request path."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + padding)
        return json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return {}


def access_token_expires_at(access_token: str) -> Optional[float]:
    """Epoch seconds from the access token's `exp` claim, or None if it
    can't be read. An unknown expiry never forces a refresh."""
    claims = decode_jwt_claims(access_token)
    exp = claims.get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None


def is_expiring_soon(access_token: str, now: Optional[float] = None) -> bool:
    expires_at = access_token_expires_at(access_token)
    if expires_at is None:
        return False
    now = time.time() if now is None else now
    return now >= (expires_at - EXPIRING_SOON_BUFFER_SECONDS)


def chatgpt_plan_type(id_token: str) -> Optional[str]:
    """Reads the ChatGPT plan tier ("free"/"plus"/"pro"/"business"/
    "enterprise"/"edu") from the id_token's claims. The Anthropic equivalent
    needs fetch_account_profile(); here it is embedded locally, so no
    network call is needed."""
    claims = decode_jwt_claims(id_token)
    # The claim is nested under a namespaced key; probe the plausible shapes
    # and fail open to None. Display-only, never load-bearing for routing.
    for key in ("chatgpt_plan_type", "https://api.openai.com/auth", "https://openai.com/auth"):
        value = claims.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict) and isinstance(value.get("chatgpt_plan_type"), str):
            return value["chatgpt_plan_type"]
    return None


def chatgpt_email(id_token: str) -> Optional[str]:
    claims = decode_jwt_claims(id_token)
    email = claims.get("email")
    return email if isinstance(email, str) else None


def encode(cred: StoredOpenAICredential) -> str:
    return json.dumps({
        "access_token": cred.access_token,
        "refresh_token": cred.refresh_token,
        "account_id": cred.account_id,
        "id_token": cred.id_token,
    })


def decode(raw: str) -> StoredOpenAICredential:
    """Must accept BOTH stored shapes.

    A chatgpt_subscription credential is the JSON blob encode() produces.
    An api_key-mode codex Profile is created through
    profiles.create_profile()'s default path, which calls
    oauth_credential.encode() with no refresh_token/expires_at and therefore
    stores a bare string — so a plain OpenAI API key arrives here unwrapped
    and must not be fed to json.loads()."""
    stripped = raw.strip()
    if not stripped.startswith("{"):
        return StoredOpenAICredential(access_token=raw, refresh_token=None, account_id=None, id_token=None)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return StoredOpenAICredential(access_token=raw, refresh_token=None, account_id=None, id_token=None)
    if not isinstance(data, dict) or "access_token" not in data:
        return StoredOpenAICredential(access_token=raw, refresh_token=None, account_id=None, id_token=None)
    return StoredOpenAICredential(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        account_id=data.get("account_id"),
        id_token=data.get("id_token"),
    )
