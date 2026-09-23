import json

import pytest

import claude_unlimited.gateway as gateway_module
from claude_unlimited.config import Pool, Profile, Settings, load_pool, save_pool
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

    def fake_run(profile, credential, body, timeout=120, parity=None, context=None):
        assert credential == "tok-c"
        return OpenAIBridgeResult(status=200, headers={"content-type": "text/event-stream"},
                                   body_chunks=iter([b"event: message_stop\ndata: {}\n\n"]))

    monkeypatch.setattr(gateway_module.openai_bridge, "run", fake_run)

    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("must not use the Anthropic transport")))
    result = gw.handle("POST", "/v1/messages", {}, b"{}")

    assert result.status == 200
    assert result.profile_id == "c"
    assert gw._current_profile_id == "c"


def test_rotating_onto_a_codex_profile_announces_it_and_answers(pool_env, monkeypatch):
    # The ordinary handover: the pool pointer is on another account and
    # rotation lands on the Codex one. Every other codex test starts from a
    # fresh Gateway (pointer None), so the announce branch never ran — and it
    # referenced a `decision` the codex handler does not have: a NameError
    # that dropped the client's connection mid-turn.
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=0, automatic=True, enabled=False),
        _codex_profile(priority=1),
    ]))

    def fake_run(profile, credential, body, timeout=120, parity=None, context=None):
        return OpenAIBridgeResult(status=200, headers={"content-type": "text/event-stream"},
                                   body_chunks=iter([b"event: message_stop\ndata: {}\n\n"]))

    monkeypatch.setattr(gateway_module.openai_bridge, "run", fake_run)
    announced = []
    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("must not use the Anthropic transport")))
    monkeypatch.setattr(gw, "_announce_rotation",
                        lambda pool, previous, profile, reason: announced.append((previous, profile.id, reason)))
    gw._current_profile_id = "a"

    result = gw.handle("POST", "/v1/messages", {}, b"{}")

    assert result.status == 200
    assert gw._current_profile_id == "c"
    assert len(announced) == 1 and announced[0][:2] == ("a", "c")
    assert isinstance(announced[0][2], str) and announced[0][2]


def test_success_response_never_leaks_raw_openai_headers_to_the_client(pool_env, monkeypatch):
    # OpenAI's raw response headers (Cloudflare ray/cookies, x-codex-* quota
    # telemetry) must be replaced by a clean Anthropic-shaped header set, or
    # the client can see it is not talking to Anthropic.
    save_pool(Pool(profiles=[_codex_profile()]))

    def fake_run(profile, credential, body, timeout=120, parity=None, context=None):
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

    def fake_run(profile, credential, body, timeout=120, parity=None, context=None):
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

    def fake_run(profile, credential, body, timeout=120, parity=None, context=None):
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

    def fake_run(profile, credential, body, timeout=120, parity=None, context=None):
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

    def fake_run(profile, credential, body, timeout=120, parity=None, context=None):
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


def _usage_sse(model: str) -> list[bytes]:
    """An Anthropic-shaped stream the usage capture can actually read: the
    model arrives on message_start, the final token counts on message_delta."""
    start = json.dumps({"type": "message_start",
                        "message": {"model": model, "usage": {"input_tokens": 10, "output_tokens": 0}}})
    delta = json.dumps({"type": "message_delta", "usage": {"input_tokens": 10, "output_tokens": 5}})
    return [f"event: message_start\ndata: {start}\n\n".encode(),
            f"event: message_delta\ndata: {delta}\n\n".encode(),
            b"event: message_stop\ndata: {}\n\n"]


def test_usage_records_the_claude_model_asked_for_next_to_the_openai_one(pool_env, monkeypatch):
    """A codex Profile answers as gpt-6-astra whether the client asked for Fable
    or for Opus — the mapping is the only difference. Without the requested
    model the log cannot tell them apart, which is exactly what made a nested
    'fable' subagent impossible to spot from our own records."""
    import claude_unlimited.usage_history as usage_history

    save_pool(Pool(profiles=[_codex_profile()]))

    def fake_run(profile, credential, body, timeout=120, parity=None, context=None):
        return OpenAIBridgeResult(status=200, headers={"content-type": "text/event-stream"},
                                   body_chunks=iter(_usage_sse("gpt-6-astra")))

    monkeypatch.setattr(gateway_module.openai_bridge, "run", fake_run)
    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("no Anthropic transport")))

    result = gw.handle("POST", "/v1/messages", {},
                       json.dumps({"model": "claude-fable-5-1", "stream": True,
                                   "messages": [{"role": "user", "content": "hi"}]}).encode())
    list(result.body_chunks)

    events = usage_history.list_events()
    assert len(events) == 1
    assert events[0].model == "gpt-6-astra"
    assert events[0].requested_model == "claude-fable-5-1"


