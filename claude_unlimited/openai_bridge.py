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
import re
import sys
import threading
import time
from dataclasses import dataclass, replace
from typing import Iterator, Optional
from urllib.parse import urlsplit

from . import codex_state, openai_credential, openai_login
from . import openai_translate
from .openai_translate import user_turn_index
from .config import Profile
from .openai_models import fallback_models, map_model
from . import wire_formats

CHATGPT_BACKEND_URL = "https://chatgpt.com/backend-api/codex/responses"
API_KEY_BACKEND_URL = "https://api.openai.com/v1/responses"

# Only the version component of the User-Agent string. Nothing here depends
# on the `codex` binary being installed, so drift from a locally installed
# version is harmless.
CODEX_CLI_VERSION = "0.149.0"
ORIGINATOR = "codex_cli_rs"

# Socket timeout, which here is an IDLE timeout: it bounds the wait for the
# next chunk, not the length of the response. 120s was too tight for a
# reasoning model — a hard task can think for minutes before emitting its
# first event, and the read would time out mid-turn. The failure is no longer
# silent either (the stream ends in an `error` event, see _translated_chunks),
# so the cost of waiting longer is only that a truly dead connection takes
# this long to be called dead.
DEFAULT_TIMEOUT_SECONDS = 300
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
# Guards the check-and-set on _refresh_not_before, and records which Profiles
# are mid-refresh. See refresh_now() for why both are needed.
_refresh_lock = threading.Lock()
_refresh_in_progress: set = set()

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


# A model this backend has already rejected -> the one that worked instead.
# Learned at runtime so the cost of a retired model is one failed request per
# daemon lifetime, not one on every request. Deliberately not persisted: a
# model that comes back, or an account that gains access to one, should be
# retried after a restart rather than written off permanently.
_MODEL_SUBSTITUTIONS: dict[str, str] = {}
_MODEL_SUBSTITUTION_LOCK = threading.Lock()

# Matched against the error body, not the status code: the backend answers 400
# for a rejected model and 400 for a malformed request alike, and retrying the
# latter on another model would just burn a second request to fail identically.
_MODEL_REJECTION_PATTERN = re.compile(
    r"model[^.]{0,80}?(not supported|not found|does not exist|no longer|"
    r"deprecat|retired|unavailable|invalid)"
    r"|(unsupported|unknown|invalid|deprecated)[^.]{0,40}?model"
    r"|model_not_found",
    re.IGNORECASE,
)


# Efforts from least to most reasoning, for picking the nearest one a model
# does accept. Wider than VALID_REASONING_EFFORTS on purpose: it must place
# whatever a backend lists as supported, not just what the dashboard offers.
_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_SUPPORTED_EFFORTS_PATTERN = re.compile(r"supported values are:?([^.]*)", re.IGNORECASE)

# (host, model, refused effort) -> the effort that model accepts instead.
# Support differs per model (issue #8: gpt-6-astra refuses "minimal" that the
# generic list allows), so it is learned from the refusal, not hardcoded.
_EFFORT_SUBSTITUTIONS: dict[tuple, str] = {}


def _effort_replacement(status: int, body_text: str, effort: Optional[str]) -> Optional[str]:
    """The nearest effort the backend says it supports, when this error is a
    refused reasoning.effort; None for any other error. Nearest means the
    strongest one not above the refused value, else the weakest above it."""
    if status != 400 or not effort or "reasoning.effort" not in body_text:
        return None
    found = _SUPPORTED_EFFORTS_PATTERN.search(body_text)
    if not found:
        return None
    supported = [e for e in re.findall(r"'([a-z]+)'", found.group(1)) if e in _EFFORT_ORDER]
    if not supported or effort in supported:
        return None
    rank = _EFFORT_ORDER.index(effort) if effort in _EFFORT_ORDER else len(_EFFORT_ORDER)
    below = [e for e in supported if _EFFORT_ORDER.index(e) <= rank]
    if below:
        return max(below, key=_EFFORT_ORDER.index)
    return min(supported, key=_EFFORT_ORDER.index)


def _looks_like_model_rejection(status: int, body_text: str) -> bool:
    """Whether an error response means "not this model" rather than "not this
    request". Only these are worth retrying on a different model."""
    if status not in (400, 403, 404, 422):
        return False
    return bool(_MODEL_REJECTION_PATTERN.search(body_text))


