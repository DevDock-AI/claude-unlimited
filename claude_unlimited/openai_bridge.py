"""Orchestrates one Claude Code request through a codex-kind Profile:
credential refresh, request translation, the HTTPS call to OpenAI's backend,
and response translation back to Anthropic's shape. The single entry point
gateway.py's codex branch calls into.

Talks to the same backend endpoints the `codex` CLI does —
https://chatgpt.com/backend-api/codex/responses for a ChatGPT/Codex
subscription, https://api.openai.com/v1/responses for a raw API key — with
matching request shapes and headers. See openai_translate.py for the
Anthropic <-> OpenAI translation itself, and openai_credential.py /
openai_login.py for the credential side.
"""

from __future__ import annotations

import http.client
import json
import os
import platform
import time
from dataclasses import dataclass
from typing import Iterator, Optional
from urllib.parse import urlsplit

from . import openai_credential, openai_login
from .config import Profile
from .openai_models import map_model
from .openai_translate import ResponseTranslator, anthropic_request_to_openai

CHATGPT_BACKEND_URL = "https://chatgpt.com/backend-api/codex/responses"
API_KEY_BACKEND_URL = "https://api.openai.com/v1/responses"

# Only the version component of the User-Agent string. Nothing here depends
# on the `codex` binary being installed, so drift from a locally installed
# version is harmless.
CODEX_CLI_VERSION = "0.149.0"
ORIGINATOR = "codex_cli_rs"

DEFAULT_TIMEOUT_SECONDS = 120
CHUNK_READ_SIZE = 8192


class OpenAIBridgeError(Exception):
    """A local or network-level failure with no response to classify.

    Mirrors upstream.py's OSError-shaped failures on the Anthropic path, so
    gateway.py's codex branch handles it the same way (cooldown, rotate)."""


@dataclass
class OpenAIBridgeResult:
    status: int
    headers: dict[str, str]
    body_chunks: Iterator[bytes]


def _uuid7() -> str:
    """A UUIDv7 (RFC 9562), the shape the Codex CLI's thread and session ids
    use. Python's stdlib `uuid` gained uuid7() only in 3.14 and this project
    targets 3.10+, so it is implemented here rather than adding a
    dependency."""
    unix_ms = int(time.time() * 1000)
    rand = os.urandom(10)
    b = bytearray(16)
    b[0:6] = unix_ms.to_bytes(6, "big")
    b[6] = 0x70 | (rand[0] & 0x0F)
    b[7] = rand[1]
    b[8] = 0x80 | (rand[2] & 0x3F)
    b[9:16] = rand[3:10]
    hex_str = b.hex()
    return f"{hex_str[0:8]}-{hex_str[8:12]}-{hex_str[12:16]}-{hex_str[16:20]}-{hex_str[20:32]}"


def _codex_user_agent() -> str:
    """The Codex CLI's User-Agent format: "{originator}/{version} ({os_type}
    {os_version}; {arch})". The terminal-info suffix that client also
    appends is omitted; the provider/version/os/arch prefix is the part that
    identifies the client."""
    system = platform.system()
    if system == "Darwin":
        os_type, os_version = "Mac OS", (platform.mac_ver()[0] or "unknown")
    elif system == "Linux":
        os_type, os_version = "Linux", (platform.release() or "unknown")
    elif system == "Windows":
        os_type, os_version = "Windows", (platform.release() or "unknown")
    else:
        os_type, os_version = (system or "unknown"), (platform.release() or "unknown")
    arch = platform.machine() or "unknown"
    return f"{ORIGINATOR}/{CODEX_CLI_VERSION} ({os_type} {os_version}; {arch})"


# Per-Profile throttle for _refresh_if_needed: profile id -> the monotonic
# time before which another refresh attempt must not be made. Load-bearing.
# Without it, a 429 from OpenAI's token endpoint would be retried on every
# request and the rate limit would never clear.
_REFRESH_CHECK_COOLDOWN_SECONDS = 60.0
_RATE_LIMIT_BACKOFF_SECONDS = 900.0
_refresh_not_before: dict[str, float] = {}

