"""Serving the official `codex` CLI from the pool (issue #7).

`claude-unlimited codex` starts the real `codex` with a custom model provider
pointed at this daemon (`/codex/v1`, see launch_overrides). codex then sends
its own Responses API requests here, and the gateway forwards each one to the
ChatGPT Codex backend on one of the pool's ChatGPT-subscription accounts.

Nothing is translated: codex already speaks the backend's dialect. This module
is the small set of things a passthrough still has to get right:

- which request headers go upstream, and which response headers come back;
- what to drop from a conversation when it moves to a different account
  (strip_account_bound — reasoning ciphertext is minted per account);
- reading token usage off the stream without changing a byte of it;
- errors in the exact shape codex acts on: a pool with no capacity left is a
  429 `usage_limit_reached` (codex prints "try again at HH:MM" and does not
  retry), anything the user must fix is a 400 (shown, not retried). A 5xx
  would make codex retry 30 times in 24 seconds.

The decisions behind these rules are in docs/adr/0010-serving-the-codex-cli.md.
"""

from __future__ import annotations

import http.client
import json
from datetime import datetime, timezone
from typing import Iterator, Optional

from . import openai_credential
from .openai_bridge import CHATGPT_BACKEND_URL, CHUNK_READ_SIZE, DEFAULT_TIMEOUT_SECONDS, OpenAIBridgeError

BASE_PATH = "/codex/v1"
RESPONSES_PATH = BASE_PATH + "/responses"
MODELS_PATH = BASE_PATH + "/models"
PROVIDER_ID = "claude-unlimited"
TOKEN_ENV = "CLAUDE_UNLIMITED_CODEX_TOKEN"

# Request headers codex sends that describe the conversation, the client and
# the turn. Forwarded unchanged so the backend sees exactly what a codex
# session sends — session-id/thread-id are its cache-affinity key. Everything
# else (Host, Authorization, Content-Length, Accept-Encoding, Connection…) is
# rebuilt here.
_FORWARDED_REQUEST_HEADERS = (
    "originator", "user-agent", "session-id", "thread-id", "x-client-request-id",
    "x-codex-window-id", "x-codex-turn-metadata", "x-codex-beta-features",
    "x-codex-parent-thread-id", "x-openai-subagent", "x-openai-internal-codex-responses-lite",
    "x-codex-turn-state", "openai-beta",
)
# Response headers codex reads: the quota windows for /status and its own
# usage-limit message, the per-turn sticky token, and request ids for errors.
_RETURNED_RESPONSE_PREFIXES = ("x-codex-", "openai-")
_RETURNED_RESPONSE_HEADERS = ("content-type", "x-request-id", "x-oai-request-id", "retry-after", "cf-ray",
                              "etag", "x-models-etag")


def launch_overrides(port: int) -> list[str]:
    """The `-c` arguments that point one codex invocation at this daemon.

    Per-invocation on purpose: the user's own ~/.codex is never written, so
    their history, MCP servers and skills keep working, and plain `codex`
    still uses their own login. A custom provider (not the built-in `openai`
    one) is what keeps codex from needing a ChatGPT login, calling /models,
    opening a WebSocket or compressing bodies — see ADR 0010."""
    provider = (f'{{ name="Claude Unlimited", base_url="http://127.0.0.1:{int(port)}{BASE_PATH}", '
                f'env_key="{TOKEN_ENV}", wire_api="responses" }}')
    return ["-c", f'model_provider="{PROVIDER_ID}"', "-c", f"model_providers.{PROVIDER_ID}={provider}"]


def thread_key(parsed: dict, headers: dict) -> Optional[str]:
    """The conversation a request belongs to: codex's prompt_cache_key, which
    is its thread id (also sent as the `thread-id` header)."""
    key = parsed.get("prompt_cache_key")
    if isinstance(key, str) and key:
        return key
    lowered = {k.lower(): v for k, v in headers.items()}
    return lowered.get("thread-id") or lowered.get("session-id") or None


