import http.client
import json
import time

import pytest

import claude_unlimited.openai_bridge as bridge_module
import claude_unlimited.openai_login as login_module
from claude_unlimited.config import Profile
from claude_unlimited import codex_state
from claude_unlimited.openai_bridge import ConversationContext, OpenAIBridgeError, run
from claude_unlimited.openai_credential import StoredOpenAICredential, encode


class FakeHTTPResponse:
    def __init__(self, status: int, headers: dict, body: bytes):
        self.status = status
        self._headers = headers
        self._body = body
        self._pos = 0

    def getheaders(self):
        return list(self._headers.items())

    def read(self, n=None):
        if n is None:
            chunk = self._body[self._pos:]
            self._pos = len(self._body)
            return chunk
        chunk = self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


class IncompleteChunkedHTTPResponse(FakeHTTPResponse):
    """One chunked read whose bytes survived a missing HTTP terminator."""

    def read(self, n=None):
        if n is None:
            return super().read(n)
        partial = self._body[self._pos:]
        self._pos = len(self._body)
        raise http.client.IncompleteRead(partial)


class FakeHTTPSConnection:
    last_instance = None

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.requests = []
        self.closed = False
        FakeHTTPSConnection.last_instance = self

    def request(self, method, path, body=None, headers=None):
        self.requests.append({"method": method, "path": path, "body": body, "headers": headers})

    def getresponse(self):
        return self._response

    def close(self):
        self.closed = True


def _install_fake_connection(monkeypatch, response: FakeHTTPResponse):
    def factory(host, port, timeout=None):
        conn = FakeHTTPSConnection(host, port, timeout)
        conn._response = response
        return conn

    monkeypatch.setattr(bridge_module.http.client, "HTTPSConnection", factory)


def _subscription_profile(**overrides) -> Profile:
    defaults = dict(id="p1", name="Codex", kind="codex", auth_mode="chatgpt_subscription")
    defaults.update(overrides)
    return Profile(**defaults)


def _cred() -> str:
    return encode(StoredOpenAICredential(
        access_token="tok-a", refresh_token=None, account_id="acct-1", id_token=None))


def _sse_body(events: list[dict]) -> bytes:
    out = b""
    for event in events:
        out += f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
    return out


@pytest.fixture(autouse=True)
def reset_backoff_state():
    bridge_module._refresh_not_before.clear()
    bridge_module._MODEL_SUBSTITUTIONS.clear()
    bridge_module._EFFORT_SUBSTITUTIONS.clear()
    yield
    bridge_module._refresh_not_before.clear()
    bridge_module._MODEL_SUBSTITUTIONS.clear()
    bridge_module._EFFORT_SUBSTITUTIONS.clear()


def _install_fake_connections(monkeypatch, responses: list[FakeHTTPResponse]) -> list:
    """Hands out one queued response per connection, so a test can script a
    rejection followed by a success."""
    conns: list = []
    queue = list(responses)

    def factory(host, port, timeout=None):
        conn = FakeHTTPSConnection(host, port, timeout)
        conn._response = queue.pop(0)
        conns.append(conn)
        return conn

    monkeypatch.setattr(bridge_module.http.client, "HTTPSConnection", factory)
    return conns


def _model_error(message: str, status: int = 400) -> FakeHTTPResponse:
    return FakeHTTPResponse(status, {}, json.dumps(
        {"error": {"type": "invalid_request_error", "message": message}}).encode())


def _ok() -> FakeHTTPResponse:
    return FakeHTTPResponse(200, {}, _sse_body([{"type": "response.completed", "response": {"usage": {}}}]))


def _sent_model(conn) -> str:
    return json.loads(conn.requests[0]["body"])["model"]


def test_a_rejected_model_falls_back_to_the_next_one(monkeypatch):
    conns = _install_fake_connections(monkeypatch, [
        _model_error("The 'gpt-5.6-sol' model is not supported when using Codex with a ChatGPT account."),
        _ok(),
    ])
    body = json.dumps({"model": "claude-fable-5", "messages": [{"role": "user", "content": "hi"}]}).encode()

    result = run(_subscription_profile(), _cred(), body)
    list(result.body_chunks)

    assert result.status == 200
    assert _sent_model(conns[0]) == "gpt-5.6-sol"       # Fable's default target
    assert _sent_model(conns[1]) == "gpt-5.6-terra"     # first rung below it