_INSTALLATION_ID_CACHE: Optional[str] = None


def _installation_id() -> str:
    """A stable per-install id, mirroring Codex's x-codex-installation-id.

    Generated on the first codex-kind request and persisted under APP_DIR,
    so it stays stable across restarts."""
    global _INSTALLATION_ID_CACHE
    if _INSTALLATION_ID_CACHE is not None:
        return _INSTALLATION_ID_CACHE
    from .config import APP_DIR, ensure_app_dir
    ensure_app_dir()
    path = APP_DIR / "codex_installation_id"
    try:
        _INSTALLATION_ID_CACHE = path.read_text().strip()
        if _INSTALLATION_ID_CACHE:
            return _INSTALLATION_ID_CACHE
    except OSError:
        pass
    _INSTALLATION_ID_CACHE = _uuid7()
    try:
        path.write_text(_INSTALLATION_ID_CACHE)
    except OSError:
        pass
    return _INSTALLATION_ID_CACHE


def _build_headers(cred: openai_credential.StoredOpenAICredential, *, is_subscription: bool) -> dict[str, str]:
    session_id = _uuid7()
    thread_id = _uuid7()
    headers = {
        "Authorization": f"Bearer {cred.access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "originator": ORIGINATOR,
        "User-Agent": _codex_user_agent(),
        "session-id": session_id,
        "thread-id": thread_id,
        "x-client-request-id": thread_id,
    }
    if is_subscription and cred.account_id:
        headers["ChatGPT-Account-ID"] = cred.account_id
    return headers


def _refresh_if_needed(profile: Profile, cred: openai_credential.StoredOpenAICredential) -> openai_credential.StoredOpenAICredential:
    """Proactive refresh, mirroring gateway.py's _maybe_refresh_credential.

    Applies only to chatgpt_subscription auth_mode; an api_key credential
    has no refresh_token or expiry to check. A failure is a silent no-op:
    the request goes out with whatever credential is on hand, and a 401 from
    the request itself is what drives AUTH_INVALID."""
    if not cred.refresh_token or not openai_credential.is_expiring_soon(cred.access_token):
        return cred

    now = time.monotonic()
    not_before = _refresh_not_before.get(profile.id)
    if not_before is not None and now < not_before:
        return cred  # still inside the backoff window from a recent failed attempt
    _refresh_not_before[profile.id] = now + _REFRESH_CHECK_COOLDOWN_SECONDS

    try:
        refreshed = openai_login.refresh_access_token(cred.refresh_token)
    except openai_login.OpenAILoginError as exc:
        if exc.status_code == 429:
            # A rate limit, not a dead refresh_token: back off far longer
            # than the normal cooldown.
            _refresh_not_before[profile.id] = now + _RATE_LIMIT_BACKOFF_SECONDS
        return cred
    new_cred = openai_credential.StoredOpenAICredential(
        access_token=refreshed.access_token,
        refresh_token=refreshed.refresh_token or cred.refresh_token,
        account_id=cred.account_id,
        id_token=refreshed.id_token or cred.id_token,
    )
    try:
        from . import profiles as profile_repo
        # update_credential_raw, NOT update_credential: the latter re-encodes
        # through oauth_credential.py's Anthropic-specific blob shape, which
        # would wrap this already-encoded JSON string inside another one and
        # break every future decode.
        profile_repo.update_credential_raw(profile.id, openai_credential.encode(new_cred))
    except Exception:
        pass  # the refreshed credential still serves this request even if persisting failed
    return new_cred