# "the input is bigger than this model will take", in the several wordings the
# providers use for it.
_CONTEXT_OVERFLOW_PATTERN = re.compile(
    r"context[_ ]length|context window|maximum context|too many tokens|"
    r"prompt is too long|input is too long|reduce the length|exceeds? the (?:model|maximum)",
    re.IGNORECASE)


def _looks_like_context_overflow(body_text: str) -> bool:
    return bool(_CONTEXT_OVERFLOW_PATTERN.search(body_text))


def forget_model_substitutions() -> None:
    """Drops everything learned about rejected models.

    Called when the parity mapping changes: a model rejected once would
    otherwise be substituted for the rest of this daemon's life, so a user who
    deliberately selects that model in Settings would silently keep getting the
    fallback with no feedback — the exact "I set it and nothing happened"
    failure the setting exists to avoid."""
    with _MODEL_SUBSTITUTION_LOCK:
        _MODEL_SUBSTITUTIONS.clear()
        _EFFORT_SUBSTITUTIONS.clear()


def _substitute_model(model: str) -> str:
    with _MODEL_SUBSTITUTION_LOCK:
        seen = set()
        while model in _MODEL_SUBSTITUTIONS and model not in seen:
            seen.add(model)
            model = _MODEL_SUBSTITUTIONS[model]
        return model


def _remember_substitution(rejected: str, accepted: str) -> None:
    if rejected == accepted:
        return
    with _MODEL_SUBSTITUTION_LOCK:
        _MODEL_SUBSTITUTIONS[rejected] = accepted


def _build_headers(cred: openai_credential.StoredOpenAICredential, *, is_subscription: bool,
                   ids: Optional["codex_state.ConversationIds"] = None,
                   turn_state: Optional[str] = None) -> dict[str, str]:
    """Codex CLI request headers. `ids` is the conversation's stable identity
    (codex_state): ChatGPT derives cache affinity from `session-id`, so a
    per-request random one — what this used to send for every request —
    gave the backend no way to keep a conversation next to its cached prefix.
    Without ids (not identifiable Claude Code traffic) each request still
    gets fresh ones."""
    session_id = ids.session_id if ids else _uuid7()
    thread_id = ids.thread_id if ids else _uuid7()
    headers = {
        "Authorization": f"Bearer {cred.access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "originator": ORIGINATOR,
        "User-Agent": _codex_user_agent(),
        "session-id": session_id,
        "thread-id": thread_id,
        "x-client-request-id": _uuid7(),
    }
    if ids and ids.is_subagent:
        # What the Codex CLI sends for a spawned agent.
        headers["x-openai-subagent"] = "collab_spawn"
        if ids.parent_thread_id:
            headers["x-codex-parent-thread-id"] = ids.parent_thread_id
    if turn_state:
        headers["x-codex-turn-state"] = turn_state
    if is_subscription and cred.account_id:
        headers["ChatGPT-Account-ID"] = cred.account_id
    return headers


@dataclass(frozen=True)
class ConversationContext:
    """Which Claude Code conversation a request belongs to (gateway supplies
    it from project_attribution). None fields mean "not identifiable"."""
    claude_session_id: Optional[str] = None
    agent_id: Optional[str] = None
    # Only for a nested subagent (Claude Code omits it for a direct one).
    parent_agent_id: Optional[str] = None


def _refresh_if_needed(profile: Profile, cred: openai_credential.StoredOpenAICredential) -> openai_credential.StoredOpenAICredential:
    """Proactive refresh, mirroring gateway.py's _maybe_refresh_credential.

    Applies only to chatgpt_subscription auth_mode; an api_key credential
    has no refresh_token or expiry to check. A failure is a silent no-op:
    the request goes out with whatever credential is on hand, and a 401 from
    the request itself is what drives AUTH_INVALID."""
    if not cred.refresh_token or not openai_credential.is_expiring_soon(cred.access_token):
        return cred
    return refresh_now(profile.id, cred) or cred


def refresh_attempt_due(profile_id: str) -> bool:
    """Whether refresh_now() could do anything for this Profile right now.

    The counterpart of gateway._refresh_attempt_due, and for the same reason:
    the sync-driven recovery path would otherwise read this Profile's
    credential out of the keychain — a subprocess on macOS — on every poll
    tick just to find out it is still inside the backoff window."""
    not_before = _refresh_not_before.get(profile_id)
    return not_before is None or time.monotonic() >= not_before