def test_a_working_substitution_is_reused_on_the_next_request(monkeypatch):
    conns = _install_fake_connections(monkeypatch, [
        _model_error("The model `gpt-5.6-sol` does not exist."), _ok(), _ok(),
    ])
    body = json.dumps({"model": "claude-fable-5", "messages": [{"role": "user", "content": "hi"}]}).encode()

    list(run(_subscription_profile(), _cred(), body).body_chunks)
    list(run(_subscription_profile(), _cred(), body).body_chunks)

    # The third connection is the second request: it must skip the model
    # already known to be rejected (sol) rather than pay for that failure
    # again — going straight to the learned working substitute.
    assert len(conns) == 3
    assert _sent_model(conns[2]) == "gpt-5.6-terra"


def test_a_non_model_error_is_returned_without_retrying(monkeypatch):
    conns = _install_fake_connections(monkeypatch, [
        _model_error("Invalid value for 'temperature': must be <= 2."),
    ])
    body = json.dumps({"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]}).encode()

    result = run(_subscription_profile(), _cred(), body)
    chunks = b"".join(result.body_chunks)

    assert result.status == 400
    assert len(conns) == 1
    assert b"temperature" in chunks


def test_when_every_model_is_rejected_the_last_error_is_returned(monkeypatch):
    rejection = "model is not supported"
    # Opus maps to gpt-5.6-terra; its full walk is terra -> luna -> astra -> sol.
    conns = _install_fake_connections(monkeypatch, [_model_error(rejection) for _ in range(4)])
    body = json.dumps({"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]}).encode()

    result = run(_subscription_profile(), _cred(), body)
    chunks = b"".join(result.body_chunks)

    assert result.status == 400
    assert len(conns) == 4
    assert b"not supported" in chunks


def test_a_retired_profile_override_still_falls_back(monkeypatch):
    # A pinned model that gets retired must not take the Profile down with it.
    conns = _install_fake_connections(monkeypatch, [
        _model_error("The model `gpt-4o-legacy` has been deprecated."), _ok(),
    ])
    profile = _subscription_profile(codex_model="gpt-4o-legacy")
    body = json.dumps({"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]}).encode()

    result = run(profile, _cred(), body)
    list(result.body_chunks)

    assert result.status == 200
    assert _sent_model(conns[0]) == "gpt-4o-legacy"
    # An off-ladder override falls back to the whole ladder, top rung first.
    assert _sent_model(conns[1]) in ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")


def test_run_sends_the_real_confirmed_subscription_endpoint_and_headers(monkeypatch):
    events = [{"type": "response.completed", "response": {"usage": {}}}]
    _install_fake_connection(monkeypatch, FakeHTTPResponse(200, {"content-type": "text/event-stream"}, _sse_body(events)))

    profile = _subscription_profile()
    cred = encode(StoredOpenAICredential(access_token="tok-a", refresh_token=None, account_id="acct-1", id_token=None))
    body = json.dumps({"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]}).encode()

    result = run(profile, cred, body)
    list(result.body_chunks)  # drain to trigger the actual request

    conn = FakeHTTPSConnection.last_instance
    assert conn.host == "chatgpt.com"
    req = conn.requests[0]
    assert req["path"] == "/backend-api/codex/responses"
    assert req["headers"]["Authorization"] == "Bearer tok-a"
    assert req["headers"]["ChatGPT-Account-ID"] == "acct-1"
    assert req["headers"]["originator"] == "codex_cli_rs"
    assert req["headers"]["User-Agent"].startswith("codex_cli_rs/")
    assert "session-id" in req["headers"]
    assert "thread-id" in req["headers"]
    assert conn.closed is True


def test_run_uses_api_key_endpoint_and_no_account_header(monkeypatch):
    events = [{"type": "response.completed", "response": {"usage": {}}}]
    _install_fake_connection(monkeypatch, FakeHTTPResponse(200, {}, _sse_body(events)))

    profile = _subscription_profile(auth_mode="api_key")
    cred = encode(StoredOpenAICredential(access_token="sk-real-key", refresh_token=None, account_id=None, id_token=None))
    body = json.dumps({"messages": []}).encode()

    result = run(profile, cred, body)
    list(result.body_chunks)

    conn = FakeHTTPSConnection.last_instance
    assert conn.host == "api.openai.com"
    req = conn.requests[0]
    assert req["path"] == "/v1/responses"
    assert req["headers"]["Authorization"] == "Bearer sk-real-key"
    assert "ChatGPT-Account-ID" not in req["headers"]


def test_complete_sse_survives_a_missing_http_chunk_terminator(monkeypatch):
    events = [
        {"type": "response.created", "response": {"id": "r1", "model": "gpt-test"}},
        {"type": "response.output_item.added", "item": {"type": "message"}},
        {"type": "response.output_text.delta", "delta": "hello"},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 1, "output_tokens": 1}}},
    ]
    response = IncompleteChunkedHTTPResponse(
        200,
        {"content-type": "text/event-stream"},
        _sse_body(events),
    )
    _install_fake_connection(monkeypatch, response)

    result = run(
        _subscription_profile(),
        _cred(),
        json.dumps({"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]}).encode(),
    )
    chunks = b"".join(result.body_chunks)

    assert b"hello" in chunks
    assert b"event: message_stop" in chunks
    assert FakeHTTPSConnection.last_instance.closed is True


def test_truncated_sse_ends_the_stream_with_an_error_event(monkeypatch):
    events = [
        {"type": "response.created", "response": {"id": "r1", "model": "gpt-test"}},
        {"type": "response.output_item.added", "item": {"type": "message"}},
        {"type": "response.output_text.delta", "delta": "partial"},
    ]
    response = IncompleteChunkedHTTPResponse(
        200,
        {"content-type": "text/event-stream"},
        _sse_body(events),
    )
    _install_fake_connection(monkeypatch, response)

    result = run(
        _subscription_profile(),
        _cred(),
        json.dumps({"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]}).encode(),
    )

    # It must NOT simply raise: the status line and headers are already with
    # the client, so an exception here just stops the bytes, leaving the
    # client waiting on a message_stop that never comes (a subagent stuck on
    # "Waiting for task"). The turn has to fail visibly instead.
    chunks = b"".join(result.body_chunks)
    assert b"event: error" in chunks
    assert b"IncompleteRead" in chunks
    assert FakeHTTPSConnection.last_instance.closed is True


def test_run_accepts_a_bare_api_key_string_not_just_the_encoded_blob(monkeypatch):
    # An api_key-mode codex Profile added through the standard Add Profile flow
    # (create_profile without credential_already_encoded) stores a bare
    # credential string, not an encode()-produced blob. run() must decode it
    # without crashing.
    events = [{"type": "response.completed", "response": {"usage": {}}}]
    _install_fake_connection(monkeypatch, FakeHTTPResponse(200, {}, _sse_body(events)))

    profile = _subscription_profile(auth_mode="api_key")
    bare_credential = "sk-proj-abc123def456"

    result = run(profile, bare_credential, json.dumps({"messages": []}).encode())
    list(result.body_chunks)

    conn = FakeHTTPSConnection.last_instance
    assert conn.requests[0]["headers"]["Authorization"] == "Bearer sk-proj-abc123def456"


def test_run_uses_custom_base_url_override_for_api_key_mode(monkeypatch):
    events = [{"type": "response.completed", "response": {"usage": {}}}]
    _install_fake_connection(monkeypatch, FakeHTTPResponse(200, {}, _sse_body(events)))

    profile = _subscription_profile(auth_mode="api_key", base_url="https://my-gateway.example.com/v1")
    cred = encode(StoredOpenAICredential(access_token="sk-a", refresh_token=None, account_id=None, id_token=None))
    result = run(profile, cred, json.dumps({"messages": []}).encode())
    list(result.body_chunks)

    conn = FakeHTTPSConnection.last_instance
    assert conn.host == "my-gateway.example.com"
    assert conn.requests[0]["path"] == "/v1/responses"


def test_run_refuses_a_plain_http_base_url(monkeypatch):
    # A hand-edited config could carry one past profiles.py; it must not be
    # silently dialled with TLS on the wrong port, nor sent in the clear.
    _install_fake_connection(monkeypatch, FakeHTTPResponse(200, {}, b""))
    profile = _subscription_profile(auth_mode="api_key", base_url="http://127.0.0.1:8000/v1")
    cred = encode(StoredOpenAICredential(access_token="sk-a", refresh_token=None, account_id=None, id_token=None))
    with pytest.raises(OpenAIBridgeError, match="https://"):
        run(profile, cred, json.dumps({"messages": []}).encode())


def test_run_translates_a_full_sse_stream_to_anthropic_shaped_chunks(monkeypatch):
    events = [
        {"type": "response.created", "response": {"model": "gpt-5.6-terra"}},
        {"type": "response.output_item.added", "item": {"type": "message"}},
        {"type": "response.output_text.delta", "delta": "Hi"},
        {"type": "response.output_item.done", "item": {"type": "message"}},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 1, "output_tokens": 1}}},
    ]
    _install_fake_connection(monkeypatch, FakeHTTPResponse(200, {}, _sse_body(events)))

    profile = _subscription_profile()
    cred = encode(StoredOpenAICredential(access_token="tok-a", refresh_token=None, account_id="acct-1", id_token=None))
    result = run(profile, cred, json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode())

    combined = b"".join(result.body_chunks)
    assert b"message_start" in combined
    assert b'"text": "Hi"' in combined
    assert b"message_stop" in combined


def test_run_returns_error_status_without_attempting_sse_parse(monkeypatch):
    error_body = json.dumps({"error": {"message": "invalid token"}}).encode()
    _install_fake_connection(monkeypatch, FakeHTTPResponse(401, {}, error_body))

    profile = _subscription_profile()
    cred = encode(StoredOpenAICredential(access_token="bad-tok", refresh_token=None, account_id="acct-1", id_token=None))
    result = run(profile, cred, json.dumps({"messages": []}).encode())

    assert result.status == 401
    combined = b"".join(result.body_chunks)
    assert b"invalid token" in combined


def test_run_raises_bridge_error_on_connection_failure(monkeypatch):
    def factory(host, port, timeout=None):
        raise OSError("DNS resolution failed")

    monkeypatch.setattr(bridge_module.http.client, "HTTPSConnection", factory)

    profile = _subscription_profile()
    cred = encode(StoredOpenAICredential(access_token="tok-a", refresh_token=None, account_id="acct-1", id_token=None))
    with pytest.raises(OpenAIBridgeError):
        run(profile, cred, json.dumps({"messages": []}).encode())


def test_run_treats_a_non_json_credential_as_a_bare_token_not_an_error(monkeypatch):
    # openai_credential.decode() fails open, treating anything that isn't a
    # JSON blob as the access token itself. A plain string is the normal shape
    # for an api_key-mode credential and must not raise.
    events = [{"type": "response.completed", "response": {"usage": {}}}]
    _install_fake_connection(monkeypatch, FakeHTTPResponse(200, {}, _sse_body(events)))
    profile = _subscription_profile(auth_mode="api_key")
    result = run(profile, "not-json-at-all", json.dumps({"messages": []}).encode())
    list(result.body_chunks)
    assert FakeHTTPSConnection.last_instance.requests[0]["headers"]["Authorization"] == "Bearer not-json-at-all"


def test_run_raises_bridge_error_on_malformed_request_body(monkeypatch):
    events = [{"type": "response.completed", "response": {"usage": {}}}]
    _install_fake_connection(monkeypatch, FakeHTTPResponse(200, {}, _sse_body(events)))
    profile = _subscription_profile()
    cred = encode(StoredOpenAICredential(access_token="tok-a", refresh_token=None, account_id="acct-1", id_token=None))
    with pytest.raises(OpenAIBridgeError):
        run(profile, cred, b"not json")


class FakeSecretStore:
    def __init__(self):
        self.tokens: dict[str, str] = {}

    def set_token(self, profile_id, token):
        self.tokens[profile_id] = token

    def get_token(self, profile_id):
        return self.tokens[profile_id]


def _codex_test_env(monkeypatch, tmp_path):
    """Isolates the secret store and config file.

    _refresh_if_needed persists a successful refresh through
    profiles.update_credential_raw, which touches both.
    """
    import claude_unlimited.activity as activity
    import claude_unlimited.profiles as profile_repo

    store = FakeSecretStore()
    monkeypatch.setattr(profile_repo, "secret_store", store)
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(activity, "APP_DIR", tmp_path)
    monkeypatch.setattr(activity, "ACTIVITY_FILE", tmp_path / "activity.jsonl")
    return store, profile_repo


def test_refresh_if_needed_persists_a_decodable_credential_on_success(monkeypatch, tmp_path):
    from claude_unlimited.config import Pool, save_pool
    from claude_unlimited.openai_credential import decode

    store, profile_repo = _codex_test_env(monkeypatch, tmp_path)
    profile = _subscription_profile(account_uuid="acct-1")
    save_pool(Pool(profiles=[profile]))

    old_cred = StoredOpenAICredential(access_token=_expired_jwt(), refresh_token="ref-old",
                                       account_id="acct-1", id_token=None)

    def fake_refresh(refresh_token):
        assert refresh_token == "ref-old"
        return login_module.RefreshedTokens(access_token="tok-new-real", refresh_token="ref-new", id_token=None)

    monkeypatch.setattr(login_module, "refresh_access_token", fake_refresh)

    new_cred = bridge_module._refresh_if_needed(profile, old_cred)

    assert new_cred.access_token == "tok-new-real"
    # decode() must read back a StoredOpenAICredential, not a double-encoded
    # Anthropic-shaped blob.
    persisted = decode(store.get_token(profile.id))
    assert persisted.access_token == "tok-new-real"
    assert persisted.refresh_token == "ref-new"


def test_refresh_if_needed_backs_off_far_longer_after_a_429(monkeypatch, tmp_path):
    store, profile_repo = _codex_test_env(monkeypatch, tmp_path)
    profile = _subscription_profile(account_uuid="acct-1")
    from claude_unlimited.config import Pool, save_pool
    save_pool(Pool(profiles=[profile]))

    cred = StoredOpenAICredential(access_token=_expired_jwt(), refresh_token="ref-old",
                                   account_id="acct-1", id_token=None)

    refresh_calls = []

    def fake_refresh(refresh_token):
        refresh_calls.append(refresh_token)
        raise login_module.OpenAILoginError("rate limited", status_code=429)

    monkeypatch.setattr(login_module, "refresh_access_token", fake_refresh)
    fake_now = [1000.0]
    monkeypatch.setattr(bridge_module.time, "monotonic", lambda: fake_now[0])

    bridge_module._refresh_if_needed(profile, cred)
    assert len(refresh_calls) == 1

    fake_now[0] += bridge_module._REFRESH_CHECK_COOLDOWN_SECONDS + 30
    bridge_module._refresh_if_needed(profile, cred)
    assert len(refresh_calls) == 1  # still within the 429 backoff window — not retried yet

    fake_now[0] += bridge_module._RATE_LIMIT_BACKOFF_SECONDS
    bridge_module._refresh_if_needed(profile, cred)
    assert len(refresh_calls) == 2


def _expired_jwt() -> str:
    import base64
    import json as _json
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(_json.dumps({"exp": 1}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def test_api_key_profile_never_attempts_a_refresh(monkeypatch):
    # api_key credentials have no refresh_token or expiry, so the refresh path
    # must never be reached for one.
    def fail_refresh(*a, **kw):
        raise AssertionError("api_key profiles must never attempt a token refresh")

    monkeypatch.setattr(login_module, "refresh_access_token", fail_refresh)
    events = [{"type": "response.completed", "response": {"usage": {}}}]
    _install_fake_connection(monkeypatch, FakeHTTPResponse(200, {}, _sse_body(events)))

    profile = _subscription_profile(auth_mode="api_key")
    cred = encode(StoredOpenAICredential(access_token="sk-a", refresh_token=None, account_id=None, id_token=None))
    result = run(profile, cred, json.dumps({"messages": []}).encode())
    list(result.body_chunks)


def test_a_context_length_refusal_retries_once_without_replayed_reasoning(monkeypatch):
    """Replayed reasoning is input Claude Code never sent and cannot budget
    for, so it is the first thing to drop when the model says the prompt is
    too long — not the conversation."""
    codex_state.clear()
    codex_state.remember_reasoning("p1", "gpt-5.6-terra", ["call:toolu_1"],
                                   [{"type": "reasoning", "id": "rs_1", "encrypted_content": "x" * 64}])
    responses = [
        FakeHTTPResponse(400, {"content-type": "application/json"},
                         json.dumps({"error": {"message": "Your input exceeds the maximum context length"}}).encode()),
        FakeHTTPResponse(200, {"content-type": "text/event-stream"}, _sse_body([
            {"type": "response.created", "response": {"id": "r1", "model": "gpt-5.6-terra"}},
            {"type": "response.completed", "response": {"usage": {"input_tokens": 1, "output_tokens": 1}}},
        ])),
    ]
    conns = _install_fake_connections(monkeypatch, responses)

    body = json.dumps({
        "model": "claude-sonnet-5",
        "tools": [{"name": "x", "description": "d", "input_schema": {"type": "object"}}],
        "messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "x", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]},
        ],
    }).encode()
    result = run(_subscription_profile(), _cred(), body,
                 context=ConversationContext(claude_session_id="s1", agent_id="main"))
    b"".join(result.body_chunks)

    assert len(conns) == 2, "the refusal should have been retried"
    # The replayed blob itself, not the `include` flag (which names the same
    # field on every request).
    assert "x" * 64 in conns[0].requests[0]["body"].decode()
    assert "x" * 64 not in conns[1].requests[0]["body"].decode()
    assert result.status == 200


# ---------------------------------------------------------------------------
# Follow-ups from the PR #1 merge review.
#
# PR #1 taught the bridge to accept a chunked response that ends after a valid
# terminal event but before the zero-length HTTP terminator. That was covered
# by a single-read fixture; these cover the rest of the shape.
# ---------------------------------------------------------------------------

class MultiChunkIncompleteResponse(FakeHTTPResponse):
    """Delivers the body over SEVERAL reads and only then loses the HTTP
    terminator — which is what a real streamed response looks like. The
    single-read fixture could not catch a buffer that is reset per read, or a
    terminal event that arrives split across two reads."""

    def __init__(self, status, headers, body, chunk_size):
        super().__init__(status, headers, body)
        self._chunk_size = chunk_size

    def read(self, n=None):
        if n is None:
            return super().read(n)
        remaining = self._body[self._pos:]
        if len(remaining) > self._chunk_size:
            self._pos += self._chunk_size
            return remaining[:self._chunk_size]
        self._pos = len(self._body)
        raise http.client.IncompleteRead(remaining)


def _run_stream(monkeypatch, response):
    _install_fake_connection(monkeypatch, response)
    result = run(
        _subscription_profile(), _cred(),
        json.dumps({"model": "claude-sonnet-5",
                    "messages": [{"role": "user", "content": "hi"}]}).encode(),
    )
    return b"".join(result.body_chunks)


def test_a_truncated_close_is_accepted_when_the_body_arrived_over_many_reads(monkeypatch, capsys):
    """the terminal event may land in an EARLIER read than the one that
    raises, and the partial may be only the tail of the stream."""
    events = [
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "message", "id": "m1", "role": "assistant", "content": []}},
        {"type": "response.output_text.delta", "output_index": 0, "delta": "hello "},
        {"type": "response.output_text.delta", "output_index": 0, "delta": "world"},
        {"type": "response.completed", "response": {"usage": {}}},
    ]
    body = _sse_body(events)
    chunks = _run_stream(monkeypatch, MultiChunkIncompleteResponse(
        200, {"content-type": "text/event-stream"}, body, chunk_size=37))

    # A complete, successful turn — not an error event.
    assert b"event: error" not in chunks
    assert b"IncompleteRead" not in chunks
    assert b"message_stop" in chunks
    # Every delta survived being split across read boundaries.
    assert b"hello " in chunks and b"world" in chunks
    # The accepted close is no longer silent.
    assert "truncated" in capsys.readouterr().err.lower()


def test_a_multi_read_stream_without_a_terminal_event_still_fails(monkeypatch):
    """The other half: accepting a truncated close must stay conditional
    on the provider's own terminal event. A stream that simply died mid-answer
    has to surface, or the client silently believes a partial reply."""
    events = [
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "message", "id": "m1", "role": "assistant", "content": []}},
        {"type": "response.output_text.delta", "output_index": 0, "delta": "half an ans"},
    ]
    chunks = _run_stream(monkeypatch, MultiChunkIncompleteResponse(
        200, {"content-type": "text/event-stream"}, _sse_body(events), chunk_size=29))
    assert b"event: error" in chunks
    assert b"IncompleteRead" in chunks


def test_a_non_dict_json_frame_is_skipped_not_fatal(monkeypatch):
    """`data: "hi"` parses as a str, and `event.get(...)` on a str raises
    AttributeError — which the broad handler turns into an aborted stream. A
    junk frame must be skipped the same way malformed JSON already is."""
    body = (b'event: junk\ndata: "just a string"\n\n'
            b'event: junk2\ndata: [1, 2, 3]\n\n'
            b'event: junk3\ndata: null\n\n'
            + _sse_body([{"type": "response.completed", "response": {"usage": {}}}]))
    chunks = _run_stream(monkeypatch, FakeHTTPResponse(
        200, {"content-type": "text/event-stream"}, body))
    assert b"event: error" not in chunks
    assert b"message_stop" in chunks


def test_crlf_framed_sse_is_parsed(monkeypatch):
    """the SSE spec allows CRLF, and a proxy may rewrite line endings.
    Splitting only on "\\n\\n" finds no frame boundary at all in "\\r\\n\\r\\n",
    so the whole stream would be buffered and silently dropped."""
    events = [
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "message", "id": "m1", "role": "assistant", "content": []}},
        {"type": "response.output_text.delta", "output_index": 0, "delta": "crlf ok"},
        {"type": "response.completed", "response": {"usage": {}}},
    ]
    body = b"".join(
        f"event: {e['type']}\r\ndata: {json.dumps(e)}\r\n\r\n".encode() for e in events)
    chunks = _run_stream(monkeypatch, FakeHTTPResponse(
        200, {"content-type": "text/event-stream"}, body))
    assert b"crlf ok" in chunks
    assert b"message_stop" in chunks
    assert b"event: error" not in chunks


def test_events_after_the_terminal_event_are_ignored(monkeypatch):
    """anything fed to the translator after the terminal event is emitted
    AFTER message_stop, which is not a stream the client can read."""
    events = [
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "message", "id": "m1", "role": "assistant", "content": []}},
        {"type": "response.output_text.delta", "output_index": 0, "delta": "the answer"},
        {"type": "response.completed", "response": {"usage": {}}},
        {"type": "response.output_text.delta", "output_index": 0, "delta": "STRAY"},
    ]
    chunks = _run_stream(monkeypatch, FakeHTTPResponse(
        200, {"content-type": "text/event-stream"}, _sse_body(events)))
    assert b"the answer" in chunks
    assert b"STRAY" not in chunks
    # Count the EVENT line, not the substring: each frame carries the name
    # twice, once in `event:` and once inside the `data:` payload.
    assert chunks.count(b"event: message_stop") == 1


