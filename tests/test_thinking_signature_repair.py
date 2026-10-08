"""A thinking block's signature is only valid for the account/model that
produced it, so a rotation can leave the history carrying one Anthropic then
rejects with an exact, field-addressed 400. Recovery is deliberately narrow:
one request-only retry, completed turns only, never the active tool turn, and
only against Anthropic's own /v1/messages.
"""

import json

import pytest

import claude_unlimited.gateway as gateway_module
from claude_unlimited.config import Pool, Profile, save_pool
from claude_unlimited.gateway import Gateway
from claude_unlimited.proxy import UpstreamRequest, retry_without_rejected_thinking
from claude_unlimited.upstream import UpstreamResponse

URL = "https://api.anthropic.com/v1/messages"
REJECTION = {"type": "error", "error": {"type": "invalid_request_error",
             "message": "messages.1.content.0: Invalid `signature` in `thinking` block"}}


def _history():
    return {"model": "claude-sonnet-5-5", "messages": [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "hm", "signature": "sig-1"},
                                          {"type": "text", "text": "first answer"}]},
        {"role": "user", "content": "second question"},
    ]}


def _req(body=None, url=URL):
    raw = json.dumps(body or _history()).encode()
    return UpstreamRequest(url=url, method="POST",
                           headers={"Content-Length": str(len(raw)), "x-api-key": "k"}, body=raw)


def _repair(body=None, error=None, status=400, url=URL):
    return retry_without_rejected_thinking(_req(body, url), status, json.dumps(error or REJECTION).encode())


def test_a_completed_turn_loses_only_its_thinking_and_the_original_is_untouched():
    original = _req()
    repaired = retry_without_rejected_thinking(original, 400, json.dumps(REJECTION).encode())
    assert repaired is not None
    sent = json.loads(repaired.body)
    assert sent["messages"][1]["content"] == [{"type": "text", "text": "first answer"}]
    assert sent["messages"][0] == _history()["messages"][0] and sent["messages"][2] == _history()["messages"][2]
    assert repaired.headers["Content-Length"] == str(len(repaired.body))
    assert json.loads(original.body) == _history()  # the caller's stored conversation is never edited


@pytest.mark.parametrize("mutate", [
    lambda e: e["error"].update(message="messages.1.content.0: some other 400 mentioning thinking"),
    lambda e: e["error"].update(type="overloaded_error"),
    lambda e: e["error"].update(message="messages.7.content.0: Invalid `signature` in `thinking` block"),
    lambda e: e["error"].update(message="messages.1.content.1: Invalid `signature` in `thinking` block"),
])
def test_anything_but_the_exact_signature_rejection_is_not_repaired(mutate):
    error = json.loads(json.dumps(REJECTION))
    mutate(error)
    assert _repair(error=error) is None


def test_only_a_400_from_anthropics_own_messages_endpoint_is_repaired():
    assert _repair(status=500) is None
    assert _repair(url="https://openrouter.ai/api/v1/messages") is None
    assert _repair(url="https://api.anthropic.com/v1/complete") is None
    assert retry_without_rejected_thinking(_req(), 400, b"not json") is None
    assert retry_without_rejected_thinking(_req(), 400, b"x" * 70000) is None


def test_the_active_tool_turn_is_never_rewritten():
    body = _history()
    # The rejected block sits in the turn that is still waiting on its tool result.
    body["messages"] = [
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "hm", "signature": "sig-1"},
                                          {"type": "tool_use", "id": "t1", "name": "bash", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "done"}]},
    ]
    assert _repair(body=body) is None


def test_a_historical_message_made_only_of_thinking_is_not_emptied():
    body = _history()
    body["messages"][1]["content"] = [{"type": "thinking", "thinking": "hm", "signature": "sig-1"}]
    assert _repair(body=body) is None


# ---- through the Gateway ---------------------------------------------------

class _Conn:
    def close(self):
        pass


def _resp(status, body=b"ok"):
    return UpstreamResponse(status=status, headers={}, body_chunks=iter([body]), connection=_Conn())


class _Secrets:
    def get_token(self, profile_id):
        return "tok-" + profile_id


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    monkeypatch.setattr("claude_unlimited.gateway.usage_history.USAGE_HISTORY_FILE", tmp_path / "usage_history.jsonl")
    monkeypatch.setattr(gateway_module, "secret_store", _Secrets())
    monkeypatch.setattr(Gateway, "_maybe_check_oauth_credential", lambda self, p, rt: None)
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))
    return tmp_path


def test_gateway_retries_once_without_historical_thinking_and_relays_the_success(env):
    sent = []

    def transport(req):
        sent.append(json.loads(req.body))
        if len(sent) == 1:
            return _resp(400, json.dumps(REJECTION).encode())
        return _resp(200)

    result = Gateway(transport=transport).handle("POST", "/v1/messages", {}, json.dumps(_history()).encode())
    assert result.status == 200
    assert len(sent) == 2
    assert any(b.get("type") == "thinking" for b in sent[0]["messages"][1]["content"])
    assert all(b.get("type") != "thinking" for b in sent[1]["messages"][1]["content"])


def test_gateway_repairs_at_most_once_and_otherwise_relays_the_400(env):
    sent = []

    def transport(req):
        sent.append(1)
        return _resp(400, json.dumps(REJECTION).encode())

    result = Gateway(transport=transport).handle("POST", "/v1/messages", {}, json.dumps(_history()).encode())
    assert result.status == 400
    assert len(sent) == 2  # original + exactly one repair, no loop
    assert b"signature" in b"".join(result.body_chunks)