def has_account_bound_items(parsed: dict) -> bool:
    return any(_is_account_bound(item) for item in parsed.get("input") or [] if isinstance(item, dict))


def _is_account_bound(item: dict) -> bool:
    return item.get("type") == "reasoning" or "encrypted_content" in item


def strip_account_bound(parsed: dict) -> tuple[bytes, int]:
    """The request without the items only the account that minted them can
    read: reasoning items (their encrypted_content is per account) and any
    other item carrying ciphertext. Everything else — every message, tool call
    and tool output — is kept, so the conversation itself is intact; the model
    just re-reasons from it. Returns (body, items dropped).

    Only ever used when a conversation moves to a different account, or when
    the backend refused the ciphertext. Our Claude Code bridge never sends
    reasoning back at all, so the backend demonstrably accepts a history
    without it."""
    items = parsed.get("input")
    if not isinstance(items, list):
        return json.dumps(parsed).encode("utf-8"), 0
    kept = [item for item in items if not (isinstance(item, dict) and _is_account_bound(item))]
    stripped = dict(parsed, input=kept)
    return json.dumps(stripped).encode("utf-8"), len(items) - len(kept)


def looks_like_refused_ciphertext(status: int, text: str) -> bool:
    lowered = text.lower()
    return status == 400 and ("encrypted" in lowered or "reasoning" in lowered)


def outbound_headers(client_headers: dict, cred: openai_credential.StoredOpenAICredential,
                     content_length: Optional[int], keep_turn_state: bool) -> dict:
    lowered = {k.lower(): v for k, v in client_headers.items()}
    headers = {name: lowered[name] for name in _FORWARDED_REQUEST_HEADERS if lowered.get(name)}
    if not keep_turn_state:
        # The turn token is minted by the account that served the turn's
        # first request; another account cannot honour it.
        headers.pop("x-codex-turn-state", None)
    headers["Authorization"] = f"Bearer {cred.access_token}"
    if content_length is None:          # GET /models
        headers["Accept"] = "application/json"
        for name in ("if-none-match",):
            if lowered.get(name):
                headers[name] = lowered[name]
    else:
        headers.update({"Content-Type": "application/json", "Accept": "text/event-stream",
                        "Content-Length": str(content_length)})
    if cred.account_id:
        headers["ChatGPT-Account-ID"] = cred.account_id
    version = client_version(lowered.get("user-agent", ""))
    if version:
        # What codex sends with its own ChatGPT login; a custom provider
        # leaves it out.
        headers["version"] = version
    return headers


def client_version(user_agent: str) -> Optional[str]:
    """`codex_exec/0.149.0 (Mac OS 26.0; arm64) …` -> `0.149.0`."""
    head = user_agent.split(" ", 1)[0]
    _, _, version = head.partition("/")
    return version or None


def client_response_headers(upstream_headers: dict) -> dict:
    out = {}
    for name, value in upstream_headers.items():
        lower = name.lower()
        if lower in _RETURNED_RESPONSE_HEADERS or lower.startswith(_RETURNED_RESPONSE_PREFIXES):
            out[lower] = value
    out.setdefault("content-type", "text/event-stream")
    return out


def send(cred: openai_credential.StoredOpenAICredential, payload: Optional[bytes], headers: dict,
         timeout: float = DEFAULT_TIMEOUT_SECONDS, *, method: str = "POST", path: str = "/responses"):
    """One call to the Codex backend (`path` is relative to
    .../backend-api/codex, query string included). Returns (status, headers,
    response, connection); the caller streams or reads the response. Raises
    OpenAIBridgeError when the backend could not be reached at all."""
    from urllib.parse import urlsplit
    from .upstream import _connect as upstream_connect

    parts = urlsplit(CHATGPT_BACKEND_URL)
    base_path = parts.path.rsplit("/", 1)[0]          # /backend-api/codex
    try:
        conn = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=timeout)
        conn._create_connection = upstream_connect
        conn.request(method, base_path + path, body=payload, headers=headers)
        resp = conn.getresponse()
    except (OSError, http.client.HTTPException) as exc:
        # HTTPException covers a malformed status line or header, which is not
        # an OSError: left uncaught it would drop codex's connection unanswered.
        raise OpenAIBridgeError(f"Could not reach {parts.hostname}: {exc}") from exc
    return resp.status, dict(resp.getheaders()), resp, conn


