"""Codex conversation state: what the real Codex CLI keeps between requests
(openai/codex codex-rs/core/src/client.rs) and the bridge used to throw away —
stable session/thread identity, the prompt cache key, the sticky turn token,
and encrypted reasoning replayed before the output it preceded.
"""
import json

import pytest

import claude_unlimited.openai_bridge as bridge_module
from claude_unlimited import codex_state
from claude_unlimited.openai_bridge import ConversationContext, run
from claude_unlimited.openai_models import OpenAIModelTarget
from claude_unlimited.openai_translate import (ResponseTranslator, anthropic_request_to_openai,
                                               assistant_anchors, user_turn_index)

from test_openai_bridge import (FakeHTTPResponse, _cred, _install_fake_connections, _sse_body,
                                _subscription_profile, reset_backoff_state)  # noqa: F401

RS = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAAA-encrypted-1"}


@pytest.fixture(autouse=True)
def clean_state():
    codex_state.clear()
    yield
    codex_state.clear()


# ---- identity -----------------------------------------------------------------

def test_a_conversation_keeps_its_ids_across_requests():
    a = codex_state.conversation_ids("sess-1", None)
    b = codex_state.conversation_ids("sess-1", None)
    assert a == b and a.thread_id and a.session_id and not a.is_subagent
    assert a.prompt_cache_key == a.session_id


def test_a_subagent_shares_the_session_but_has_its_own_thread_and_cache_key():
    main = codex_state.conversation_ids("sess-1", None)
    sub = codex_state.conversation_ids("sess-1", "agent-x")
    assert sub.session_id == main.session_id
    assert sub.thread_id != main.thread_id and sub.parent_thread_id == main.thread_id
    assert sub.prompt_cache_key != main.prompt_cache_key and sub.is_subagent
    assert codex_state.conversation_ids("sess-1", "agent-y").thread_id != sub.thread_id


def test_a_nested_subagent_points_at_its_real_parent_not_the_main_thread():
    main = codex_state.conversation_ids("sess-n", None)
    child = codex_state.conversation_ids("sess-n", "agent-c")
    grandchild = codex_state.conversation_ids("sess-n", "agent-g", "agent-c")
    assert child.parent_thread_id == main.thread_id          # direct: no parent id sent
    assert grandchild.parent_thread_id == child.thread_id    # nested: its spawner
    assert grandchild.thread_id not in (main.thread_id, child.thread_id)


def test_unidentifiable_traffic_has_no_stable_ids():
    assert codex_state.conversation_ids(None, None) is None


# ---- turns ----------------------------------------------------------------------

def test_a_turn_is_counted_by_typed_messages_not_tool_results():
    body = {"messages": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "Bash", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "x"}]},
    ]}
    assert user_turn_index(body) == 1
    body["messages"].append({"role": "user", "content": [{"type": "text", "text": "next"}]})
    assert user_turn_index(body) == 2


def test_the_turn_token_is_kept_for_its_turn_only_and_never_replaced():
    codex_state.remember_turn_state("s", None, 1, "tok-A")
    codex_state.remember_turn_state("s", None, 1, "tok-B")         # Codex keeps the first (OnceLock)
    assert codex_state.turn_state("s", None, 1) == "tok-A"
    assert codex_state.turn_state("s", None, 2) is None             # never into the next turn
    assert codex_state.turn_state("s", "agent", 1) is None          # nor into another branch


# ---- reasoning --------------------------------------------------------------------

def test_the_translator_captures_reasoning_and_its_anchors():
    t = ResponseTranslator()
    for event in [
        {"type": "response.created", "response": {"model": "gpt-5.6-sol"}},
        {"type": "response.output_item.added", "item": {"type": "reasoning"}},
        {"type": "response.output_item.done", "item": RS},
        {"type": "response.output_item.added", "item": {"type": "function_call", "call_id": "call_9", "name": "Bash"}},
        {"type": "response.function_call_arguments.delta", "delta": "{}"},
        {"type": "response.output_item.done", "item": {"type": "function_call"}},
        {"type": "response.completed", "response": {"usage": {}}},
    ]:
        list(t.feed(event))
    assert t.reasoning_items == [RS]
    assert t.anchors() == [codex_state.anchor_for_call("call_9")]


def test_reasoning_is_replayed_right_before_the_message_it_preceded():
    codex_state.remember_reasoning("p1", "gpt-5.6-sol", [codex_state.anchor_for_call("call_9")], [RS])
    body = {"messages": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call_9", "name": "Bash", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_9", "content": "ok"}]},
    ]}
    out = anthropic_request_to_openai(
        body, OpenAIModelTarget(model="gpt-5.6-sol", reasoning_effort="medium"),
        reasoning_lookup=lambda anchors: codex_state.reasoning_for("p1", "gpt-5.6-sol", anchors),
        prompt_cache_key="key-1")
    types = [i["type"] for i in out["input"]]
    assert types == ["message", "reasoning", "function_call", "function_call_output"]
    assert out["input"][1] == RS
    assert out["include"] == ["reasoning.encrypted_content"] and out["prompt_cache_key"] == "key-1"
    assert out["store"] is False


