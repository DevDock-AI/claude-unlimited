import json

import pytest

import claude_unlimited.openai_bridge as bridge_module
import claude_unlimited.openai_login as login_module
from claude_unlimited.config import Profile
from claude_unlimited.openai_bridge import OpenAIBridgeError, run
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
    yield
    bridge_module._refresh_not_before.clear()
    bridge_module._MODEL_SUBSTITUTIONS.clear()


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
    body = json.dumps({"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]}).encode()

    result = run(_subscription_profile(), _cred(), body)
    list(result.body_chunks)

    assert result.status == 200
    assert _sent_model(conns[0]) == "gpt-5.6-sol"
    assert _sent_model(conns[1]) == "gpt-5.6-terra"


def test_a_working_substitution_is_reused_on_the_next_request(monkeypatch):
    conns = _install_fake_connections(monkeypatch, [
        _model_error("The model `gpt-5.6-sol` does not exist."), _ok(), _ok(),
    ])
    body = json.dumps({"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]}).encode()

    list(run(_subscription_profile(), _cred(), body).body_chunks)
    list(run(_subscription_profile(), _cred(), body).body_chunks)

    # The third connection is the second request: it must skip the model
    # already known to be rejected rather than pay for that failure again.
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
    conns = _install_fake_connections(monkeypatch, [_model_error(rejection) for _ in range(3)])
    body = json.dumps({"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]}).encode()

    result = run(_subscription_profile(), _cred(), body)
    chunks = b"".join(result.body_chunks)

    assert result.status == 400
    assert len(conns) == 3
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
    assert _sent_model(conns[1]) in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")


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
