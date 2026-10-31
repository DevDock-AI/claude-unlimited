"""Builds the upstream request for a chosen Profile. Pure, no network I/O.

Kept separate from the socket work in upstream.py so it is fully testable
without a real Anthropic connection.

Protocol details this module depends on:
  - Claude Code embeds `metadata.user_id` in the /v1/messages JSON body as a
    STRING that is itself JSON-encoded and contains an `account_uuid` field
    alongside other fields (e.g. a session id) that must be preserved. It is
    not a bare account uuid string.
  - `x-api-key` carries API-key auth; `Authorization: Bearer` carries OAuth
    and the "bearer" auth_mode.
  - `/v1/oauth/token` calls are relayed upstream untouched: the client
    manages its own token refresh and the daemon must not intercept it.

If the nested body shape isn't found, the body is passed through unrewritten
rather than guessed at. Only a body that matches the expected shape but
fails to re-serialize raises.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from . import observation
from .config import Profile

ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION_HEADER = "anthropic-version"

# Largest request body each kind of Profile's upstream accepts, so the daemon
# rejects exactly what the provider would and nothing it would take.
#   - Anthropic Messages API (oauth, and api Profiles, whose base_url is an
#     Anthropic-compatible endpoint): 32 MB. Anthropic's "Request size
#     limits" table; over it the API answers 413 request_too_large. A
#     gateway's own limit, if lower, is enforced by that gateway and relayed
#     back unchanged.
#   - Codex / OpenAI Responses (codex): 50 MB total payload per request, per
#     OpenAI's image-input requirements. The Anthropic body is translated
#     roughly 1:1 (base64 stays base64), so the inbound size is the measure.
# 1024-based on purpose: "32 MB" is not specified to the byte, and a body that
# slips under this cap but over the real one is refused by the provider with
# its own 413, which is relayed. The reverse (cap below the real limit) is
# what kills sessions, so round up.
MESSAGES_MAX_REQUEST_BYTES = 32 * 1024 * 1024
CODEX_MAX_REQUEST_BYTES = 50 * 1024 * 1024
# Above this the daemon will not even buffer a body (Content-Length is
# refused up front, before a Profile is chosen): 256 MB is Anthropic's largest
# endpoint (the Batch API), so nothing a Profile could take is larger. It is
# deliberately well above the per-Profile limits: ECO compaction can shrink a
# text-heavy body, and the per-Profile check runs on what would actually be
# sent.
MAX_REQUEST_BYTES = 256 * 1024 * 1024

# The status and body Anthropic itself returns for an oversized request:
# HTTP 413, error.type "request_too_large". Claude Code recognises exactly
# this shape and recovers (strips images/documents, compacts) instead of
# ending the session, so it must not be paraphrased.
REQUEST_TOO_LARGE_STATUS = 413
REQUEST_TOO_LARGE_BODY = {
    "type": "error",
    "error": {"type": "request_too_large",
              "message": "Request exceeds the maximum allowed number of bytes."},
}


class RequestTooLarge(ValueError):
    """The inbound body exceeds what this Profile's upstream accepts. A
    ValueError so older `except ValueError` callers still treat it as a
    structurally invalid request; callers that can answer precisely catch
    this first and return REQUEST_TOO_LARGE_STATUS."""


def max_request_bytes(profile: Profile) -> int:
    """The largest request body this Profile's upstream accepts."""
    return CODEX_MAX_REQUEST_BYTES if profile.kind == "codex" else MESSAGES_MAX_REQUEST_BYTES


def check_request_size(profile: Profile, body: bytes) -> None:
    """Raises RequestTooLarge if `body` is over this Profile's upstream limit."""
    limit = max_request_bytes(profile)
    if len(body) > limit:
        raise RequestTooLarge(f"request body is {len(body)} bytes; limit is {limit}")


# Headers stripped from the inbound (client -> daemon) request before
# forwarding upstream. Anything credential-shaped or hop-by-hop.
_STRIPPED_INBOUND_HEADERS = {
    "authorization",
    "x-api-key",
    "host",
    "connection",
    "content-length",  # recomputed after any body rewrite
    "proxy-connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    # The `claude` CLI sends this and Anthropic honors it, so the response
    # body arrives gzip- or brotli-compressed. The bytes are forwarded
    # either way, but usage_tracking.py's copy needs plain SSE text to
    # parse and this daemon has no Brotli decoder (stdlib only), so a
    # compressed response records no usage at all. Dropping the header
    # costs nothing on a loopback proxy and is allowed by spec: the server
    # then defaults to identity encoding.
    "accept-encoding",
}

# Response headers allowed to reach observation.classify(), and nothing
# else: the response-classification path only ever reads headers, never
# bodies. The rate-limit/quota names come from observation.ALLOWED_HEADERS
# so the two lists cannot drift apart.
RESPONSE_HEADER_ALLOWLIST = observation.ALLOWED_HEADERS + ("content-type",)


class AccountUuidRewriteError(ValueError):
    """Raised rather than forwarding a body that failed to rewrite: sending
    a mismatched account_uuid to Anthropic is worse than refusing."""