def test_reasoning_never_crosses_accounts_or_models():
    anchors = [codex_state.anchor_for_call("call_9")]
    codex_state.remember_reasoning("p1", "gpt-5.6-sol", anchors, [RS])
    assert codex_state.reasoning_for("p2", "gpt-5.6-sol", anchors) == []
    assert codex_state.reasoning_for("p1", "gpt-5.6-terra", anchors) == []


def test_a_text_only_reply_is_matched_by_its_exact_text():
    codex_state.remember_reasoning("p1", "m", [codex_state.anchor_for_text("Done.")], [RS])
    message = {"role": "assistant", "content": [{"type": "text", "text": "Done."}]}
    assert codex_state.reasoning_for("p1", "m", assistant_anchors(message)) == [RS]


def test_items_without_encrypted_content_are_not_kept():
    codex_state.remember_reasoning("p1", "m", ["call:x"], [{"type": "reasoning", "id": "rs", "summary": []}])
    assert codex_state.reasoning_for("p1", "m", ["call:x"]) == []


# ---- the bridge, end to end -------------------------------------------------------

def _stream(call_id="call_1", turn_state=None):
    headers = {"x-codex-turn-state": turn_state} if turn_state else {}
    return FakeHTTPResponse(200, headers, _sse_body([
        {"type": "response.created", "response": {"model": "gpt-5.6-sol"}},
        {"type": "response.output_item.added", "item": {"type": "reasoning"}},
        {"type": "response.output_item.done", "item": RS},
        {"type": "response.output_item.added", "item": {"type": "function_call", "call_id": call_id, "name": "Bash"}},
        {"type": "response.function_call_arguments.delta", "delta": "{}"},
        {"type": "response.output_item.done", "item": {"type": "function_call"}},
        {"type": "response.completed", "response": {"usage": {}}},
    ]))


TOOL = [{"name": "Bash", "description": "run", "input_schema": {"type": "object"}}]


def _body(messages, tools=TOOL):
    return json.dumps({"model": "claude-fable-5-1", "messages": messages, "tools": tools}).encode()


FIRST = [{"role": "user", "content": "go"}]
SECOND = FIRST + [
    {"role": "assistant", "content": [{"type": "tool_use", "id": "call_1", "name": "Bash", "input": {}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "ok"}]},
]


def test_a_tool_loop_sends_stable_ids_the_turn_token_and_the_previous_reasoning(monkeypatch):
    conns = _install_fake_connections(monkeypatch, [_stream("call_1", turn_state="ts-1"), _stream("call_2")])
    ctx = ConversationContext(claude_session_id="sess-1", agent_id="agent-7")
    for body in (_body(FIRST), _body(SECOND)):
        result = run(_subscription_profile(), _cred(), body, context=ctx)
        list(result.body_chunks)                     # drain, as the gateway does

    h1, h2 = conns[0].requests[0]["headers"], conns[1].requests[0]["headers"]
    assert h1["session-id"] == h2["session-id"] and h1["thread-id"] == h2["thread-id"]
    assert h1["x-client-request-id"] != h2["x-client-request-id"]
    assert h1["x-openai-subagent"] == "collab_spawn" and "x-codex-parent-thread-id" in h1
    assert "x-codex-turn-state" not in h1 and h2["x-codex-turn-state"] == "ts-1"

    sent1, sent2 = json.loads(conns[0].requests[0]["body"]), json.loads(conns[1].requests[0]["body"])
    assert sent1["prompt_cache_key"] == sent2["prompt_cache_key"]
    assert "reasoning" not in [i["type"] for i in sent1["input"]]
    assert [i["type"] for i in sent2["input"]] == ["message", "reasoning", "function_call", "function_call_output"]
    assert sent2["input"][1]["encrypted_content"] == RS["encrypted_content"]


def test_rejected_reasoning_is_retried_once_without_it(monkeypatch):
    codex_state.remember_reasoning("p1", "gpt-5.6-sol", [codex_state.anchor_for_call("call_1")], [RS])
    bridge_module._MODEL_SUBSTITUTIONS["gpt-5.6-sol"] = "gpt-5.6-sol"
    reject = FakeHTTPResponse(400, {}, json.dumps({"error": {"message": "The encrypted content could not be verified."}}).encode())
    conns = _install_fake_connections(monkeypatch, [reject, _stream("call_2")])
    result = run(_subscription_profile(), _cred(), _body(SECOND),
                 context=ConversationContext(claude_session_id="sess-1"))
    list(result.body_chunks)
    assert result.status == 200 and len(conns) == 2
    retried = json.loads(conns[1].requests[0]["body"])
    assert "reasoning" not in [i["type"] for i in retried["input"]]


