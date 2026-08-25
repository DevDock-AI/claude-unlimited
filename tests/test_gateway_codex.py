import json

import pytest

import claude_unlimited.gateway as gateway_module
from claude_unlimited.config import Pool, Profile, save_pool
from claude_unlimited.gateway import Gateway
from claude_unlimited.openai_bridge import OpenAIBridgeError, OpenAIBridgeResult


class FakeSecretStore:
    def __init__(self, tokens):
        self.tokens = tokens

    def get_token(self, profile_id):
        return self.tokens[profile_id]


@pytest.fixture
def pool_env(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    monkeypatch.setattr("claude_unlimited.gateway.usage_history.USAGE_HISTORY_FILE", tmp_path / "usage_history.jsonl")
    monkeypatch.setattr(gateway_module, "secret_store", FakeSecretStore({"c": "tok-c", "a": "tok-a"}))
    return tmp_path


def _codex_profile(**overrides) -> Profile:
    defaults = dict(id="c", name="C", kind="codex", auth_mode="chatgpt_subscription",
                     priority=1, automatic=True, enabled=True)
    defaults.update(overrides)
    return Profile(**defaults)


def test_successful_codex_request_returns_200_and_sets_current_profile(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex_profile()]))

    def fake_run(profile, credential, body, timeout=120):
        assert credential == "tok-c"
        return OpenAIBridgeResult(status=200, headers={"content-type": "text/event-stream"},
                                   body_chunks=iter([b"event: message_stop\ndata: {}\n\n"]))

    monkeypatch.setattr(gateway_module.openai_bridge, "run", fake_run)

    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("must not use the Anthropic transport")))
    result = gw.handle("POST", "/v1/messages", {}, b"{}")

    assert result.status == 200
    assert result.profile_id == "c"
    assert gw._current_profile_id == "c"


def test_success_response_never_leaks_raw_openai_headers_to_the_client(pool_env, monkeypatch):
    # OpenAI's raw response headers (Cloudflare ray/cookies, x-codex-* quota
    # telemetry) must be replaced by a clean Anthropic-shaped header set, or
    # the client can see it is not talking to Anthropic.
    save_pool(Pool(profiles=[_codex_profile()]))

    def fake_run(profile, credential, body, timeout=120):
        return OpenAIBridgeResult(
            status=200,
            headers={"Server": "cloudflare", "CF-RAY": "abcd1234", "x-codex-plan-type": "plus",
                     "Set-Cookie": "__oailb=secret; HttpOnly"},
            body_chunks=iter([b"event: message_stop\ndata: {}\n\n"]),
        )

    monkeypatch.setattr(gateway_module.openai_bridge, "run", fake_run)
    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("no transport expected")))
    result = gw.handle("POST", "/v1/messages", {}, b"{}")

    assert result.headers == {"content-type": "text/event-stream; charset=utf-8"}
    assert "Server" not in result.headers
    assert "CF-RAY" not in result.headers
    assert "Set-Cookie" not in result.headers
    assert "x-codex-plan-type" not in result.headers


def test_error_response_headers_are_json_not_event_stream(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex_profile()]))

    def fake_run(profile, credential, body, timeout=120):
        return OpenAIBridgeResult(status=401, headers={"Server": "cloudflare"},
                                   body_chunks=iter([b'{"error":"bad token"}']))

    monkeypatch.setattr(gateway_module.openai_bridge, "run", fake_run)
    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("no transport expected")))
    result = gw.handle("POST", "/v1/messages", {}, b"{}")

    assert result.headers == {"content-type": "application/json"}


def test_count_tokens_path_never_calls_openai_bridge(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex_profile()]))

    def fail_run(*a, **kw):
        raise AssertionError("count_tokens must be answered locally, not bridged to OpenAI")

    monkeypatch.setattr(gateway_module.openai_bridge, "run", fail_run)

    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("no transport expected")))
    result = gw.handle("POST", "/v1/messages/count_tokens", {}, json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode())

    assert result.status == 200
    body = b"".join(result.body_chunks)
    assert b"input_tokens" in body


def test_unsupported_path_returns_404_not_a_mistranslated_bridge_call(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex_profile()]))

    def fail_run(*a, **kw):
        raise AssertionError("must not bridge an unsupported path")

    monkeypatch.setattr(gateway_module.openai_bridge, "run", fail_run)

    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("no transport expected")))
    result = gw.handle("POST", "/v1/complete", {}, b"")
    assert result.status == 404