@dataclass(frozen=True)
class UpstreamRequest:
    url: str
    method: str
    headers: dict
    body: bytes


def resolve_base_url(profile: Profile) -> str:
    if profile.kind == "oauth":
        return ANTHROPIC_DEFAULT_BASE_URL
    return (profile.base_url or ANTHROPIC_DEFAULT_BASE_URL).rstrip("/")


def _is_messages_path(path: str) -> bool:
    return path.rstrip("/").endswith("/v1/messages")


def build_upstream_request(
    profile: Profile,
    credential: str,
    method: str,
    path: str,
    inbound_headers: dict[str, str],
    inbound_body: bytes,
    *,
    claude_effort: Optional[str] = None,
) -> UpstreamRequest:
    """`claude_effort`, when given, is written to `output_config.effort` on a
    /v1/messages body for oauth- and api-kind Profiles — the Claude side of a
    parity row's reasoning effort. It is the caller's job (gateway) to resolve
    it against the request's model and the per-model support gate
    (openai_models.claude_effort_for), so this module never guesses and stays
    free of a model-catalogue dependency. None = leave the body untouched."""
    check_request_size(profile, inbound_body)

    headers = {k: v for k, v in inbound_headers.items() if k.lower() not in _STRIPPED_INBOUND_HEADERS}

    if profile.kind == "oauth":
        headers["Authorization"] = f"Bearer {credential}"
    elif profile.auth_mode == "bearer":
        headers["Authorization"] = f"Bearer {credential}"
    else:
        headers["x-api-key"] = credential

    body = inbound_body
    if profile.kind == "oauth" and profile.account_uuid and _is_messages_path(path) and inbound_body:
        body = _rewrite_account_uuid(body, profile.account_uuid)
    if claude_effort and profile.kind in ("oauth", "api") and _is_messages_path(path) and body:
        body = _inject_output_effort(body, claude_effort)

    if body is not inbound_body:
        headers["Content-Length"] = str(len(body))

    base_url = resolve_base_url(profile)
    return UpstreamRequest(url=f"{base_url}{path}", method=method, headers=headers, body=body)


def _inject_output_effort(body: bytes, effort: str) -> bytes:
    """Set `output_config.effort` on a Messages request, MERGING into any
    existing output_config (never clobbering a structured-output config). The
    body is passed through untouched if it isn't a JSON object or can't be
    re-serialized — a reasoning-effort nicety must never fail a real request."""
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
    if not isinstance(parsed, dict):
        return body
    output_config = parsed.get("output_config")
    if not isinstance(output_config, dict):
        output_config = {}
    output_config["effort"] = effort
    parsed["output_config"] = output_config
    try:
        return json.dumps(parsed).encode("utf-8")
    except (TypeError, ValueError):
        return body


def _rewrite_account_uuid(body: bytes, account_uuid: str) -> bytes:
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Not JSON, so nothing to rewrite. Deliberately not an error: some
        # requests have no body worth touching.
        return body

    if not isinstance(parsed, dict):
        return body

    metadata = parsed.get("metadata")
    if not isinstance(metadata, dict):
        return body

    user_id_raw = metadata.get("user_id")
    if not isinstance(user_id_raw, str):
        # Shape doesn't match the expected one; pass through unrewritten
        # rather than guessing (see module docstring).
        return body

    try:
        user_id_obj = json.loads(user_id_raw)
    except json.JSONDecodeError:
        return body

    if not isinstance(user_id_obj, dict) or "account_uuid" not in user_id_obj:
        return body

    # Preserve every other field in the nested object (e.g. a session id);
    # only the account_uuid value changes.
    user_id_obj["account_uuid"] = account_uuid
    try:
        metadata["user_id"] = json.dumps(user_id_obj)
        return json.dumps(parsed).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AccountUuidRewriteError(f"Failed to re-serialize body after account_uuid rewrite: {exc}") from exc


def request_model(body: bytes) -> Optional[str]:
    """The body's top-level "model" field, or None if the body isn't a JSON
    object with a string one. gateway.py uses it to decide whether a
    fallback-to-default-model retry is possible for this request."""
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    model = parsed.get("model")
    return model if isinstance(model, str) else None


def rewrite_model(body: bytes, model: str) -> bytes:
    """Swaps the body's top-level "model" field for `model`.

    Backs an api-kind Profile's default_model fallback: a request for a
    model that Profile's key has no access to is retried once with the
    Profile's configured default, instead of surfacing a misleading "needs
    re-authentication" (see gateway.py's _maybe_retry_with_default_model)."""
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
    if not isinstance(parsed, dict) or "model" not in parsed:
        return body
    parsed["model"] = model
    return json.dumps(parsed).encode("utf-8")


def filter_response_headers(headers: dict[str, str]) -> dict[str, str]:
    lower = {k.lower(): v for k, v in headers.items()}
    return {k: lower[k] for k in RESPONSE_HEADER_ALLOWLIST if k in lower}