def test_a_non_streaming_codex_request_still_records_both_models(pool_env, monkeypatch):
    """stream:false takes the assemble-to-JSON path, which reads the same parsed
    body the requested model comes from."""
    import claude_unlimited.usage_history as usage_history

    save_pool(Pool(profiles=[_codex_profile()]))

    def fake_run(profile, credential, body, timeout=120, parity=None, context=None):
        return OpenAIBridgeResult(status=200, headers={"content-type": "text/event-stream"},
                                   body_chunks=iter(_usage_sse("gpt-5.6-terra")))

    monkeypatch.setattr(gateway_module.openai_bridge, "run", fake_run)
    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("no Anthropic transport")))

    result = gw.handle("POST", "/v1/messages", {},
                       json.dumps({"model": "claude-opus-5", "stream": False,
                                   "messages": [{"role": "user", "content": "hi"}]}).encode())
    body = b"".join(result.body_chunks)

    assert result.headers["content-type"] == "application/json"
    assert json.loads(body)["type"] == "message"
    events = usage_history.list_events()
    assert len(events) == 1
    assert (events[0].model, events[0].requested_model) == ("gpt-5.6-terra", "claude-opus-5")



@pytest.mark.parametrize("stream_field, wants_sse", [
    ({}, False),                      # the classifier's shape: no `stream` at all
    ({"stream": False}, False),
    ({"stream": True}, True),
])
def test_only_an_explicit_stream_true_gets_sse_back(pool_env, monkeypatch, stream_field, wants_sse):
    """Claude Code's auto-mode safety classifier calls messages.create() with no
    `stream` field. Treating that as streaming sent it an SSE body it could not
    parse, and every Edit/Bash in auto mode on a Codex account failed with
    "claude-sonnet-5 is temporarily unavailable"."""
    save_pool(Pool(profiles=[_codex_profile()]))

    def fake_run(profile, credential, body, timeout=120, parity=None, context=None):
        return OpenAIBridgeResult(status=200, headers={"content-type": "text/event-stream"},
                                   body_chunks=iter(_usage_sse("gpt-5.6-terra")))

    monkeypatch.setattr(gateway_module.openai_bridge, "run", fake_run)
    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("no Anthropic transport")))
    body = {"model": "claude-sonnet-5", "max_tokens": 64, "stop_sequences": ["</block>"],
            "messages": [{"role": "user", "content": "classify"}], **stream_field}
    result = gw.handle("POST", "/v1/messages", {}, json.dumps(body).encode())
    raw = b"".join(result.body_chunks)
    if wants_sse:
        assert result.headers["content-type"].startswith("text/event-stream")
    else:
        assert result.headers["content-type"] == "application/json"
        assert json.loads(raw)["type"] == "message"


def test_an_unparsable_body_still_streams():
    assert gateway_module._client_wants_streaming(None) is True


# ---------------------------------------------------------------------------
# Issue #6 — prepaid Codex credits.
# ---------------------------------------------------------------------------

_SPENT_HEADERS = {
    "x-codex-primary-used-percent": "100",
    "x-codex-primary-window-minutes": "10080",
    "x-codex-credits-has-credits": "true",
    "x-codex-credits-balance": "12.50",
}


def _bridge_returning(status, headers):
    def fake_run(profile, credential, body, timeout=120, parity=None, context=None):
        return OpenAIBridgeResult(status=status, headers=dict(headers),
                                   body_chunks=iter([b'{"ok":true}']))
    return fake_run


def test_credit_headers_are_recorded_even_when_the_setting_is_off(pool_env, monkeypatch):
    """Part 1 of the issue: the balance is DISPLAY, and display must work
    whether or not the pool is allowed to spend it — "why is this account idle
    when I paid for credits?" is the question that has to be answerable."""
    from claude_unlimited.config import Settings

    save_pool(Pool(profiles=[_codex_profile()], settings=Settings(codex_spend_credits=False)))
    monkeypatch.setattr(gateway_module.openai_bridge, "run", _bridge_returning(200, _SPENT_HEADERS))

    gw = Gateway(transport=lambda req: None)
    gw.handle("POST", "/v1/messages", {}, b"{}")

    rt = gw.runtime_snapshot()["c"]
    assert rt.credits_has is True
    assert rt.credits_balance == 12.5
    # ...but nothing is being spent, and the account is parked as usual.
    assert rt.state is not gateway_module.ProfileState.ELIGIBLE


def test_a_spent_codex_profile_with_credits_keeps_serving_only_when_allowed(pool_env, monkeypatch):
    from claude_unlimited.config import Settings

    save_pool(Pool(profiles=[_codex_profile()], settings=Settings(codex_spend_credits=True)))
    monkeypatch.setattr(gateway_module.openai_bridge, "run", _bridge_returning(200, _SPENT_HEADERS))

    gw = Gateway(transport=lambda req: None)
    result = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert result.status == 200

    rt = gw.runtime_snapshot()["c"]
    assert rt.state == gateway_module.ProfileState.ELIGIBLE
    assert rt.last_usage_percent == 100.0
    from claude_unlimited.router import spending_on_credits
    assert spending_on_credits(rt) is True


def test_a_429_quota_with_credits_does_not_exhaust_the_profile(pool_env, monkeypatch):
    """The plan window is genuinely gone (429), but the account can still be
    served from credits, so marking it EXHAUSTED would idle an account that
    works."""
    from claude_unlimited.config import Settings

    save_pool(Pool(profiles=[_codex_profile()], settings=Settings(codex_spend_credits=True)))
    monkeypatch.setattr(gateway_module.openai_bridge, "run", _bridge_returning(429, _SPENT_HEADERS))

    gw = Gateway(transport=lambda req: None)
    gw.handle("POST", "/v1/messages", {}, b"{}")

    rt = gw.runtime_snapshot()["c"]
    assert rt.credits_has is True
    assert rt.state == gateway_module.ProfileState.ELIGIBLE