def refresh_now(profile_id: str,
                cred: openai_credential.StoredOpenAICredential
                ) -> Optional[openai_credential.StoredOpenAICredential]:
    """The ONE place that calls openai_login.refresh_access_token.

    Shared by _refresh_if_needed (the per-request path, for whichever Profile
    was just chosen) and gateway.py's sync-driven recovery (which covers idle
    and AUTH_INVALID Profiles a request never reaches) specifically so the two
    share ONE per-Profile backoff clock rather than two independent ones —
    the same reasoning as gateway._try_refresh on the Anthropic side. With
    separate clocks, one path takes a 429 and the other retries the same
    account moments later, unaware, and each attempt is itself another strike
    against the limiter.

    Returns None both when still inside the backoff window and when the
    refresh genuinely failed; the caller keeps using whatever it had.
    """
    now = time.monotonic()
    # Claim the slot atomically, exactly as gateway._try_refresh does on the
    # Anthropic side and for the same reason: OpenAI ROTATES the refresh token
    # on use (see `refreshed.refresh_token or cred.refresh_token` below), so
    # two threads refreshing this Profile at once send the SAME single-use
    # token. One consumes it; the other replays a token that no longer exists,
    # which earns a 429 and can invalidate the grant outright — an account
    # that was refreshing fine suddenly needing a manual re-auth.
    #
    # Reading not_before, deciding, and then writing it is a check-then-act
    # that both threads can pass. The daemon serves requests on threads, so
    # two concurrent turns on one Codex account reach here together.
    with _refresh_lock:
        if profile_id in _refresh_in_progress:
            return None   # another thread is already refreshing this one
        not_before = _refresh_not_before.get(profile_id)
        if not_before is not None and now < not_before:
            return None  # still inside the backoff window from a recent failed attempt
        _refresh_not_before[profile_id] = now + _REFRESH_CHECK_COOLDOWN_SECONDS
        _refresh_in_progress.add(profile_id)

    try:
        refreshed = openai_login.refresh_access_token(cred.refresh_token)
    except openai_login.OpenAILoginError as exc:
        if exc.status_code == 429:
            # A rate limit, not a dead refresh_token: back off far longer
            # than the normal cooldown.
            _refresh_not_before[profile_id] = now + _RATE_LIMIT_BACKOFF_SECONDS
        return None
    finally:
        # Must run on EVERY exit, including the success path below and any
        # unexpected exception — a slot left claimed would block this
        # Profile's refreshes for the life of the process.
        with _refresh_lock:
            _refresh_in_progress.discard(profile_id)
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
        profile_repo.update_credential_raw(profile_id, openai_credential.encode(new_cred))
    except Exception:
        pass  # the refreshed credential still serves this request even if persisting failed
    return new_cred