def test_codex_profile_answers_models_listing_with_its_own_mapped_models(pool_env, monkeypatch):
    """A codex Profile has no Anthropic backend to relay GET /v1/models to, so
    it must answer locally. On a 404 the client falls back to its built-in
    Claude list and the picker offers models the Profile cannot serve."""
    import json

    save_pool(Pool(profiles=[_codex_profile()]))
    monkeypatch.setattr(gateway_module.openai_bridge, "run",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not bridge a models call")))

    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("no transport expected")))
    result = gw.handle("GET", "/v1/models", {}, b"")

    assert result.status == 200
    payload = json.loads(b"".join(result.body_chunks))
    ids = [m["id"] for m in payload["data"]]
    # Anthropic-shaped ids on purpose: Claude Code sends the picked id back
    # in /v1/messages and openai_models.map_model is keyed on exactly these.
    assert "claude-sonnet-5" in ids
    # ...but every display name must name the backing model.
    assert all("GPT" in m["display_name"] for m in payload["data"])


def test_codex_models_retrieve_form_returns_one_model(pool_env):
    import json

    save_pool(Pool(profiles=[_codex_profile()]))
    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("no transport expected")))

    result = gw.handle("GET", "/v1/models/claude-sonnet-5", {}, b"")
    assert result.status == 200
    assert json.loads(b"".join(result.body_chunks))["id"] == "claude-sonnet-5"

    missing = gw.handle("GET", "/v1/models/gpt-does-not-exist", {}, b"")
    assert missing.status == 404


def test_oauth_profile_models_listing_is_relayed_upstream_not_answered_locally(pool_env):
    """The registry only overrides kinds whose backend isn't Anthropic-shaped
    (connectors.models_listing returns None for oauth/api), so a Claude Profile
    keeps serving Anthropic's own model list."""
    from claude_unlimited.upstream import UpstreamResponse

    class FakeConnection:
        def close(self):
            pass

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1,
                                      automatic=True, enabled=True, account_uuid="u")]))
    seen = {}

    def transport(req):
        seen["path"] = req.url
        return UpstreamResponse(status=200, headers={}, body_chunks=iter([b'{"data":[]}']),
                                 connection=FakeConnection())

    gw = Gateway(transport=transport)
    result = gw.handle("GET", "/v1/models", {}, b"")
    assert result.status == 200
    assert seen["path"].endswith("/v1/models"), seen


def test_auth_invalid_codex_profile_rotates_to_next_eligible_profile(pool_env, monkeypatch):
    from claude_unlimited.upstream import UpstreamResponse

    save_pool(Pool(profiles=[
        _codex_profile(priority=1),
        Profile(id="a", name="A", kind="oauth", priority=2, automatic=True, enabled=True),
    ]))

    def fake_run(profile, credential, body, timeout=120):
        return OpenAIBridgeResult(status=401, headers={}, body_chunks=iter([b'{"error":"bad token"}']))

    monkeypatch.setattr(gateway_module.openai_bridge, "run", fake_run)

    class FakeConnection:
        def close(self):
            pass

    def transport(req):
        return UpstreamResponse(status=200, headers={"anthropic-ratelimit-unified-5h-utilization": "0.1",
                                                       "anthropic-ratelimit-unified-5h-reset": "1787191800"},
                                 body_chunks=iter([b"ok"]), connection=FakeConnection())

    gw = Gateway(transport=transport)
    # A 401 is forwarded to the client unchanged on the request that discovers
    # it, matching the Anthropic-side behavior: no rotation within the same
    # request.
    first = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert first.status == 401
    assert first.profile_id == "c"
    assert gw.runtime_snapshot()["c"].state == gateway_module.ProfileState.AUTH_INVALID

    # The next request excludes the now-AUTH_INVALID codex profile and rotates
    # to "a".
    second = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert second.status == 200
    assert second.profile_id == "a"


def test_openai_bridge_connection_failure_rotates_to_next_profile(pool_env, monkeypatch):
    from claude_unlimited.upstream import UpstreamResponse

    save_pool(Pool(profiles=[
        _codex_profile(priority=1),
        Profile(id="a", name="A", kind="oauth", priority=2, automatic=True, enabled=True),
    ]))

    def fake_run(profile, credential, body, timeout=120):
        raise OpenAIBridgeError("could not reach chatgpt.com")

    monkeypatch.setattr(gateway_module.openai_bridge, "run", fake_run)

    class FakeConnection:
        def close(self):
            pass

    def transport(req):
        return UpstreamResponse(status=200, headers={"anthropic-ratelimit-unified-5h-utilization": "0.1",
                                                       "anthropic-ratelimit-unified-5h-reset": "1787191800"},
                                 body_chunks=iter([b"ok"]), connection=FakeConnection())

    gw = Gateway(transport=transport)
    result = gw.handle("POST", "/v1/messages", {}, b"{}")

    assert result.status == 200
    assert result.profile_id == "a"