def test_a_429_quota_without_credits_still_exhausts_the_profile(pool_env, monkeypatch):
    from claude_unlimited.config import Settings

    headers = dict(_SPENT_HEADERS, **{"x-codex-credits-has-credits": "false",
                                       "x-codex-credits-balance": "0"})
    save_pool(Pool(profiles=[_codex_profile()], settings=Settings(codex_spend_credits=True)))
    monkeypatch.setattr(gateway_module.openai_bridge, "run", _bridge_returning(429, headers))

    gw = Gateway(transport=lambda req: None)
    gw.handle("POST", "/v1/messages", {}, b"{}")

    rt = gw.runtime_snapshot()["c"]
    assert rt.credits_has is False
    assert rt.state == gateway_module.ProfileState.EXHAUSTED


def test_turning_the_setting_off_stops_the_spending_on_the_next_sync(pool_env, monkeypatch):
    """A live toggle has to take effect now, not at the next request: the
    user turning it off is asking for the spending to stop."""
    from claude_unlimited.config import Settings

    save_pool(Pool(profiles=[_codex_profile()], settings=Settings(codex_spend_credits=True)))
    monkeypatch.setattr(gateway_module.openai_bridge, "run", _bridge_returning(200, _SPENT_HEADERS))

    gw = Gateway(transport=lambda req: None)
    gw.handle("POST", "/v1/messages", {}, b"{}")
    assert gw.runtime_snapshot()["c"].state == gateway_module.ProfileState.ELIGIBLE

    save_pool(Pool(profiles=[_codex_profile()], settings=Settings(codex_spend_credits=False)))
    rt = gw.runtime_snapshot()["c"]
    assert rt.state == gateway_module.ProfileState.DRAINING
    # The balance itself survives the toggle — it is observed, not configured.
    assert rt.credits_balance == 12.5


def test_an_oauth_profile_never_gets_credit_permission(pool_env, monkeypatch):
    """_may_spend_credits is codex-only: no other backend reports a balance,
    and a permission that can never legitimately apply must not be mirrored
    onto the runtime where a later change could read it."""
    from claude_unlimited.config import Settings

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1,
                                      automatic=True, enabled=True)],
                   settings=Settings(codex_spend_credits=True)))
    gw = Gateway(transport=lambda req: None)
    gw.runtime_snapshot()
    assert gw.runtime_snapshot()["a"].may_spend_credits is False


def test_credits_survive_a_restart(pool_env, monkeypatch):
    from claude_unlimited.config import Settings

    save_pool(Pool(profiles=[_codex_profile()], settings=Settings(codex_spend_credits=True)))
    monkeypatch.setattr(gateway_module.openai_bridge, "run", _bridge_returning(200, _SPENT_HEADERS))

    gw = Gateway(transport=lambda req: None)
    gw.handle("POST", "/v1/messages", {}, b"{}")

    restarted = Gateway(transport=lambda req: None)
    rt = restarted.runtime_snapshot()["c"]
    assert rt.credits_has is True
    assert rt.credits_balance == 12.5


# ---------------------------------------------------------------------------
# Codex per-model availability feeding "leave when Fable
# is spent".
#
# OpenAI reports availability keyed by the GPT model id, while the rule is
# about a Claude model (Fable), and resolving one to the other needs the
# parity list plus the model catalogue (file I/O). So the resolution happens
# in gateway._blocked_models_for and router.py receives a plain set.
# ---------------------------------------------------------------------------

def _unavailable(gpt_model, resets_at=None):
    from claude_unlimited.observation import ModelWindow
    return (ModelWindow(name=gpt_model, percent=100.0, resets_at=resets_at, active=True),)


def test_a_codex_profile_counts_as_spent_when_fables_gpt_target_is_unavailable(pool_env):
    """The whole point: claude-fable-5 maps to gpt-5.6-sol, and if OpenAI says
    that model is unavailable on this account the account has spent its
    Fable week — the Codex counterpart of Anthropic's Fable bucket."""
    from datetime import datetime, timedelta, timezone

    save_pool(Pool(profiles=[
        _codex_profile(priority=1, leave_on_fable_limit=True),
        Profile(id="a", name="A", kind="oauth", priority=2, automatic=True, enabled=True),
    ]))

    gw = Gateway(transport=lambda req: None)
    gw.runtime_snapshot()   # prime
    later = datetime.now(timezone.utc) + timedelta(hours=3)
    with gw._lock:
        gw._runtime["c"].model_usage = _unavailable("gpt-5.6-sol", later)
    gw.runtime_snapshot()   # re-sync recomputes blocked_models

    assert "claude-fable-5" in gw._runtime["c"].blocked_models

    from claude_unlimited.router import fable_spent, must_leave
    now = datetime.now(timezone.utc)
    assert fable_spent(gw._runtime["c"], now) is True
    assert must_leave(gw._runtime["c"], now) is True