def run(profile: Profile, stored_credential: str, body: bytes,
        timeout: float = DEFAULT_TIMEOUT_SECONDS, parity: Optional[dict] = None,
        context: Optional[ConversationContext] = None,
        replay_reasoning: bool = True) -> OpenAIBridgeResult:
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
        parity=parity,
    )
    fmt = wire_formats.get(getattr(profile, "wire_format", None))
    if is_subscription:
        url = CHATGPT_BACKEND_URL
    else:
        base = (profile.base_url or "https://api.openai.com/v1").rstrip("/")
        url = f"{base}{fmt.endpoint_path}"
    parts = urlsplit(url)
    if parts.scheme != "https":
        # Codex profiles are https-only (plain http is an API-profile feature
        # for local servers); profiles.py refuses this on save, and this
        # catches a config edited by hand.
        raise OpenAIBridgeError(f"Refusing to send a Codex request over {parts.scheme or 'no scheme'}: "
                                "the Base URL must start with https://.")

    # Start from whatever this backend last accepted in place of the mapped
    # model, then walk the ladder if that is rejected too. A Profile override
    # is honoured as the first candidate but is not the only one: a pinned
    # model that gets retired should degrade to a working one rather than take
    # the Profile down with it.
    # Gated with the ladder, not before it: the memo is a process-global dict
    # keyed by model name alone, so a substitution learned against the Codex
    # backend would otherwise be applied to a same-named model on a completely
    # different server.
    first_choice = _substitute_model(target.model) if fmt.uses_model_ladder else target.model
    candidates = [first_choice]
    if fmt.uses_model_ladder:
        # Scoped to formats served by the Codex backend, whose lineup the
        # ladder describes. A local server serves arbitrary model names, so
        # substituting a Codex model for a rejected one would be nonsense.
        candidates += [m for m in fallback_models(first_choice) if m not in candidates]

    # Only agent turns (requests offering tools) belong to the conversation.
    # Claude Code's side calls — the auto-mode safety classifier, titles —
    # carry the same session id but a different prompt, and must not take the
    # conversation's sticky turn token or cache key.
    is_agent_turn = bool(anthropic_body.get("tools"))
    ids = (codex_state.conversation_ids(context.claude_session_id, context.agent_id,
                                        context.parent_agent_id)
           if context and is_agent_turn else None)
    if ids is None:
        # No conversation to key on (a script calling the proxy directly, or a
        # Claude Code side call). Requests sharing a big system prompt are the
        # same cached prefix upstream, so key on the prompt itself rather than
        # sending a fresh random session-id — which is what left 400k+ tokens
        # uncached on a fan-out of small, same-system requests.
        ids = codex_state.identity_from_prompt(
            target.model, openai_translate._extract_system_text(anthropic_body.get("system")))
    turn = user_turn_index(anthropic_body)
    turn_token = (codex_state.turn_state(context.claude_session_id, context.agent_id, turn)
                  if ids and context else None)

    resp = None
    conn = None
    served_model = None
    attempts = [(m, replay_reasoning) for m in candidates]
    effort_fixed: set = set()
    index = 0
    while index < len(attempts):
        model, replay_reasoning = attempts[index]
        index += 1
        with _MODEL_SUBSTITUTION_LOCK:
            effort = _EFFORT_SUBSTITUTIONS.get((parts.hostname, model, target.reasoning_effort),
                                               target.reasoning_effort)
        attempt_target = replace(target, model=model, reasoning_effort=effort)
        lookup = ((lambda anchors, m=model: codex_state.reasoning_for(profile.id, m, anchors))
                  if replay_reasoning else None)
        payload = json.dumps(fmt.to_provider(
            anthropic_body, attempt_target, reasoning_lookup=lookup,
            prompt_cache_key=ids.prompt_cache_key if ids else None)).encode("utf-8")
        headers = _build_headers(cred, is_subscription=is_subscription, ids=ids, turn_state=turn_token)
        headers["Content-Length"] = str(len(payload))

        try:
            conn = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=timeout)
            conn.request("POST", parts.path or "/", body=payload, headers=headers)
            resp = conn.getresponse()
        except OSError as exc:
            raise OpenAIBridgeError(f"Could not reach {parts.hostname}: {exc}") from exc

        if resp.status < 400:
            _remember_substitution(target.model, model)
            served_model = model
            break

        # An error response is never SSE, so read it whole (error bodies are
        # small) and hand it back as one Anthropic-shaped error chunk. The
        # SSE translator only understands `event: ... / data: ...` frames.
        raw = resp.read()
        response_headers = dict(resp.getheaders())
        status = resp.status
        conn.close()
        text = raw.decode("utf-8", errors="replace")

        # A reasoning effort this model does not take (issue #8). Checked
        # first: the wording ("… is not supported with the 'x' model") also
        # reads as a model rejection, and walking to another model would
        # change the model to fix the effort. Retried once on the same model
        # with the nearest supported effort, and remembered for next time.
        replacement = _effort_replacement(status, text, effort)
        if replacement is not None and model not in effort_fixed:
            effort_fixed.add(model)
            with _MODEL_SUBSTITUTION_LOCK:
                _EFFORT_SUBSTITUTIONS[(parts.hostname, model, target.reasoning_effort)] = replacement
            attempts.insert(index, (model, replay_reasoning))
            continue

        # Replayed reasoning the backend will not accept (expired, or from a
        # rotated key) must never cost the request: retry once without it.
        #
        # A context-length refusal counts. Replaying reasoning ADDS input
        # Claude Code did not send and cannot see, so its own context budget
        # cannot account for it — and it is the one part of the request we are
        # free to drop, because it is an optimization, not the conversation.
        if (replay_reasoning and status == 400
                and ("encrypted" in text.lower() or "reasoning" in text.lower()
                     or _looks_like_context_overflow(text))):
            attempts.insert(index, (model, False))
            continue

        if model != candidates[-1] and _looks_like_model_rejection(status, text):
            continue

        def _error_chunks(raw: bytes = raw) -> Iterator[bytes]:
            message = raw.decode("utf-8", errors="replace")[:2000]
            yield json.dumps({"type": "error", "error": {"type": "api_error", "message": message}}).encode("utf-8")

        return OpenAIBridgeResult(status=status, headers=response_headers, body_chunks=_error_chunks())

    assert resp is not None and conn is not None  # candidates is never empty
    response_headers = dict(resp.getheaders())
    if ids and context:
        lowered = {k.lower(): v for k, v in response_headers.items()}
        codex_state.remember_turn_state(context.claude_session_id, context.agent_id, turn,
                                        lowered.get("x-codex-turn-state", ""))

    def _translated_chunks() -> Iterator[bytes]:
        translator = fmt.response_stream()
        buffer = b""
        terminal_event_seen = False
        try:
            while True:
                incomplete = None
                try:
                    chunk = resp.read(CHUNK_READ_SIZE)
                except http.client.IncompleteRead as exc:
                    # ChatGPT's backend sometimes closes a chunked response
                    # after sending a complete SSE stream but before writing
                    # the zero-length HTTP terminator. http.client preserves
                    # those final bytes on the exception. Translate them, and
                    # accept the close only when the provider's own terminal
                    # event proves the response was complete. A genuinely
                    # truncated SSE stream must still fail visibly.
                    incomplete = exc
                    chunk = exc.partial
                if not chunk:
                    if incomplete is None:
                        break
                else:
                    buffer += chunk
                # The SSE spec allows CRLF, and a proxy may rewrite line
                # endings even when the origin does not use them. "\r\n\r\n"
                # contains no "\n\n", so without this the frame boundary is
                # never found and the entire stream sits in the buffer and is
                # silently dropped. Safe on the payload: a raw CR cannot appear
                # inside a JSON string, only as the escape \r.
                if b"\r\n" in buffer:
                    buffer = buffer.replace(b"\r\n", b"\n")
                while b"\n\n" in buffer:
                    frame, _, buffer = buffer.partition(b"\n\n")
                    event = _parse_sse_frame(frame)
                    # Nothing may follow the provider's terminal event: the
                    # translator has already emitted message_stop, so a later
                    # frame would append content AFTER the end of the message
                    # and the client cannot read that.
                    if event is not None and not terminal_event_seen:
                        if event.get("type") in {
                            "response.completed",
                            "response.failed",
                            "response.incomplete",
                        }:
                            terminal_event_seen = True
                        yield from translator.feed(event)
                if incomplete is not None:
                    if terminal_event_seen:
                        # Not silent: this is us deciding a protocol-level
                        # anomaly was benign, and if the provider's behaviour
                        # changes the daemon log is the only place that would
                        # show it. Not an Activity entry — the turn SUCCEEDED,
                        # and the only category that fits there is "error".
                        print("[codex] accepted a truncated chunked close: the terminal "
                              f"event arrived first ({len(incomplete.partial)} trailing bytes)",
                              file=sys.stderr, flush=True)
                        break
                    raise incomplete
        except Exception as exc:  # noqa: BLE001 - see abort() on why this cannot propagate
            # The status line and headers left long ago, so there is no way to
            # turn this into an HTTP error: the only thing the client can still
            # be told is an SSE `error` event. Without one it waits for a
            # `message_stop` that will never come.
            yield from translator.abort(f"upstream stream failed: {type(exc).__name__}: {exc}"[:500])
        finally:
            conn.close()
            # Only a finished response is replayed from: a partial turn's
            # reasoning would precede output Claude Code never kept.
            if terminal_event_seen and served_model and getattr(translator, "reasoning_items", None):
                codex_state.remember_reasoning(profile.id, served_model, translator.anchors(),
                                               translator.reasoning_items)

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
        parsed = json.loads(data_line)
    except json.JSONDecodeError:
        return None
    # `data: "hi"`, `data: [1,2]` and `data: null` are all valid JSON and none
    # of them is an event. Returning them would hand a str/list/None to
    # `event.get("type")`, an AttributeError that the caller's broad handler
    # turns into an aborted stream — a junk frame must be skipped, exactly
    # like malformed JSON already is.
    return parsed if isinstance(parsed, dict) else None