def relay(resp, conn) -> Iterator[bytes]:
    """The response body, byte for byte. ChatGPT's backend sometimes closes a
    chunked stream without the final terminator after a complete response;
    the bytes that did arrive are passed on and codex judges completeness
    itself (it requires response.completed)."""
    try:
        while True:
            try:
                # read1, not read: on a chunked response read(n) waits until n
                # bytes have gathered, so small SSE events would reach codex
                # in 8 KB bursts instead of as they arrive.
                chunk = resp.read1(CHUNK_READ_SIZE)
            except http.client.IncompleteRead as exc:
                if exc.partial:
                    yield exc.partial
                break
            if not chunk:
                break
            yield chunk
    finally:
        conn.close()


class UsageTee:
    """Reads model and token usage from a Responses SSE stream as it passes,
    never changing it. The usage is mapped to the same fields the Claude Code
    bridge records (openai_translate): OpenAI counts cached tokens INSIDE
    input_tokens, Anthropic-style accounting keeps them apart."""

    def __init__(self) -> None:
        self.model: Optional[str] = None
        self.usage: Optional[dict] = None
        self._buffer = b""

    def wrap(self, chunks: Iterator[bytes]) -> Iterator[bytes]:
        for chunk in chunks:
            if self.usage is None:
                self._feed(chunk)
            yield chunk

    def _feed(self, chunk: bytes) -> None:
        self._buffer += chunk.replace(b"\r\n", b"\n")
        while b"\n\n" in self._buffer:
            frame, _, self._buffer = self._buffer.partition(b"\n\n")
            data = b"".join(line[5:].strip() for line in frame.split(b"\n") if line.startswith(b"data:"))
            if b"response.completed" not in data:
                continue
            try:
                event = json.loads(data)
            except ValueError:
                continue
            if event.get("type") != "response.completed":
                continue
            response = event.get("response") or {}
            self.model = response.get("model") or self.model
            self.usage = map_usage(response.get("usage") or {})
            self._buffer = b""
            return


def map_usage(usage: dict) -> dict:
    in_details = usage.get("input_tokens_details") or {}
    out_details = usage.get("output_tokens_details") or {}
    total_input = int(usage.get("input_tokens", 0) or 0)
    cached = min(int(in_details.get("cached_tokens", 0) or 0), total_input)
    return {
        "input_tokens": total_input - cached,
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": 0,
        "reasoning_tokens": int(out_details.get("reasoning_tokens", 0) or 0),
    }


def usage_limit_resets_at(raw: bytes) -> Optional[datetime]:
    """resets_at from a backend `usage_limit_reached` body, if it has one."""
    try:
        error = json.loads(raw).get("error") or {}
        if error.get("type") == "usage_limit_reached" and error.get("resets_at"):
            return datetime.fromtimestamp(int(error["resets_at"]), tz=timezone.utc)
    except (ValueError, TypeError, AttributeError, OverflowError, OSError):
        pass
    return None


def error_body(message: str, *, error_type: str = "invalid_request_error") -> bytes:
    return json.dumps({"error": {"type": error_type, "code": error_type,
                                 "message": f"[claude-unlimited] {message}"}}).encode("utf-8")


def usage_limit_body(resets_at: Optional[datetime], message: str) -> bytes:
    """codex's own usage-limit error: it prints its "try again at HH:MM"
    message from resets_at and does not retry."""
    error = {"type": "usage_limit_reached", "message": f"[claude-unlimited] {message}"}
    if resets_at is not None:
        error["resets_at"] = int(resets_at.timestamp())
    return json.dumps({"error": error}).encode("utf-8")