def test_an_unavailable_non_fable_target_does_not_count_as_fable_spent(pool_env):
    """Only the Fable bucket drives the rule. Opus maps to a different GPT
    target; that one being unavailable is not a spent Fable week."""
    from datetime import datetime, timedelta, timezone
    from claude_unlimited import openai_models

    save_pool(Pool(profiles=[_codex_profile(leave_on_fable_limit=True)]))
    gw = Gateway(transport=lambda req: None)
    gw.runtime_snapshot()
    opus_target = openai_models.map_model("claude-opus-5-5").model
    fable_target = openai_models.map_model("claude-fable-5").model
    assert opus_target != fable_target, "the default parity must keep them apart for this test"
    later = datetime.now(timezone.utc) + timedelta(hours=3)
    with gw._lock:
        gw._runtime["c"].model_usage = _unavailable(opus_target, later)
    gw.runtime_snapshot()

    from claude_unlimited.router import fable_spent
    assert "claude-opus-5-5" in gw._runtime["c"].blocked_models   # the default Opus row
    assert fable_spent(gw._runtime["c"], datetime.now(timezone.utc)) is False


def test_an_available_codex_model_does_not_block(pool_env):
    from datetime import datetime, timezone
    from claude_unlimited.observation import ModelWindow

    save_pool(Pool(profiles=[_codex_profile()]))
    gw = Gateway(transport=lambda req: None)
    gw.runtime_snapshot()
    with gw._lock:
        gw._runtime["c"].model_usage = (
            ModelWindow(name="gpt-5.6-sol", percent=0.0, resets_at=None, active=False),)
    gw.runtime_snapshot()
    assert gw._runtime["c"].blocked_models == frozenset()


def test_an_expired_availability_window_stops_blocking(pool_env):
    """A bucket past its own available_at is stale, not blocking — holding an
    account back on it would idle capacity that has already returned."""
    from datetime import datetime, timedelta, timezone

    save_pool(Pool(profiles=[_codex_profile()]))
    gw = Gateway(transport=lambda req: None)
    gw.runtime_snapshot()
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    with gw._lock:
        gw._runtime["c"].model_usage = _unavailable("gpt-5.6-sol", past)
    gw.runtime_snapshot()
    assert gw._runtime["c"].blocked_models == frozenset()


def test_a_per_profile_codex_model_override_is_respected(pool_env):
    """The profile pins every Claude model to one GPT model, so when THAT is
    unavailable the account cannot serve anything — resolving through the
    parity list alone would have missed it."""
    from datetime import datetime, timedelta, timezone

    save_pool(Pool(profiles=[_codex_profile(codex_model="gpt-6-astra")]))
    gw = Gateway(transport=lambda req: None)
    gw.runtime_snapshot()
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    with gw._lock:
        gw._runtime["c"].model_usage = _unavailable("gpt-6-astra", later)
    gw.runtime_snapshot()

    blocked = gw._runtime["c"].blocked_models
    assert "claude-fable-5" in blocked and "claude-opus-5-5" in blocked
    # ...and the model the parity list would otherwise have mapped is NOT the
    # thing being consulted.
    assert "gpt-5.6-sol" not in blocked


def test_blocked_models_survives_the_per_tick_runtime_rebuild(pool_env):
    """_sync_snapshot reconstructs ProfileRuntime field by field on EVERY poll
    tick. A field missing from that constructor is silently dropped a second
    later — the exact trap that has already lost model_usage and the credit
    balance."""
    from datetime import datetime, timedelta, timezone

    save_pool(Pool(profiles=[_codex_profile()]))
    gw = Gateway(transport=lambda req: None)
    gw.runtime_snapshot()
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    with gw._lock:
        gw._runtime["c"].model_usage = _unavailable("gpt-5.6-sol", later)

    for _ in range(3):        # several ticks, as an open dashboard would cause
        gw.runtime_snapshot()
        assert "claude-fable-5" in gw._runtime["c"].blocked_models


def test_a_non_codex_profile_never_gets_blocked_models(pool_env):
    """Anthropic reports a percentage per model, which fable_spent() tests
    directly; mirroring it into this set too would double-count."""
    from datetime import datetime, timedelta, timezone
    from claude_unlimited.observation import ModelWindow

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth",
                                      automatic=True, enabled=True)]))
    gw = Gateway(transport=lambda req: None)
    gw.runtime_snapshot()
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    with gw._lock:
        gw._runtime["a"].model_usage = (
            ModelWindow(name="Fable", percent=100.0, resets_at=later, active=True),)
    gw.runtime_snapshot()
    assert gw._runtime["a"].blocked_models == frozenset()


