"""Primitive speech: an instruction added to agent turns so replies use fewer
output tokens. The request body is the only thing touched, and only in one
place — so these tests pin that place, the cache-safety, and every account kind.
"""
import copy
import json
import sqlite3

import pytest

import claude_unlimited.db as db
import claude_unlimited.gateway as gateway_module
import claude_unlimited.usage_history as usage_history
from claude_unlimited import openai_translate, speech
from claude_unlimited.config import Pool, Profile, save_pool
from claude_unlimited.gateway import Gateway
from claude_unlimited.upstream import UpstreamResponse

TOOLS = [{"name": "Bash", "description": "run", "input_schema": {"type": "object"}}]
SYSTEM = [{"type": "text", "text": "You are Claude Code."},
          {"type": "text", "text": "Project rules.", "cache_control": {"type": "ephemeral"}}]


def agent_turn(**overrides):
    body = {"model": "claude-opus-5", "system": copy.deepcopy(SYSTEM), "tools": TOOLS,
            "messages": [{"role": "user", "content": "why does it re-render?"}]}
    body.update(overrides)
    return body


# ---- the instruction ------------------------------------------------------

def test_off_and_unknown_levels_have_no_instruction():
    assert speech.instruction("off") is None
    assert speech.instruction("wenyan") is None


@pytest.mark.parametrize("level", ["lite", "full", "ultra"])
def test_each_level_is_fixed_text_that_keeps_the_safety_rules(level):
    text = speech.instruction(level)
    # Deterministic: the same bytes every turn, or the prompt cache misses.
    assert text == speech.instruction(level)
    assert speech.MARKER in text and f"Level: {level}" in text
    for rule in ("error messages", "not, never, no, only, except, unless",
                 "commit messages", "irreversible", "user's language", "Privately"):
        assert rule in text


def test_the_levels_share_a_config_list():
    from claude_unlimited.config import SPEECH_LEVELS
    assert SPEECH_LEVELS == speech.LEVELS


# ---- applying it ------------------------------------------------------------

def test_it_is_the_last_system_block_and_leaves_the_cached_prefix_untouched():
    body = agent_turn()
    out, added = speech.apply(body, "full")
    assert added
    assert out["system"][:2] == SYSTEM                       # byte-identical, cache_control kept
    assert out["system"][2] == {"type": "text", "text": speech.instruction("full")}
    assert "cache_control" not in out["system"][2]          # never spends a breakpoint
    assert out["tools"] == body["tools"]
    assert out["messages"][0]["content"][0] == {"type": "text", "text": body["messages"][0]["content"]}


def test_the_input_is_never_mutated():
    body = agent_turn()
    before = copy.deepcopy(body)
    speech.apply(body, "ultra")
    assert body == before


def test_it_is_never_added_twice():
    once, _ = speech.apply(agent_turn(), "full")
    twice, added = speech.apply(once, "lite")
    assert not added and twice == once


def test_a_string_system_prompt_becomes_blocks_with_the_same_text():
    out, added = speech.apply(agent_turn(system="Be helpful."), "lite")
    assert added and out["system"][0] == {"type": "text", "text": "Be helpful."}


def test_no_system_prompt_gets_one_block():
    body = agent_turn()
    del body["system"]
    out, added = speech.apply(body, "lite")
    assert added and len(out["system"]) == 1


@pytest.mark.parametrize("tools", [None, []])
def test_helper_calls_without_tools_are_left_alone(tools):
    body = agent_turn(tools=tools)
    out, added = speech.apply(body, "full")
    assert not added and out is body


def test_an_unknown_system_shape_is_left_alone():
    body = agent_turn(system={"weird": True})
    out, added = speech.apply(body, "full")
    assert not added and out is body


# ---- the gateway ------------------------------------------------------------

class FakeSecretStore:
    def get_token(self, profile_id):
        return "sk-ant-api-key-long-enough"


class FakeConnection:
    def close(self):
        pass


@pytest.fixture
def pool_env(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(gateway_module, "secret_store", FakeSecretStore())
    return tmp_path


def _sent_body(level, kind="api", body=None, path="/v1/messages"):
    from claude_unlimited.config import Settings
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind=kind, automatic=True, enabled=True)],
                   settings=Settings(speech_level=level)))
    sent = []

    def transport(req):
        sent.append(req.body)
        return UpstreamResponse(status=200, headers={}, body_chunks=iter([b"{}"]), connection=FakeConnection())

    raw = json.dumps(body if body is not None else agent_turn()).encode()
    Gateway(transport=transport).handle("POST", path, {}, raw)
    return raw, sent[0]


def test_off_sends_the_request_byte_for_byte(pool_env):
    raw, sent = _sent_body("off")
    assert sent == raw


@pytest.mark.parametrize("kind", ["api", "oauth"])
def test_an_anthropic_bound_agent_turn_carries_the_instruction(pool_env, kind):
    _, sent = _sent_body("full", kind=kind)
    assert json.loads(sent)["system"][-1]["text"] == speech.instruction("full")


def test_token_counting_is_not_touched(pool_env):
    raw, sent = _sent_body("full", path="/v1/messages/count_tokens")
    assert sent == raw