def run(profile: Profile, stored_credential: str, body: bytes,
        timeout: float = DEFAULT_TIMEOUT_SECONDS) -> OpenAIBridgeResult:
    """Runs one Claude Code request through a codex-kind Profile: decode the
    credential, maybe refresh it, translate the Anthropic request body, make
    the HTTPS call, and return a lazily-translated body_chunks generator of
    Anthropic SSE bytes the caller streams straight to the client.

    The result mirrors upstream.py's UpstreamResponse closely enough that
    gateway.py's wrapping still applies on top. Raises OpenAIBridgeError
    only when there was no response to classify (DNS or connection failure);
    a non-2xx status is returned normally for the caller's own Observation
    classification, exactly as on the Anthropic path."""
    try:
        cred = openai_credential.decode(stored_credential)
    except (json.JSONDecodeError, KeyError) as exc:
        raise OpenAIBridgeError(f"Stored codex credential is malformed: {exc}") from exc

    is_subscription = profile.auth_mode != "api_key"
    if is_subscription:
        cred = _refresh_if_needed(profile, cred)

    try:
        anthropic_body = json.loads(body) if body else {}
    except json.JSONDecodeError as exc:
        raise OpenAIBridgeError(f"Request body was not valid JSON: {exc}") from exc

    target = map_model(
        anthropic_body.get("model"),
        override_model=profile.codex_model,
        override_reasoning_effort=profile.codex_reasoning_effort,
    )
    openai_body = anthropic_request_to_openai(anthropic_body, target)
    payload = json.dumps(openai_body).encode("utf-8")

    if is_subscription:
        url = CHATGPT_BACKEND_URL
    else:
        base = (profile.base_url or "https://api.openai.com/v1").rstrip("/")
        url = f"{base}/responses"

    headers = _build_headers(cred, is_subscription=is_subscription)
    headers["Content-Length"] = str(len(payload))

    parts = urlsplit(url)
    try:
        conn = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=timeout)
        conn.request("POST", parts.path or "/", body=payload, headers=headers)
        resp = conn.getresponse()
    except OSError as exc:
        raise OpenAIBridgeError(f"Could not reach {parts.hostname}: {exc}") from exc

    response_headers = dict(resp.getheaders())

    if resp.status >= 400:
        # An error response is never SSE, so read it whole (error bodies are
        # small) and hand it back as one Anthropic-shaped error chunk. The
        # SSE translator only understands `event: ... / data: ...` frames.
        raw = resp.read()
        conn.close()

        def _error_chunks() -> Iterator[bytes]:
            message = raw.decode("utf-8", errors="replace")[:2000]
            yield json.dumps({"type": "error", "error": {"type": "api_error", "message": message}}).encode("utf-8")

        return OpenAIBridgeResult(status=resp.status, headers=response_headers, body_chunks=_error_chunks())

    def _translated_chunks() -> Iterator[bytes]:
        translator = ResponseTranslator()
        buffer = b""
        try:
            while True:
                chunk = resp.read(CHUNK_READ_SIZE)
                if not chunk:
                    break
                buffer += chunk
                while b"\n\n" in buffer:
                    frame, _, buffer = buffer.partition(b"\n\n")
                    event = _parse_sse_frame(frame)
                    if event is not None:
                        yield from translator.feed(event)
        finally:
            conn.close()

    return OpenAIBridgeResult(status=200, headers=response_headers, body_chunks=_translated_chunks())


def _parse_sse_frame(frame: bytes) -> Optional[dict]:
    """One `event: X\\ndata: {...}` block -> the parsed data payload, which
    carries its own "type" field matching the event name per OpenAI's
    Responses API SSE shape. Returns None for a frame with no data line (a
    comment or keepalive) or malformed JSON; an unparseable frame is skipped
    rather than fatal."""
    data_line = None
    for line in frame.split(b"\n"):
        if line.startswith(b"data:"):
            data_line = line[len(b"data:"):].strip()
            break
    if not data_line:
        return None
    try:
        return json.loads(data_line)
    except json.JSONDecodeError:
        return None