def test_pinned_codex_profile_that_fails_returns_error_not_rotate(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex_profile()]))

    def fake_run(profile, credential, body, timeout=120):
        raise OpenAIBridgeError("network down")

    monkeypatch.setattr(gateway_module.openai_bridge, "run", fake_run)

    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("no transport expected")))
    result = gw.handle("POST", "/v1/messages", {}, b"{}", forced_profile_id="c")

    assert result.status == 503


def _sse_ok_bridge(monkeypatch):
    """A bridge result shaped like a real translated streaming answer."""
    sse = (b'event: message_start\ndata: {"type":"message_start","message":{"id":"m1","model":"gpt-5.6-sol"}}\n\n'
           b'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
           b'"content_block":{"type":"text","text":""}}\n\n'
           b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
           b'"delta":{"type":"text_delta","text":"SAFE"}}\n\n'
           b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
           b'"usage":{"input_tokens":5,"output_tokens":2}}\n\n'
           b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
    monkeypatch.setattr(gateway_module.openai_bridge, "run",
                        lambda *a, **kw: OpenAIBridgeResult(
                            status=200, headers={"content-type": "text/event-stream"},
                            body_chunks=iter([sse])))


def test_non_streaming_request_gets_one_json_message_not_sse(pool_env, monkeypatch):
    """Claude Code's auto-mode safety classifier calls with stream:false. An
    SSE body makes the client read the model as unavailable, which silently
    blocks every tool that needs a safety decision."""
    save_pool(Pool(profiles=[_codex_profile()]))
    _sse_ok_bridge(monkeypatch)

    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("no transport expected")))
    result = gw.handle("POST", "/v1/messages", {},
                       json.dumps({"model": "claude-opus-5[1m]", "stream": False,
                                   "messages": [{"role": "user", "content": "hi"}]}).encode())

    assert result.status == 200
    assert result.headers["content-type"] == "application/json"
    body = json.loads(b"".join(result.body_chunks))
    assert body["type"] == "message" and body["role"] == "assistant"
    assert body["content"] == [{"type": "text", "text": "SAFE"}]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"]["output_tokens"] == 2


def test_streaming_request_still_gets_sse(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex_profile()]))
    _sse_ok_bridge(monkeypatch)

    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("no transport expected")))
    result = gw.handle("POST", "/v1/messages", {},
                       json.dumps({"model": "claude-sonnet-5", "stream": True,
                                   "messages": [{"role": "user", "content": "hi"}]}).encode())

    assert result.headers["content-type"].startswith("text/event-stream")
    assert b"event: message_start" in b"".join(result.body_chunks)


def test_tool_call_survives_the_non_streaming_collapse(pool_env, monkeypatch):
    """A tool call arrives as streamed argument fragments; collapsing must
    reassemble them into real JSON input, not drop the call."""
    save_pool(Pool(profiles=[_codex_profile()]))
    sse = (b'event: message_start\ndata: {"type":"message_start","message":{"id":"m1","model":"gpt-5.6-sol"}}\n\n'
           b'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
           b'"content_block":{"type":"tool_use","id":"call_1","name":"Read","input":{}}}\n\n'
           b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
           b'"delta":{"type":"input_json_delta","partial_json":"{\\"file"}}\n\n'
           b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
           b'"delta":{"type":"input_json_delta","partial_json":"_path\\":\\"a.txt\\"}"}}\n\n'
           b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"tool_use"},'
           b'"usage":{"input_tokens":1,"output_tokens":1}}\n\n')
    monkeypatch.setattr(gateway_module.openai_bridge, "run",
                        lambda *a, **kw: OpenAIBridgeResult(status=200, headers={}, body_chunks=iter([sse])))

    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("no transport expected")))
    result = gw.handle("POST", "/v1/messages", {},
                       json.dumps({"model": "claude-opus-5", "stream": False,
                                   "messages": [{"role": "user", "content": "hi"}]}).encode())

    block, = json.loads(b"".join(result.body_chunks))["content"]
    assert block["name"] == "Read"
    assert block["input"] == {"file_path": "a.txt"}
