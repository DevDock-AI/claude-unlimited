"""Sends one minimal live request through a specific Profile's stored
credential to confirm that account can serve traffic right now.

Deliberately bypasses the Router: it tests the one Profile the caller asked
about, not whichever Profile is currently eligible. Costs at most a handful
of tokens (max_tokens=1, a one-word prompt), and reports the measured
elapsed time and the real response.
"""

from __future__ import annotations

import json
import time
import uuid

from . import proxy, secret_store, upstream
from .config import load_pool

TEST_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_VERSION = "2023-06-01"
TEST_TIMEOUT_SECONDS = 20
# Minimum gap between two test requests for the SAME Profile. Without it, a
# double-click, a stuck retry, or a script calling "Test connection" in a
# loop fires an unthrottled request to Anthropic every time. Kept short
# (unlike the OAuth refresh path's much longer backoff) because this hits
# /v1/messages, a far less tightly limited endpoint; the goal is only to
# stop a tight loop.
MIN_TEST_INTERVAL_SECONDS = 5.0

_last_test_at: dict[str, float] = {}


class ConnectionTestError(Exception):
    """Raised for a local or network-level failure, where there is no
    response to report. An error response from Anthropic (401, 429, ...) is
    not this: that is an informative test result and gets returned."""

    def __init__(self, message: str, detail: str | None = None):
        super().__init__(message)
        self.detail = detail


class ConnectionTestThrottled(Exception):
    """Raised instead of making a request when this Profile was already
    tested within MIN_TEST_INTERVAL_SECONDS. Never silently swallowed:
    daemon.py surfaces it as a "wait a moment" response rather than a
    generic failure."""

    def __init__(self, retry_after_seconds: float):
        super().__init__(f"Tested too recently — try again in {retry_after_seconds:.0f}s.")
        self.retry_after_seconds = retry_after_seconds


def test_connection(profile_id: str) -> dict:
    now = time.monotonic()
    last = _last_test_at.get(profile_id)
    if last is not None and now - last < MIN_TEST_INTERVAL_SECONDS:
        raise ConnectionTestThrottled(MIN_TEST_INTERVAL_SECONDS - (now - last))
    _last_test_at[profile_id] = now

    pool = load_pool()
    profile = pool.get(profile_id)
    if profile is None:
        raise ConnectionTestError(f"No profile with id {profile_id!r}.")

    try:
        credential = secret_store.get_token(profile_id)
    except Exception as exc:  # noqa: BLE001 - surface any backend failure uniformly
        raise ConnectionTestError("Could not read the stored credential from Keychain.", str(exc)) from exc

    if profile.kind == "codex":
        return _test_codex_connection(profile, credential)

    body: dict = {
        "model": TEST_MODEL,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
    if profile.kind == "oauth" and profile.account_uuid:
        # Mirrors the nested shape Claude Code sends (see proxy.py's module
        # docstring) so this looks like ordinary traffic, not a probe.
        body["metadata"] = {"user_id": json.dumps({
            "account_uuid": profile.account_uuid,
            "session_id": str(uuid.uuid4()),
        })}

    inbound_body = json.dumps(body).encode("utf-8")
    inbound_headers = {"content-type": "application/json", "anthropic-version": ANTHROPIC_VERSION}

    req = proxy.build_upstream_request(profile, credential, "POST", "/v1/messages", inbound_headers, inbound_body)

    started = time.monotonic()
    try:
        resp = upstream.send(req, timeout=TEST_TIMEOUT_SECONDS)
        raw = b"".join(resp.body_chunks)
        resp.connection.close()
    except OSError as exc:
        raise ConnectionTestError("Could not reach Anthropic — network error.", str(exc)) from exc
    elapsed_ms = round((time.monotonic() - started) * 1000)

    if resp.status == 200:
        parsed_ok = {}
        try:
            parsed_ok = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        return {
            "ok": True, "status": resp.status, "elapsed_ms": elapsed_ms,
            "model": parsed_ok.get("model", TEST_MODEL),
        }

    message = None
    try:
        parsed_err = json.loads(raw)
        message = (parsed_err.get("error") or {}).get("message")
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        pass
    return {"ok": False, "status": resp.status, "elapsed_ms": elapsed_ms, "message": message}


def _test_codex_connection(profile, credential: str) -> dict:
    """A codex-kind Profile has no Anthropic-compatible upstream to build a
    proxy.UpstreamRequest for. openai_bridge.run() owns its own HTTP call
    and response translation, so this bypasses proxy.py and upstream.py."""
    from . import openai_bridge

    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",  # Anthropic-shaped on purpose; openai_bridge maps it
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }).encode("utf-8")

    started = time.monotonic()
    try:
        result = openai_bridge.run(profile, credential, body, timeout=TEST_TIMEOUT_SECONDS)
        raw = b"".join(result.body_chunks)
    except openai_bridge.OpenAIBridgeError as exc:
        raise ConnectionTestError("Could not reach OpenAI — network error.", str(exc)) from exc
    elapsed_ms = round((time.monotonic() - started) * 1000)

    if result.status == 200:
        return {"ok": True, "status": result.status, "elapsed_ms": elapsed_ms, "model": TEST_MODEL}

    message = None
    try:
        parsed_err = json.loads(raw)
        message = (parsed_err.get("error") or {}).get("message")
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        pass
    return {"ok": False, "status": result.status, "elapsed_ms": elapsed_ms, "message": message}