def test_codex_gets_it_through_the_same_body_it_translates():
    # The gateway applies speech before the kind branch (checked below); the
    # codex bridge turns the system blocks into `instructions`, so the rule
    # reaches OpenAI models too.
    out, _ = speech.apply(agent_turn(), "ultra")
    translated = openai_translate.anthropic_to_responses(out) if hasattr(openai_translate, "anthropic_to_responses") else None
    text = openai_translate._extract_system_text(out["system"])
    assert speech.MARKER in text
    if translated is not None:
        assert speech.MARKER in json.dumps(translated)


def test_speech_is_applied_before_the_kind_branch():
    import inspect
    src = inspect.getsource(Gateway.handle)
    assert src.index("_speech_apply(") < src.index('if profile.kind == "codex"')


def test_the_level_is_recorded_on_the_usage_row():
    stats = gateway_module.eco.CompactionStats()
    raw = json.dumps(agent_turn()).encode()
    body, tagged = gateway_module._speech_apply(raw, stats, "lite")
    assert gateway_module._speech_level_of(tagged) == "lite"
    assert gateway_module._speech_level_of(stats) is None    # the untagged default is never mutated
    # Not applied (no tools) -> nothing claimed.
    raw2 = json.dumps(agent_turn(tools=[])).encode()
    body2, stats2 = gateway_module._speech_apply(raw2, gateway_module.eco.CompactionStats(), "lite")
    assert body2 == raw2 and gateway_module._speech_level_of(stats2) is None


def test_an_unparseable_body_fails_open():
    body, stats = gateway_module._speech_apply(b"not json", gateway_module.eco.CompactionStats(), "full")
    assert body == b"not json" and gateway_module._speech_level_of(stats) is None


# ---- settings, storage, statistics ----------------------------------------

def test_ships_off_and_rejects_unknown_levels():
    from claude_unlimited.config import Settings, validated_settings_changes
    assert Settings().speech_level == "off"
    for level in speech.LEVELS:
        assert validated_settings_changes({"speech_level": level}) == {"speech_level": level}
    with pytest.raises(ValueError):
        validated_settings_changes({"speech_level": "wenyan"})