def test_a_real_session_leaves_a_codex_account_whose_fable_target_is_unavailable(pool_env, monkeypatch):
    """End to end, not just the predicate: with the switch on, EVERY request —
    an Opus one included — must come back served by the Claude account; with
    the switch off the codex account keeps serving."""
    from datetime import datetime, timedelta, timezone
    from claude_unlimited.upstream import UpstreamResponse

    class FakeConnection:
        def close(self):
            pass

    def transport(req):
        return UpstreamResponse(
            status=200,
            headers={"anthropic-ratelimit-unified-5h-utilization": "0.1",
                     "anthropic-ratelimit-unified-5h-reset": "1799999999"},
            body_chunks=iter([b"ok"]), connection=FakeConnection())

    served = {}

    def ok_run(profile, credential, body, timeout=120, parity=None, context=None):
        served["hit"] = profile.id
        return OpenAIBridgeResult(status=200, headers={"content-type": "text/event-stream"},
                                   body_chunks=iter([b"event: message_stop\ndata: {}\n\n"]))

    monkeypatch.setattr(gateway_module.openai_bridge, "run", ok_run)
    later = datetime.now(timezone.utc) + timedelta(hours=3)

    # Switch ON: the whole session leaves, whatever the model.
    save_pool(Pool(profiles=[
        _codex_profile(priority=1, leave_on_fable_limit=True),
        Profile(id="a", name="A", kind="oauth", priority=2, automatic=True, enabled=True),
    ]))
    gw = Gateway(transport=transport)
    gw.runtime_snapshot()
    with gw._lock:
        gw._runtime["c"].model_usage = _unavailable("gpt-5.6-sol", later)
    for model in (b'{"model": "claude-fable-5"}', b'{"model": "claude-opus-5"}'):
        result = gw.handle("POST", "/v1/messages", {}, model)
        if result.body_chunks:
            list(result.body_chunks)
        assert result.profile_id == "a"
    assert "hit" not in served, "the codex profile must not be asked to serve"

    # Switch OFF: nothing changes — the codex account serves as it always did.
    # A FRESH gateway with the pointer cleared: choose() is sticky on "a" after
    # the handover above, and that stickiness is deliberate.
    save_pool(Pool(profiles=[
        _codex_profile(priority=1),
        Profile(id="a", name="A", kind="oauth", priority=2, automatic=True, enabled=True),
    ]))
    fresh = Gateway(transport=transport)
    fresh.runtime_snapshot()
    with fresh._lock:
        fresh._current_profile_id = None
        fresh._runtime["c"].model_usage = _unavailable("gpt-5.6-sol", later)
    result2 = fresh.handle("POST", "/v1/messages", {}, b'{"model": "claude-opus-5"}')
    if result2.body_chunks:
        list(result2.body_chunks)
    assert result2.profile_id == "c"
    assert served.get("hit") == "c"


def test_an_explicit_pin_still_reaches_a_codex_account_with_an_unavailable_model(pool_env, monkeypatch):
    """Same rule as the Anthropic side: the user named the
    account, so it is served and they get the provider's own error — never a
    silent substitution."""
    from datetime import datetime, timedelta, timezone
    from claude_unlimited.config import Settings

    save_pool(Pool(profiles=[
        _codex_profile(priority=1, leave_on_fable_limit=True),
        Profile(id="a", name="A", kind="oauth", priority=2, automatic=True, enabled=True),
    ]))

    served = {}

    def fake_run(profile, credential, body, timeout=120, parity=None, context=None):
        served["hit"] = profile.id
        return OpenAIBridgeResult(status=200, headers={"content-type": "text/event-stream"},
                                   body_chunks=iter([b"event: message_stop\ndata: {}\n\n"]))

    monkeypatch.setattr(gateway_module.openai_bridge, "run", fake_run)

    gw = Gateway(transport=lambda req: None)
    gw.runtime_snapshot()
    later = datetime.now(timezone.utc) + timedelta(hours=3)
    with gw._lock:
        gw._runtime["c"].model_usage = _unavailable("gpt-5.6-sol", later)

    result = gw.handle("POST", "/v1/messages", {}, b'{"model": "claude-fable-5"}',
                       forced_profile_id="c")
    if result.body_chunks:
        list(result.body_chunks)
    assert served.get("hit") == "c"


# --- the per-request capacity guard (docs/adr/0009) ---------------------------
#
# A codex Profile's backend holds ~272K tokens; the guard keeps any turn that
# would overflow it off that account, so a mixed pool can run at 1M. These
# drive the whole gateway with real bodies: one comfortably under the byte
# floor, one well over a GPT window. Nothing here touches a network.

import base64 as _b64
import json as _json
from datetime import datetime as _dt, timezone as _tz

from claude_unlimited import gpt_windows as _gw
from claude_unlimited.upstream import UpstreamResponse as _UpstreamResponse


def _oauth(**overrides) -> Profile:
    defaults = dict(id="a", name="A", kind="oauth", priority=2, automatic=True, enabled=True)
    defaults.update(overrides)
    return Profile(**defaults)


def _big_body(model="claude-opus-5", tokens_over=400_000) -> bytes:
    """A conversation the guard must estimate above every codex budget."""
    text = "x" * (tokens_over * 3)
    return _json.dumps({"model": model, "stream": True,
                        "messages": [{"role": "user", "content": text}]}).encode()


def _small_body(model="claude-opus-5") -> bytes:
    return _json.dumps({"model": model, "stream": True,
                        "messages": [{"role": "user", "content": "hi"}]}).encode()


