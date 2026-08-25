"""OpenAI/ChatGPT-subscription token-refresh support for Claude Unlimited.

Mirrors oauth_login.py's role, but against the endpoint and client id the
`codex login` flow uses.

The interactive browser login itself lives in cli.py's add_codex_account(),
which spawns `codex login` under an isolated CODEX_HOME. This module covers
what happens afterwards: refresh_access_token() exchanges a stored
refresh_token for a new access token, called from gateway.py's proactive
refresh path.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

# The Codex CLI's own client id and token endpoint. Not a secret: this is a
# public OAuth client id for a PKCE public client.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"


class OpenAILoginError(RuntimeError):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class RefreshedTokens:
    access_token: str
    refresh_token: Optional[str]
    id_token: Optional[str]


def _parse_curl_header_dump(raw: bytes) -> dict:
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
    """Prefers curl over urllib, as oauth_login.py does for Anthropic's
    equivalent endpoint. Cloudflare bot management fronts these auth hosts
    and rejects bare Python urllib.request on TLS fingerprint, so curl is
    used defensively here too."""
    import tempfile
    from pathlib import Path

    header_file = tempfile.NamedTemporaryFile(prefix="claude-unlimited-openai-oauth-headers-", delete=False)
    header_path = header_file.name
    header_file.close()
    header_args = []
    for name, value in {"Content-Type": "application/json", **(extra_headers or {})}.items():
        header_args += ["-H", f"{name}: {value}"]
    try:
        try:
            proc = subprocess.run(
                ["curl", "-sS", "-X", "POST", url,
                 *header_args,
                 "--data-binary", "@-",
                 "-D", header_path,
                 "-w", "\n%{http_code}",
                 "--max-time", str(max(1, int(timeout)))],
                input=payload, capture_output=True, timeout=timeout + 5,
            )
        except subprocess.TimeoutExpired as exc:
            raise OpenAILoginError(f"Timed out reaching the token endpoint: {exc}") from exc

        if proc.returncode != 0:
            raise OpenAILoginError(
                f"Could not reach the token endpoint (curl exit {proc.returncode}): "
                f"{proc.stderr.decode('utf-8', errors='replace')[:300]}"
            )

        body, _, status_str = proc.stdout.rpartition(b"\n")
        if not status_str.isdigit():
            raise OpenAILoginError("Could not parse curl's response to the token endpoint.")

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
        raise OpenAILoginError(f"Could not reach the token endpoint: {exc}") from exc


def _post_json(url: str, payload: bytes, timeout: float,
                extra_headers: Optional[dict] = None) -> tuple[int, dict, bytes]:
    if shutil.which("curl"):
        return _post_json_via_curl(url, payload, timeout, extra_headers)
    return _post_json_via_urllib(url, payload, timeout, extra_headers)


def refresh_access_token(refresh_token: str, timeout: float = 30.0) -> RefreshedTokens:
    """Exchanges a stored refresh_token for a new access token: POST
    auth.openai.com/oauth/token with JSON body {client_id, grant_type:
    "refresh_token", refresh_token}, the same shape the Codex CLI sends.

    No retry-with-backoff here, unlike oauth_login.py: gateway.py's shared
    _try_refresh already owns retry, backoff and rate-limit handling."""
    payload = json.dumps({
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode("utf-8")

    status, _headers, body = _post_json(TOKEN_ENDPOINT, payload, timeout)

    if status >= 400:
        detail = body.decode("utf-8", errors="replace")[:300]
        raise OpenAILoginError(f"Token refresh failed (HTTP {status}): {detail}", status_code=status)

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise OpenAILoginError(f"Token refresh returned invalid JSON: {exc}") from exc

    access_token = data.get("access_token")
    if not access_token:
        raise OpenAILoginError("Token refresh response had no access_token.")

    return RefreshedTokens(
        access_token=access_token,
        refresh_token=data.get("refresh_token"),  # may be absent; caller keeps the old one
        id_token=data.get("id_token"),
    )