def test_without_a_conversation_every_request_still_gets_fresh_ids(monkeypatch):
    conns = _install_fake_connections(monkeypatch, [_stream(), _stream()])
    for _ in range(2):
        list(run(_subscription_profile(), _cred(), _body(FIRST)).body_chunks)
    assert conns[0].requests[0]["headers"]["session-id"] != conns[1].requests[0]["headers"]["session-id"]
    assert "prompt_cache_key" not in json.loads(conns[0].requests[0]["body"])


def test_the_gateway_passes_the_claude_code_conversation_to_the_bridge():
    import inspect
    from claude_unlimited.gateway import Gateway
    src = inspect.getsource(Gateway._handle_codex)
    assert "project_attribution.branch_key(headers, body)" in src
    assert "context=openai_bridge.ConversationContext(" in src


def test_side_calls_without_tools_do_not_join_the_conversation(monkeypatch):
    # The auto-mode classifier shares the session id; it must not take the
    # conversation's turn token or cache key.
    conns = _install_fake_connections(monkeypatch, [_stream("call_1", turn_state="ts-1"), _stream("call_2")])
    ctx = ConversationContext(claude_session_id="sess-1")
    list(run(_subscription_profile(), _cred(), _body(FIRST), context=ctx).body_chunks)
    list(run(_subscription_profile(), _cred(), _body(FIRST, tools=[]), context=ctx).body_chunks)
    agent, side = conns[0].requests[0], conns[1].requests[0]
    assert side["headers"]["session-id"] != agent["headers"]["session-id"]
    assert "x-codex-turn-state" not in side["headers"]
    assert "prompt_cache_key" not in json.loads(side["body"])


# ---- cache identity for traffic that is not Claude Code ---------------------

BIG_SYSTEM = "You are a careful assistant. " * 120   # > MIN_PROMPT_KEY_CHARS


def test_the_same_system_prompt_is_the_same_cache_identity():
    a = codex_state.identity_from_prompt("gpt-5.6-luna", BIG_SYSTEM)
    b = codex_state.identity_from_prompt("gpt-5.6-luna", BIG_SYSTEM)
    assert a is not None and a == b
    assert a.prompt_cache_key.startswith("cu-prompt-")


def test_a_different_prompt_or_model_is_a_different_identity():
    base = codex_state.identity_from_prompt("gpt-5.6-luna", BIG_SYSTEM)
    assert codex_state.identity_from_prompt("gpt-5.6-terra", BIG_SYSTEM) != base
    assert codex_state.identity_from_prompt("gpt-5.6-luna", BIG_SYSTEM + "!") != base


def test_it_survives_a_restart():
    # Derived, never stored: a daemon restart must not cost the cache.
    codex_state.clear()
    again = codex_state.identity_from_prompt("gpt-5.6-luna", BIG_SYSTEM)
    assert again == codex_state.identity_from_prompt("gpt-5.6-luna", BIG_SYSTEM)


def test_a_small_prompt_is_not_worth_keying_on():
    assert codex_state.identity_from_prompt("gpt-5.6-luna", "be brief") is None
    assert codex_state.identity_from_prompt("gpt-5.6-luna", "") is None


def test_a_fan_out_of_same_system_requests_shares_one_cache_key(monkeypatch):
    """The real case: 200 small requests, same instructions, different content,
    no Claude Code headers. They used to get a fresh random session-id each —
    the header ChatGPT derives cache affinity from — and cached 0 tokens."""
    conns = _install_fake_connections(monkeypatch, [_stream("c1"), _stream("c2")])
    for content in ("classify this", "classify that"):
        body = json.dumps({"model": "claude-haiku-4-5", "system": BIG_SYSTEM, "tools": [],
                           "messages": [{"role": "user", "content": content}]}).encode()
        list(run(_subscription_profile(), _cred(), body).body_chunks)
    h1, h2 = conns[0].requests[0]["headers"], conns[1].requests[0]["headers"]
    assert h1["session-id"] == h2["session-id"] and h1["thread-id"] == h2["thread-id"]
    k1 = json.loads(conns[0].requests[0]["body"])["prompt_cache_key"]
    assert k1 == json.loads(conns[1].requests[0]["body"])["prompt_cache_key"]


def test_claude_code_identity_still_wins_over_the_prompt(monkeypatch):
    conns = _install_fake_connections(monkeypatch, [_stream("c1")])
    body = json.dumps({"model": "claude-fable-5-1", "system": BIG_SYSTEM, "tools": TOOL,
                       "messages": FIRST}).encode()
    list(run(_subscription_profile(), _cred(), body,
             context=ConversationContext(claude_session_id="sess-9", agent_id="a1")).body_chunks)
    key = json.loads(conns[0].requests[0]["body"])["prompt_cache_key"]
    assert not key.startswith("cu-prompt-")