def _image_body(image_bytes=1_400_000) -> bytes:
    """Over the byte floor purely because of one screenshot."""
    data = _b64.b64encode(b"\x89PNG" * (image_bytes // 4 + 1)).decode()[:image_bytes]
    return _json.dumps({"model": "claude-opus-5", "stream": True, "messages": [
        {"role": "user", "content": [{"type": "text", "text": "look"},
                                     {"type": "image", "source": {"type": "base64",
                                                                  "media_type": "image/png",
                                                                  "data": data}}]}]}).encode()


class _Conn:
    def close(self):
        pass


def _anthropic_ok(req):
    return _UpstreamResponse(status=200,
                             headers={"anthropic-ratelimit-unified-5h-utilization": "0.1",
                                      "anthropic-ratelimit-unified-5h-reset": "1799999999"},
                             body_chunks=iter([b"ok"]), connection=_Conn())


def _codex_recorder(monkeypatch):
    served = []

    def fake_run(profile, credential, body, timeout=120, parity=None, context=None):
        served.append(profile.id)
        return OpenAIBridgeResult(status=200, headers={"content-type": "text/event-stream"},
                                   body_chunks=iter([b"event: message_stop\ndata: {}\n\n"]))

    monkeypatch.setattr(gateway_module.openai_bridge, "run", fake_run)
    return served


def _drain(result):
    if result.body_chunks:
        list(result.body_chunks)
    return result


def _activity_lines():
    from claude_unlimited import activity
    return [f"{e.text} | {e.meta or ''}" for e in activity.list_events(limit=200)]


def test_a_conversation_that_outgrew_the_codex_window_moves_to_the_claude_account(pool_env, monkeypatch):
    """End to end: codex is priority 1 and the current account, the body is
    over its window — the request is served by the Claude account, the codex
    bridge is never asked, and the Activity line says why (not "rotated",
    which reads as ran out). Smaller turns still go to codex."""
    save_pool(Pool(profiles=[_codex_profile(priority=1), _oauth()]))
    served = _codex_recorder(monkeypatch)
    gw = Gateway(transport=_anthropic_ok)

    assert _drain(gw.handle("POST", "/v1/messages", {}, _small_body())).profile_id == "c"
    assert served == ["c"]

    result = _drain(gw.handle("POST", "/v1/messages", {}, _big_body()))
    assert result.status == 200 and result.profile_id == "a"
    assert served == ["c"], "the codex profile must not be asked to serve the oversized turn"
    lines = _activity_lines()
    assert any("C cannot hold this conversation — handed over to A" in line for line in lines), lines


def test_bodies_under_the_byte_floor_are_never_parsed_for_the_guard(pool_env, monkeypatch):
    """Stage 1 is free: with no branch mode on, a normal turn must not cost a
    JSON parse of the whole conversation."""
    save_pool(Pool(profiles=[_codex_profile(priority=1), _oauth()]))
    _codex_recorder(monkeypatch)
    gw = Gateway(transport=_anthropic_ok)
    real_parse = gateway_module._parsed_request
    calls = []

    def counting(body):
        calls.append(len(body))
        return real_parse(body)

    monkeypatch.setattr(gateway_module, "_parsed_request", counting)
    # handle() parses nothing; _handle_codex's own parse of the body it sends
    # is the one call that remains, for the requested-model record.
    _drain(gw.handle("POST", "/v1/messages", {}, _small_body()))
    assert len(calls) <= 1
    assert gateway_module._request_guard(_small_body(), None, load_pool()) is None
    assert gateway_module._request_guard(b"x" * (_gw.CERTAINLY_FITS_BYTES - 1), None, load_pool()) is None


def test_the_guard_never_sizes_an_oauth_or_api_account(pool_env):
    """Claude Code sizes those windows itself. Every kind in one pool: only
    the codex id is ever over capacity."""
    save_pool(Pool(profiles=[_codex_profile(priority=1), _oauth(),
                             Profile(id="k", name="K", kind="api", priority=3, automatic=True, enabled=True),
                             Profile(id="x", name="X", kind="api", priority=4, automatic=True, enabled=True,
                                     base_url="https://llm.example.com/v1")]))
    guard = gateway_module._request_guard(_big_body(), None, load_pool())
    assert guard.fit.over_capacity == frozenset({"c"})
    assert set(guard.windows) == {"c"}
    assert guard.fit.estimated_tokens > _gw.MIN_BUDGET
    # A pool with no codex account is not sized at all, however big the body.
    save_pool(Pool(profiles=[_oauth()]))
    assert gateway_module._request_guard(_big_body(), None, load_pool()) is None


def test_a_screenshot_does_not_bar_codex(pool_env, monkeypatch):
    """1.4 MB of base64 is over the byte floor but ~1.6K real tokens: the
    guard subtracts the image and codex keeps the turn."""
    save_pool(Pool(profiles=[_codex_profile(priority=1), _oauth()]))
    served = _codex_recorder(monkeypatch)
    gw = Gateway(transport=_anthropic_ok)
    body = _image_body()
    assert len(body) > _gw.CERTAINLY_FITS_BYTES
    assert _drain(gw.handle("POST", "/v1/messages", {}, body)).profile_id == "c"
    assert served == ["c"]


def test_when_no_eligible_account_can_hold_it_the_answer_is_the_exact_prompt_too_long(pool_env, monkeypatch):
    """D1: capacity exists but nothing can HOLD the conversation. Not a 503
    (Claude Code would hold ten minutes and retry the same body) but the one
    error it reactively compacts on, with real numbers."""
    save_pool(Pool(profiles=[_codex_profile(priority=1)]))
    served = _codex_recorder(monkeypatch)
    gw = Gateway(transport=_anthropic_ok)
    body = _big_body()
    result = gw.handle("POST", "/v1/messages", {}, body)
    assert result.status == 400
    assert result.error == "request_exceeds_every_window"
    estimate = _gw.estimate_input_tokens(body, _json.loads(body))
    assert result.error_detail == f"prompt is too long: {estimate} tokens > 226400 maximum"
    assert result.headers == {}          # no retry-after: nothing to wait for
    assert served == []
    lines = _activity_lines()
    assert any("Conversation exceeds C's window — asked Claude Code to compact" in line for line in lines), lines
    # Said once per session, not once per retry.
    gw.handle("POST", "/v1/messages", {}, body)
    assert sum("asked Claude Code to compact" in line for line in _activity_lines()) == 1
    # The account is untouched: the next smaller turn is served on it.
    assert _drain(gw.handle("POST", "/v1/messages", {}, _small_body())).profile_id == "c"


def test_the_prompt_too_long_never_replaces_capacity_exhaustion(pool_env, monkeypatch):
    """A pool that is genuinely out of capacity keeps today's 503 whatever
    the body size — the guard only speaks when an account could serve."""
    from claude_unlimited.observation import QuotaExhausted
    save_pool(Pool(profiles=[_codex_profile(priority=1)]))
    _codex_recorder(monkeypatch)
    gw = Gateway(transport=_anthropic_ok)
    gw.runtime_snapshot()
    later = _dt.now(_tz.utc).replace(year=2999)
    with gw._lock:
        gw._observe("c", QuotaExhausted(resets_at=later), _dt.now(_tz.utc))
    result = gw.handle("POST", "/v1/messages", {}, _big_body())
    assert result.status == 503 and result.error == "all_profiles_exhausted"


def test_an_explicit_profile_pin_over_capacity_gets_the_400_not_a_substitution(pool_env, monkeypatch):
    """D2: `cu code --profile <codex>` is honoured — the Claude account beside
    it is NOT used — but a request known to overflow is not sent either: the
    session is asked to compact and stays on its account."""
    save_pool(Pool(profiles=[_codex_profile(priority=1), _oauth()]))
    served = _codex_recorder(monkeypatch)
    gw = Gateway(transport=_anthropic_ok)
    result = gw.handle("POST", "/v1/messages", {}, _big_body(), forced_profile_id="c")
    assert result.status == 400 and result.error == "request_exceeds_every_window"
    assert result.profile_id is None and served == []
    assert any("pinned session" in line for line in _activity_lines())
    # The pin itself is intact for a turn that fits.
    assert _drain(gw.handle("POST", "/v1/messages", {}, _small_body(), forced_profile_id="c")).profile_id == "c"


def test_a_standing_take_over_on_codex_over_capacity_gets_the_400(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex_profile(priority=1), _oauth()]))
    served = _codex_recorder(monkeypatch)
    gw = Gateway(transport=_anthropic_ok)
    assert gw.force_active("c")
    result = gw.handle("POST", "/v1/messages", {}, _big_body())
    assert result.status == 400 and result.error == "request_exceeds_every_window"
    assert served == []
    assert any("Take over" in line for line in _activity_lines())
    assert gw._manual_profile_id == "c", "the take-over is not cleared by an oversized turn"