# ---------------------------------------------------------------------------
# Concurrent refresh of one Codex credential.
#
# The Anthropic side guards this with a lock and an in-progress set, and says
# why in gateway._try_refresh: the provider ROTATES the refresh token on use,
# so two threads refreshing the same account send the same token — one
# consumes it, the other replays a token that no longer exists. That earns
# 429s and can invalidate the grant outright, which is how an account that was
# refreshing fine ends up needing a manual re-auth.
#
# refresh_now() had the same shape WITHOUT the lock: read not_before, decide,
# then write it, which two threads can both pass.
# ---------------------------------------------------------------------------

def test_two_threads_refreshing_one_codex_account_send_the_token_once(monkeypatch):
    import threading as _threading
    from claude_unlimited import openai_credential

    started = _threading.Barrier(2, timeout=5)
    calls = []
    calls_lock = _threading.Lock()

    # The unguarded window — read not_before, decide, write it — is only a few
    # bytecodes wide, so two plain threads almost never interleave inside it
    # and the bug hides. Widen it deterministically: this is the interleaving
    # the lock has to make impossible, not an artificial one.
    class SlowReadDict(dict):
        def get(self, key, default=None):
            value = super().get(key, default)
            time.sleep(0.05)
            return value

    monkeypatch.setattr(bridge_module, "_refresh_not_before", SlowReadDict())

    def slow_refresh(refresh_token, timeout=30.0):
        with calls_lock:
            calls.append(refresh_token)
        # Hold the "network call" open so a second thread has every chance to
        # enter while this one is still in flight — the real window.
        time.sleep(0.2)
        return login_module.RefreshedTokens(
            access_token="new-access", refresh_token="rotated-token", id_token=None)

    monkeypatch.setattr(login_module, "refresh_access_token", slow_refresh)
    monkeypatch.setattr(bridge_module.openai_login, "refresh_access_token", slow_refresh)

    import claude_unlimited.profiles as profile_repo
    monkeypatch.setattr(profile_repo, "update_credential_raw", lambda *a, **k: None)

    cred = openai_credential.StoredOpenAICredential(
        access_token="old", refresh_token="single-use", account_id="acct", id_token=None)

    def worker():
        started.wait()
        bridge_module.refresh_now("p-race", cred)

    threads = [_threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    # The single-use token must have been spent exactly once.
    assert calls == ["single-use"], f"refresh token sent {len(calls)} times: {calls}"


def test_a_failed_refresh_releases_the_slot(monkeypatch):
    """A claimed slot that is never released would block every future refresh
    of that Profile for the life of the daemon — a worse failure than the race
    it prevents, and a silent one."""
    from claude_unlimited import openai_credential

    monkeypatch.setattr(bridge_module, "_refresh_not_before", {})
    monkeypatch.setattr(bridge_module, "_refresh_in_progress", set())

    def boom(refresh_token, timeout=30.0):
        raise login_module.OpenAILoginError("nope", status_code=400)

    monkeypatch.setattr(bridge_module.openai_login, "refresh_access_token", boom)
    cred = openai_credential.StoredOpenAICredential(
        access_token="old", refresh_token="rt", account_id="acct", id_token=None)

    assert bridge_module.refresh_now("p-fail", cred) is None
    assert "p-fail" not in bridge_module._refresh_in_progress


def test_an_unexpected_exception_also_releases_the_slot(monkeypatch):
    """Not just the handled OpenAILoginError — anything that escapes."""
    from claude_unlimited import openai_credential

    monkeypatch.setattr(bridge_module, "_refresh_not_before", {})
    monkeypatch.setattr(bridge_module, "_refresh_in_progress", set())

    def boom(refresh_token, timeout=30.0):
        raise RuntimeError("socket exploded")

    monkeypatch.setattr(bridge_module.openai_login, "refresh_access_token", boom)
    cred = openai_credential.StoredOpenAICredential(
        access_token="old", refresh_token="rt", account_id="acct", id_token=None)

    with pytest.raises(RuntimeError):
        bridge_module.refresh_now("p-boom", cred)
    assert "p-boom" not in bridge_module._refresh_in_progress


def test_a_successful_refresh_releases_the_slot(monkeypatch):
    from claude_unlimited import openai_credential
    import claude_unlimited.profiles as profile_repo

    monkeypatch.setattr(bridge_module, "_refresh_not_before", {})
    monkeypatch.setattr(bridge_module, "_refresh_in_progress", set())
    monkeypatch.setattr(profile_repo, "update_credential_raw", lambda *a, **k: None)
    monkeypatch.setattr(bridge_module.openai_login, "refresh_access_token",
                        lambda rt, timeout=30.0: login_module.RefreshedTokens(
                            access_token="new", refresh_token="rotated", id_token=None))

    cred = openai_credential.StoredOpenAICredential(
        access_token="old", refresh_token="rt", account_id="acct", id_token=None)
    result = bridge_module.refresh_now("p-ok", cred)
    assert result is not None and result.access_token == "new"
    # The rotated token is what gets stored, or the next refresh replays a
    # spent one.
    assert result.refresh_token == "rotated"
    assert "p-ok" not in bridge_module._refresh_in_progress



# ---- a reasoning effort the model does not take (issue #8) -------------------

_ASTRA_MINIMAL = ("Unsupported value: 'minimal' is not supported with the 'gpt-6-astra' model. "
                  "Supported values are: 'low', 'medium', 'high', 'xhigh', and 'max'.")


def _sent(conn):
    body = json.loads(conn.requests[0]["body"])
    return body["model"], body["reasoning"]["effort"]


def test_a_refused_effort_is_retried_on_the_same_model_with_the_nearest_supported_one(monkeypatch):
    err = json.dumps({"error": {"message": _ASTRA_MINIMAL, "param": "reasoning.effort"}})
    conns = _install_fake_connections(monkeypatch, [FakeHTTPResponse(400, {}, err.encode()), _ok(), _ok()])
    profile = _subscription_profile(codex_model="gpt-6-astra", codex_reasoning_effort="minimal")
    body = json.dumps({"model": "claude-sonnet-5", "messages": []}).encode()

    list(run(profile, _cred(), body).body_chunks)
    assert [_sent(c) for c in conns[:2]] == [("gpt-6-astra", "minimal"), ("gpt-6-astra", "low")]

    list(run(profile, _cred(), body).body_chunks)       # learned: no second refusal
    assert _sent(conns[2]) == ("gpt-6-astra", "low")


def test_a_retired_effort_saved_by_an_old_build_is_sent_as_its_equivalent():
    from claude_unlimited import openai_models
    assert "ultra" not in openai_models.VALID_REASONING_EFFORTS
    assert openai_models.upgrade_reasoning_effort("ultra") == "max"
    assert openai_models.upgrade_reasoning_effort("high") == "high"


@pytest.mark.parametrize("effort,supported,expected", [
    ("ultra", "'none', 'minimal', 'low', 'medium', 'high', 'xhigh', and 'max'", "max"),
    ("minimal", "'low', 'medium', 'high', 'xhigh', and 'max'", "low"),
    ("max", "'low', 'medium', and 'high'", "high"),
])
def test_effort_replacement_picks_the_nearest(effort, supported, expected):
    text = f"Invalid value: '{effort}'. Supported values are: {supported}. param reasoning.effort"
    assert bridge_module._effort_replacement(400, text, effort) == expected


def test_other_errors_are_not_effort_refusals():
    assert bridge_module._effort_replacement(400, "model gpt-x not found", "high") is None
    assert bridge_module._effort_replacement(429, _ASTRA_MINIMAL + " reasoning.effort", "minimal") is None