@pytest.fixture
def db_env(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    db.close_this_thread()
    yield tmp_path
    db.close_this_thread()


def test_a_version_2_database_gains_the_column(db_env):
    conn = sqlite3.connect(db.path())
    db._migrate_to_1(conn)
    db._migrate_to_2(conn)
    conn.execute("PRAGMA user_version = 2")
    conn.execute("INSERT INTO usage_event (ts, profile_id, model) VALUES ('2026-09-17T10:00:00+00:00', 'a', 'm')")
    conn.commit()
    conn.close()
    live = db.connect()
    assert live.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert "speech_mode" in {r["name"] for r in live.execute("PRAGMA table_info(usage_event)")}
    assert usage_history.list_events()[0].speech_mode is None        # old rows read as off


def test_speech_mode_round_trips_and_is_summarised(db_env):
    usage_history.record("a", None, "claude-opus-5", {"output_tokens": 100}, speech_mode="full")
    usage_history.record("a", None, "claude-opus-5", {"output_tokens": 300}, speech_mode="full")
    usage_history.record("a", None, "claude-opus-5", {"output_tokens": 900})
    events = usage_history.list_events()
    assert [e.speech_mode for e in events] == ["full", "full", None]
    assert usage_history.speech_totals(events) == {
        "levels": {"full": {"requests": 2, "avg_output_tokens": 200}},
        "without": {"requests": 1, "avg_output_tokens": 900},
    }
    assert usage_history.speech_totals([]) == {"levels": {}, "without": {"requests": 0, "avg_output_tokens": 0}}


def test_every_setting_survives_a_save_and_load(pool_env):
    # load_pool reads Settings field by field. eco_tier was never read back,
    # so ECO saved as "light" silently came back "off" on the next load and
    # never ran. Any field missing from the reader fails here.
    from dataclasses import fields
    from claude_unlimited.config import Settings, load_pool
    non_default = {"update_mode": "manual", "language": "ro", "notifications_enabled": False,
                   "notify_update_available": False, "notify_approaching_threshold": False,
                   "notify_rotated": True, "notify_quota_reset": True, "notify_needs_attention": False,
                   "distribute_sessions_default": True, "keep_usage_fresh": False,
                   "model_parity": {"rows": []}, "eco_tier": "light", "speech_level": "ultra",
                   "codex_spend_credits": True, "return_to_preferred": True,
                   "fable_limit_all_profiles": True, "context_1m": "prefer_200k"}
    assert set(non_default) == {f.name for f in fields(Settings)}, "add the new field to this test"
    save_pool(Pool(profiles=[], settings=Settings(**non_default)))
    loaded = load_pool().settings
    for name, value in non_default.items():
        assert getattr(loaded, name) == value, name


def test_unknown_eco_and_speech_values_load_as_off(pool_env):
    from claude_unlimited.config import CONFIG_FILE, load_pool
    import claude_unlimited.config as config
    config.CONFIG_FILE.write_text(json.dumps({"profiles": [], "settings": {"eco_tier": "turbo", "speech_level": "wenyan"}}))
    loaded = load_pool().settings
    assert (loaded.eco_tier, loaded.speech_level) == ("off", "off")


# ---- the per-message reminder -----------------------------------------------

def conversation():
    return agent_turn(messages=[
        {"role": "user", "content": "what is left to do?"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "lots of output"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Reading."}]},
        {"role": "user", "content": [{"type": "text", "text": "<system-reminder>x</system-reminder>"},
                                     {"type": "text", "text": "tell me again",
                                      "cache_control": {"type": "ephemeral"}}]},
    ])


def test_each_typed_message_gets_the_reminder_and_tool_results_do_not():
    out, _ = speech.apply(conversation(), "ultra")
    m = out["messages"]
    nudge = speech.reminder("ultra")
    assert m[0]["content"] == [{"type": "text", "text": "what is left to do?"}, {"type": "text", "text": nudge}]
    assert m[1] == conversation()["messages"][1]
    assert m[2] == conversation()["messages"][2]                          # tool results untouched
    assert m[4]["content"][:2] == conversation()["messages"][4]["content"]  # cache_control block kept, in place
    assert m[4]["content"][2] == {"type": "text", "text": nudge}


def test_earlier_messages_are_identical_from_one_turn_to_the_next():
    # The cache contract: turn N+1 sends turn N's messages byte-for-byte.
    turn1 = agent_turn(messages=conversation()["messages"][:1])
    turn2 = conversation()
    out1, _ = speech.apply(turn1, "full")
    out2, _ = speech.apply(turn2, "full")
    assert out2["messages"][0] == out1["messages"][0]
    assert json.dumps(out2["system"]) == json.dumps(out1["system"])


def test_the_reminder_is_never_doubled():
    out, _ = speech.apply(conversation(), "full")
    stripped_system = {**out, "system": copy.deepcopy(SYSTEM)}    # as if only the messages carried it
    again, _ = speech.apply(stripped_system, "full")
    assert again["messages"] == out["messages"]


@pytest.mark.parametrize("level", ["full", "ultra"])
def test_terse_levels_ban_decorative_formatting(level):
    assert "no headings" in speech.instruction(level).lower()
    assert "no headings" in speech.reminder(level).lower() or "headings" in speech.reminder(level)


# ---- Codex quota evidence on the usage row (schema v4) ---------------------

def test_reasoning_and_the_5h_percent_are_recorded(db_env):
    usage_history.record("c", None, "gpt-5.6-sol",
                         {"input_tokens": 700, "cache_read_input_tokens": 49_300, "output_tokens": 900,
                          "reasoning_tokens": 650}, quota_5h_percent=26.0)
    usage_history.record("a", None, "claude-opus-5", {"input_tokens": 5, "output_tokens": 5})
    codex, claude = usage_history.list_events()
    assert (codex.reasoning_tokens, codex.quota_5h_percent, codex.cache_read_input_tokens) == (650, 26.0, 49_300)
    assert (claude.reasoning_tokens, claude.quota_5h_percent) == (None, None)


def test_the_codex_5h_percent_is_read_from_the_response_headers():
    assert gateway_module._codex_5h_percent({"X-Codex-Primary-Used-Percent": "26"}) == 26.0
    assert gateway_module._codex_5h_percent({}) is None
    assert gateway_module._codex_5h_percent({"x-codex-primary-used-percent": "n/a"}) is None


def test_the_codex_path_passes_the_percent_to_the_usage_row():
    import inspect
    src = inspect.getsource(Gateway._handle_codex)
    assert "quota_5h_percent=_codex_5h_percent(result.headers)" in src


def test_fable_limit_all_profiles_is_strict_and_the_retired_key_is_ignored(pool_env):
    """Only an explicit true turns the override on. An older config carrying
    the retired pool-wide model_limit_routing key (which meant something else)
    loads cleanly and turns nothing on."""
    import claude_unlimited.config as config
    from claude_unlimited.config import load_pool
    config.CONFIG_FILE.write_text(json.dumps({"profiles": [], "settings": {
        "model_limit_routing": True, "fable_limit_all_profiles": "true"}}))
    loaded = load_pool().settings
    assert loaded.fable_limit_all_profiles is False
    assert not hasattr(loaded, "model_limit_routing")
    config.CONFIG_FILE.write_text(json.dumps({"profiles": [], "settings": {"fable_limit_all_profiles": True}}))
    assert load_pool().settings.fable_limit_all_profiles is True


def test_leave_on_fable_limit_loads_strictly_and_defaults_off(pool_env):
    import claude_unlimited.config as config
    from claude_unlimited.config import load_pool
    config.CONFIG_FILE.write_text(json.dumps({"profiles": [
        {"id": "a", "name": "A", "kind": "oauth", "leave_on_fable_limit": True},
        {"id": "b", "name": "B", "kind": "oauth", "leave_on_fable_limit": "true"},
        {"id": "c", "name": "C", "kind": "api"},
    ], "settings": {}}))
    assert [p.leave_on_fable_limit for p in load_pool().profiles] == [True, False, False]