def test_a_branch_pinned_to_codex_moves_for_good_when_the_conversation_outgrows_it(pool_env, monkeypatch):
    """Distribute mode pins each branch to an account. A pin on codex whose
    conversation no longer fits is re-assigned — and the move is reported
    with its real cause."""
    save_pool(Pool(profiles=[_codex_profile(priority=1), _oauth()],
                   settings=Settings(distribute_sessions_default=True)))
    served = _codex_recorder(monkeypatch)
    gw = Gateway(transport=_anthropic_ok)
    headers = {"x-claude-code-session-id": "s1"}
    assert _drain(gw.handle("POST", "/v1/messages", headers, _small_body())).profile_id == "c"
    assert _drain(gw.handle("POST", "/v1/messages", headers, _big_body())).profile_id == "a"
    assert served == ["c"]
    assert any("Agent moved C → A | the conversation outgrew C's window" in line
               for line in _activity_lines()), _activity_lines()
    # For good: the pin now lives on A, so the next small turn stays there.
    assert _drain(gw.handle("POST", "/v1/messages", headers, _small_body())).profile_id == "a"
    assert served == ["c"]


def test_forced_for_subagents_falls_through_when_the_subagent_outgrows_codex(pool_env, monkeypatch):
    """The flag documents "falls back when unavailable"; a backend that
    cannot hold the conversation is exactly that."""
    save_pool(Pool(profiles=[_oauth(priority=1),
                             _codex_profile(priority=9, automatic=False, forced_for_subagents=True)]))
    served = _codex_recorder(monkeypatch)
    gw = Gateway(transport=_anthropic_ok)
    sub = {"x-claude-code-session-id": "s1", "x-claude-code-agent-id": "ag1"}
    assert _drain(gw.handle("POST", "/v1/messages", sub, _small_body())).profile_id == "c"
    assert _drain(gw.handle("POST", "/v1/messages", sub, _big_body())).profile_id == "a"
    assert served == ["c"]


