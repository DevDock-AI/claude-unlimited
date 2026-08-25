import pytest

import claude_unlimited.gateway as gateway_module
import claude_unlimited.project_usage as project_usage
import claude_unlimited.usage_history as usage_history
from claude_unlimited.config import Pool, Profile, save_pool
from claude_unlimited.gateway import Gateway
from claude_unlimited.upstream import UpstreamResponse

REAL_SHAPED_SSE = (
    b'event: message_start\n'
    b'data: {"type":"message_start","message":{"model":"claude-haiku-4-5-20251001","id":"msg_1",'
    b'"usage":{"input_tokens":8,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,'
    b'"output_tokens":1}}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hey"}}\n\n'
    b'event: message_delta\n'
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    b'"usage":{"input_tokens":8,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,'
    b'"output_tokens":10}}\n\n'
    b'event: message_stop\n'
    b'data: {"type":"message_stop"}\n\n'
)


class FakeConnection:
    def close(self):
        pass


class FakeSecretStore:
    def __init__(self, tokens):
        self.tokens = tokens

    def get_token(self, profile_id):
        return self.tokens[profile_id]


def fake_sse_response():
    def chunks():
        # Deliberately split mid-event to exercise chunk-boundary buffering.
        for i in range(0, len(REAL_SHAPED_SSE), 17):
            yield REAL_SHAPED_SSE[i:i + 17]

    return UpstreamResponse(status=200, headers={"Content-Type": "text/event-stream"},
                             body_chunks=chunks(), connection=FakeConnection())


@pytest.fixture
def pool_env(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    monkeypatch.setattr(gateway_module, "secret_store", FakeSecretStore({"a": "tok-a"}))
    monkeypatch.setattr(usage_history, "USAGE_HISTORY_FILE", tmp_path / "usage_history.jsonl")
    monkeypatch.setattr(project_usage, "USAGE_FILE", tmp_path / "project_usage.json")
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True, enabled=True)]))
    return tmp_path


def test_client_receives_byte_perfect_body_and_usage_is_recorded(pool_env):
    gw = Gateway(transport=lambda req: fake_sse_response())
    result = gw.handle("POST", "/v1/messages", {}, b"{}")

    assert result.status == 200
    forwarded = b"".join(result.body_chunks)
    assert forwarded == REAL_SHAPED_SSE  # every byte forwarded, unmodified

    events = usage_history.list_events()
    assert len(events) == 1
    assert events[0].profile_id == "a"
    assert events[0].model == "claude-haiku-4-5-20251001"
    assert events[0].input_tokens == 8
    assert events[0].output_tokens == 10  # message_delta's final count, not message_start's provisional 1
    assert events[0].cost_usd is not None


def test_usage_recorded_even_when_consumer_stops_before_full_drain(pool_env):
    # A client that closes its socket once it has read what it needs abandons
    # this generator before its natural end, so any recording placed after
    # `yield from` never runs even though parsing already saw the full
    # model+usage. `.close()` is what CPython does to an abandoned generator.
    gw = Gateway(transport=lambda req: fake_sse_response())
    result = gw.handle("POST", "/v1/messages", {}, b"{}")

    gen = result.body_chunks
    consumed = b""
    for chunk in gen:
        consumed += chunk
        # Wait for message_delta's event block to be fully terminated (its
        # closing `\n\n`): the parser only processes a complete block, so
        # breaking earlier would leave capture.usage at message_start's
        # provisional value.
        if b'"output_tokens":10}}\n\n' in consumed:
            break
    gen.close()

    events = usage_history.list_events()
    assert len(events) == 1
    assert events[0].output_tokens == 10  # message_delta's final count was already parsed


def test_usage_is_attributed_to_resolved_project(pool_env, monkeypatch):
    import claude_unlimited.project_attribution as project_attribution

    projects_dir = pool_env / "claude_projects"
    proj_dir = projects_dir / "-Users-a-my-app"
    proj_dir.mkdir(parents=True)
    (proj_dir / "sess-1.jsonl").write_text("{}")
    monkeypatch.setattr(project_attribution, "PROJECTS_DIR", projects_dir)

    gw = Gateway(transport=lambda req: fake_sse_response())
    result = gw.handle("POST", "/v1/messages", {"X-Claude-Code-Session-Id": "sess-1"}, b"{}")
    list(result.body_chunks)  # drain to trigger recording

    events = usage_history.list_events()
    assert events[0].project_id == "-Users-a-my-app"


def test_no_usage_recorded_when_client_never_drains_body(pool_env):
    gw = Gateway(transport=lambda req: fake_sse_response())
    result = gw.handle("POST", "/v1/messages", {}, b"{}")
    # Never iterate result.body_chunks: simulates an undrained response.
    assert usage_history.list_events() == []


def test_non_streaming_response_also_tracked(pool_env):
    import json

    body = json.dumps({"model": "claude-sonnet-5", "usage": {"input_tokens": 5, "output_tokens": 3}}).encode()

    def chunks():
        yield body

    def fake_json_response():
        return UpstreamResponse(status=200, headers={"Content-Type": "application/json"},
                                 body_chunks=chunks(), connection=FakeConnection())

    gw = Gateway(transport=lambda req: fake_json_response())
    result = gw.handle("POST", "/v1/messages", {}, b"{}")
    forwarded = b"".join(result.body_chunks)
    assert forwarded == body

    events = usage_history.list_events()
    assert len(events) == 1
    assert events[0].model == "claude-sonnet-5"
    assert events[0].input_tokens == 5
