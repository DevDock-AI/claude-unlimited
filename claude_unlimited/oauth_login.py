"""OAuth token-refresh support for Claude Unlimited.

The account-adding flow itself lives in cli.py's add_account(): a browser
login through an isolated Claude Code session (`CLAUDE_CONFIG_DIR`), read
back via anthropic_oauth.read_claude_code_credentials(). This module covers
what happens afterwards: refresh_access_token() exchanges a stored
refresh_token for a new access token, so a Profile never needs manual
re-authentication merely because its token expired.

CLIENT_ID is Claude Code's own public OAuth client id; PKCE public clients
don't use a client secret, so this is not a leaked credential, it's how
the protocol is designed to work for a locally-run CLI.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
TOKEN_ENDPOINT = "https://platform.claude.com/v1/oauth/token"

# Make our refresh request look like Claude Code's, which refreshes the same
# grants for weeks without being rate limited from the same machine/IP. Anthropic's
# token endpoint appears to bucket refreshes by client shape: a call that omits the
# scope field and doesn't carry Claude Code's axios User-Agent lands in a throttled
# bucket and gets a persistent 429 (rate_limit_error), even on a brand-new grant.
# These two values (extracted from the Claude Code 2.1.252 bundle) close the visible
# gap. The one thing we still can't match from Python/curl is the TLS/JA3
# fingerprint (curl vs node); if that turns out to be what's classified, the
# fallback is to delegate the refresh to the real `claude` CLI per account.
REFRESH_SCOPE = "user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"
REFRESH_USER_AGENT = "axios/1.9.0"


class OAuthLoginError(RuntimeError):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        # Lets a caller distinguish a 429 (backoff requested) from a 400
        # (dead refresh_token) without parsing the message text. None for a
        # network-level failure, where there was no response to read a
        # status from.
        self.status_code = status_code


@dataclass(frozen=True)
class LoginTokens:
    access_token: str
    refresh_token: Optional[str]
    expires_at: Optional[int]


def _parse_curl_header_dump(raw: bytes) -> dict:
    """Parses curl's `-D` header-block output (one status line, then
    `Name: value` lines, ending at the blank line before the body) into a
    lowercased-key dict. No `-L` is passed to curl, so there is never more
    than one status line or header block."""
    headers = {}
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line or line.startswith("HTTP/"):
            continue
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return headers


def _post_json_via_curl(url: str, payload: bytes, timeout: float,
                         extra_headers: Optional[dict] = None) -> tuple[int, dict, bytes]:
    """Caller must already know curl is on PATH. Raises OAuthLoginError on
    any curl-level failure (network error, timeout) rather than falling back
    to urllib: if curl couldn't reach the host, urllib won't either, and the
    fallback would only obscure the error."""
    import tempfile
    from pathlib import Path

    header_file = tempfile.NamedTemporaryFile(prefix="claude-unlimited-oauth-headers-", delete=False)
    header_path = header_file.name
    header_file.close()
    header_args = []
    for name, value in {"Content-Type": "application/json", **(extra_headers or {})}.items():
        header_args += ["-H", f"{name}: {value}"]
    try:
        try:
            proc = subprocess.run(
                ["curl", "-sS", "--compressed", "-X", "POST", url,  # --compressed: Accept-Encoding like axios, curl decodes the body
                 *header_args,
                 "--data-binary", "@-",
                 "-D", header_path,  # separate file, so stdout stays body-only (see -w below)
                 "-w", "\n%{http_code}",
                 "--max-time", str(max(1, int(timeout)))],
                input=payload, capture_output=True, timeout=timeout + 5,
            )
        except subprocess.TimeoutExpired as exc:
            raise OAuthLoginError(f"Timed out reaching the token endpoint: {exc}") from exc

        if proc.returncode != 0:
            raise OAuthLoginError(
                f"Could not reach the token endpoint (curl exit {proc.returncode}): "
                f"{proc.stderr.decode('utf-8', errors='replace')[:300]}"
            )

        body, _, status_str = proc.stdout.rpartition(b"\n")
        if not status_str.isdigit():
            raise OAuthLoginError("Could not parse curl's response to the token endpoint.")

        headers = _parse_curl_header_dump(Path(header_path).read_bytes())
        return int(status_str), headers, body
    finally:
        Path(header_path).unlink(missing_ok=True)


def _post_json_via_urllib(url: str, payload: bytes, timeout: float,
                           extra_headers: Optional[dict] = None) -> tuple[int, dict, bytes]:
    headers = {"Content-Type": "application/json", **(extra_headers or {})}
    req = urllib.request.Request(url, data=payload, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()
    except urllib.error.URLError as exc:
        raise OAuthLoginError(f"Could not reach the token endpoint: {exc}") from exc


def _post_json(url: str, payload: bytes, timeout: float,
                extra_headers: Optional[dict] = None) -> tuple[int, dict, bytes]:
    """POST JSON and return (status_code, headers, body).

    Prefers curl over urllib.request: Cloudflare bot management in front of
    platform.claude.com rejects Python's bare urllib.request with a 403
    ("error code: 1010") before the request reaches Anthropic, while curl's
    TLS fingerprint is not flagged the same way. Falls back to urllib only
    when curl isn't on PATH, which is degraded (the call may be blocked)
    rather than broken."""
    if shutil.which("curl"):
        return _post_json_via_curl(url, payload, timeout, extra_headers)
    return _post_json_via_urllib(url, payload, timeout, extra_headers)


def _normalize_expires_at(data: dict) -> Optional[int]:
    """Normalizes the token endpoint's expiry fields to epoch milliseconds,
    the unit Claude Code uses on disk (see anthropic_oauth.py's
    ImportedCredentials).

    Handles both shapes defensively: an absolute expires_at in seconds or
    milliseconds, disambiguated by magnitude (under 10**12 is seconds), or,
    failing that, a relative expires_in in seconds added to now."""
    raw = data.get("expires_at")
    if raw is not None:
        try:
            raw = float(raw)
        except (TypeError, ValueError):
            raw = None
    if raw is not None:
        return int(raw * 1000) if raw < 1e12 else int(raw)

    expires_in = data.get("expires_in")
    if expires_in is not None:
        try:
            return int(time.time() * 1000) + int(float(expires_in) * 1000)
        except (TypeError, ValueError):
            return None
    return None


def refresh_access_token(refresh_token: str, timeout: float = 30.0) -> LoginTokens:
    """Exchanges a stored refresh_token for a new access token, so an OAuth
    Profile keeps working past its original token's expiry without a manual
    re-authentication.

    Retries up to twice with exponential backoff on a 5xx or network-level
    failure. A 4xx is never retried: a dead refresh_token means the account
    needs a real re-login, not another attempt."""
    payload = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
        "scope": REFRESH_SCOPE,  # Claude Code sends this; omitting it is bucketed/throttled
    }).encode("utf-8")

    max_retries = 2
    base_delay = 0.5
    last_exc: Optional[OAuthLoginError] = None

    for attempt in range(max_retries + 1):
        if attempt > 0:
            time.sleep(base_delay * (2 ** (attempt - 1)))
        try:
            status, _headers, body = _post_json(
                TOKEN_ENDPOINT, payload, timeout,
                extra_headers={
                    "Accept": "application/json, text/plain, */*",
                    "User-Agent": REFRESH_USER_AGENT,  # match Claude Code's axios client
                },
            )
        except OAuthLoginError as exc:
            last_exc = exc
            continue  # network-level failure: retry

        if status >= 500 and attempt < max_retries:
            continue  # transient server error: retry
        if status >= 400:
            detail = body.decode("utf-8", errors="replace")[:300]
            raise OAuthLoginError(f"Token refresh failed (HTTP {status}): {detail}", status_code=status)

        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise OAuthLoginError(f"Token refresh response wasn't valid JSON: {exc}") from exc

        access_token = data.get("access_token")
        if not access_token:
            raise OAuthLoginError("Token refresh response had no access_token — unexpected response shape.")

        return LoginTokens(
            access_token=access_token,
            # A rotating refresh_token replaces itself in the response; a
            # static one may be omitted entirely, so keep the one the
            # caller already had rather than losing it.
            refresh_token=data.get("refresh_token") or refresh_token,
            expires_at=_normalize_expires_at(data),
        )

    raise last_exc or OAuthLoginError("Token refresh failed after retries.")