def test_an_unknown_gpt_id_is_assumed_at_the_floor_and_said_once(pool_env, monkeypatch):
    """D5: a codex_model override the table has not met is budgeted at 272K
    (never excluded on a guess) and one Activity line names the assumption."""
    save_pool(Pool(profiles=[_codex_profile(priority=1, codex_model="gpt-99-nova"), _oauth()]))
    served = _codex_recorder(monkeypatch)
    gw = Gateway(transport=_anthropic_ok)
    guard = gateway_module._request_guard(_big_body(), None, load_pool())
    assert guard.windows["c"].assumed is True and guard.windows["c"].window == 272_000
    # Excluded by the estimate against the assumed floor — not by the guess.
    assert _drain(gw.handle("POST", "/v1/messages", {}, _small_body())).profile_id == "c"
    assert served == ["c"]
    assert _drain(gw.handle("POST", "/v1/messages", {}, _big_body())).profile_id == "a"
    lines = _activity_lines()
    assumed = [line for line in lines if "assuming a 272,000-token window for gpt-99-nova" in line]
    assert len(assumed) == 1, lines


def test_an_api_key_codex_profile_is_budgeted_at_the_public_api_window(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex_profile(priority=1, auth_mode="api_key"), _oauth()]))
    served = _codex_recorder(monkeypatch)
    gw = Gateway(transport=_anthropic_ok)
    assert _drain(gw.handle("POST", "/v1/messages", {}, _big_body(tokens_over=400_000))).profile_id == "c"
    assert served == ["c"]
    guard = gateway_module._request_guard(_big_body(), None, load_pool())
    assert guard.windows["c"].window == 922_000 and not guard.fit.over_capacity


def test_a_failing_estimator_degrades_to_todays_routing(pool_env, monkeypatch):
    """Anything going wrong inside the guard must mean "not sized", never a
    blocked request: codex serves exactly as it did before the guard."""
    save_pool(Pool(profiles=[_codex_profile(priority=1), _oauth()]))
    served = _codex_recorder(monkeypatch)

    def boom(body, parsed):
        raise RuntimeError("estimator bug")

    monkeypatch.setattr(gateway_module.gpt_windows, "estimate_input_tokens", boom)
    gw = Gateway(transport=_anthropic_ok)
    assert gateway_module._request_guard(_big_body(), None, load_pool()) is None
    assert _drain(gw.handle("POST", "/v1/messages", {}, _big_body())).profile_id == "c"
    assert served == ["c"]


def test_an_unparsable_oversized_body_is_still_sized_and_kept_off_codex(pool_env, monkeypatch):
    """No parse → bytes alone, which only over-estimates: codex is excluded,
    the Claude account takes it, and nothing raises."""
    save_pool(Pool(profiles=[_codex_profile(priority=1), _oauth()]))
    served = _codex_recorder(monkeypatch)
    gw = Gateway(transport=_anthropic_ok)
    body = b"{" + b"x" * (_gw.CERTAINLY_FITS_BYTES + 10_000)
    assert _drain(gw.handle("POST", "/v1/messages", {}, body)).profile_id == "a"
    assert served == []


def test_count_tokens_is_never_refused_by_the_guard(pool_env, monkeypatch):
    """Only a real turn is sized: codex answers count_tokens locally whatever
    the body size."""
    save_pool(Pool(profiles=[_codex_profile(priority=1)]))
    _codex_recorder(monkeypatch)
    gw = Gateway(transport=_anthropic_ok)
    result = _drain(gw.handle("POST", "/v1/messages/count_tokens", {}, _big_body()))
    assert result.status == 200 and result.profile_id == "c"


def test_claude_code_subagent_headers_reach_the_codex_bridge(pool_env, monkeypatch):
    # What Claude Code 2.1.278 actually sends: x-claude-code-agent-id for every
    # subagent, x-claude-code-parent-agent-id only for a nested one. Both must
    # reach the bridge, which turns them into the Codex subagent headers.
    save_pool(Pool(profiles=[_codex_profile()]))
    seen = []

    def fake_run(profile, credential, body, timeout=120, parity=None, context=None):
        seen.append(context)
        return OpenAIBridgeResult(status=200, headers={"content-type": "text/event-stream"},
                                   body_chunks=iter([b"event: message_stop\ndata: {}\n\n"]))

    monkeypatch.setattr(gateway_module.openai_bridge, "run", fake_run)
    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("unused")))
    body = json.dumps({"metadata": {"user_id": json.dumps({"session_id": "sess-1"})}}).encode()

    gw.handle("POST", "/v1/messages", {"x-claude-code-agent-id": "a-child"}, body)
    gw.handle("POST", "/v1/messages", {"x-claude-code-agent-id": "a-grand",
                                       "x-claude-code-parent-agent-id": "a-child"}, body)

    assert [(c.claude_session_id, c.agent_id, c.parent_agent_id) for c in seen] == [
        ("sess-1", "a-child", None), ("sess-1", "a-grand", "a-child")]
