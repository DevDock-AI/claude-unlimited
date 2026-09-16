"""Per-branch account pinning: subagents routed to their own accounts.

A "branch" is one conversation thread — a session's main agent, or one of its
subagents. Claude Code tags a subagent's requests with `x-claude-code-agent-id`
(the main agent sends none) and keeps the session id lineage-stable, so the
proxy can bind each branch to its own account and keep that branch's prompt
cache warm there while OTHER branches of the same session run elsewhere.

Two modes are covered:
  * a Profile flagged `forced_for_subagents` — every subagent goes to it, the
    main agent is untouched (a Claude orchestrator with GPT subagents);
  * `distribute=True` — every branch is spread across eligible accounts.

All offline: fake transport, tmp config, no keychain.
"""

import time as real_time
from datetime import datetime, timezone

import pytest

import claude_unlimited.gateway as gateway_module
from claude_unlimited.config import Pool, Profile, Settings, load_pool, save_pool
from claude_unlimited.gateway import Gateway
from claude_unlimited.router import ProfileState
from claude_unlimited.upstream import UpstreamResponse


class FakeConnection:
    def close(self):
        pass


class FakeSecretStore:
    def __init__(self, tokens):
        self.tokens = tokens

    def get_token(self, profile_id):
        return self.tokens.get(profile_id, f"tok-{profile_id}")


def fake_response(status=200, headers=None, body=b"ok"):
    def chunks():
        if body:
            yield body
    return UpstreamResponse(status=status, headers=headers or {}, body_chunks=chunks(),
                            connection=FakeConnection())


HEALTHY = {"anthropic-ratelimit-unified-5h-utilization": "0.1",
           "anthropic-ratelimit-unified-5h-reset": "1799999999"}


@pytest.fixture
def pool_env(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    monkeypatch.setattr("claude_unlimited.gateway.usage_history.USAGE_HISTORY_FILE", tmp_path / "usage_history.jsonl")
    monkeypatch.setattr(gateway_module, "secret_store", FakeSecretStore({}))
    return tmp_path


def prof(pid, **kw):
    kw.setdefault("kind", "oauth")
    kw.setdefault("automatic", True)
    kw.setdefault("enabled", True)
    return Profile(id=pid, name=pid.upper(), **kw)


def hdrs(session="s1", agent=None, parent=None):
    """Request headers as Claude Code sends them. No agent header == main."""
    h = {"x-claude-code-session-id": session}
    if agent:
        h["x-claude-code-agent-id"] = agent
    if parent:
        h["x-claude-code-parent-agent-id"] = parent
    return h


def serve(gw, headers, **kw):
    result = gw.handle("POST", "/v1/messages", headers, b"{}", **kw)
    if result.body_chunks:
        list(result.body_chunks)
    return result


def healthy_gateway():
    return Gateway(transport=lambda req: fake_response(200, HEALTHY))


# ---- forced_for_subagents -------------------------------------------------

def test_subagents_go_to_the_forced_profile_and_the_main_agent_does_not(pool_env):
    # The headline case: a Claude orchestrator driving GPT subagents.
    save_pool(Pool(profiles=[prof("main_acct", priority=1),
                             prof("subs", priority=9, forced_for_subagents=True)]))
    gw = healthy_gateway()

    assert serve(gw, hdrs()).profile_id == "main_acct"                      # main: normal routing
    assert serve(gw, hdrs(agent="ag1")).profile_id == "subs"                # subagent: forced
    assert serve(gw, hdrs(agent="ag2")).profile_id == "subs"                # every subagent
    assert serve(gw, hdrs()).profile_id == "main_acct"                      # main still untouched


def test_the_forced_profile_is_used_even_though_rotation_would_never_pick_it(pool_env):
    # priority 9 and not even `automatic`: forcing must override both.
    save_pool(Pool(profiles=[prof("main_acct", priority=1),
                             prof("subs", priority=9, automatic=False, forced_for_subagents=True)]))
    gw = healthy_gateway()
    assert serve(gw, hdrs(agent="ag1")).profile_id == "subs"


def test_subagents_fall_back_to_spreading_when_the_forced_profile_is_unavailable(pool_env):
    # Owner-specified fallback: don't fail, alternate across the others.
    save_pool(Pool(profiles=[prof("a"), prof("b"),
                             prof("subs", forced_for_subagents=True)]))
    gw = healthy_gateway()
    serve(gw, hdrs())  # prime runtime
    with gw._lock:
        gw._runtime["subs"].state = ProfileState.EXHAUSTED

    landed = {serve(gw, hdrs(agent=f"ag{i}")).profile_id for i in range(4)}
    assert "subs" not in landed
    assert landed <= {"a", "b"} and landed  # spread across what's left


def test_an_explicit_profile_pin_still_beats_forced_subagents(pool_env):
    save_pool(Pool(profiles=[prof("a"), prof("subs", forced_for_subagents=True)]))
    gw = healthy_gateway()
    result = serve(gw, hdrs(agent="ag1"), forced_profile_id="a")
    assert result.profile_id == "a"


def test_a_disabled_forced_profile_is_ignored(pool_env):
    save_pool(Pool(profiles=[prof("a"), prof("subs", enabled=False, forced_for_subagents=True)]))
    gw = healthy_gateway()
    assert serve(gw, hdrs(agent="ag1")).profile_id == "a"


def test_forced_subagents_works_for_a_codex_profile(pool_env):
    # The owner's actual setup is a codex account as the subagent target, so
    # Profile.kind must not affect the routing decision. Asserted at the
    # decision level: actually serving it would exercise openai_bridge, which
    # is a different module's business.
    save_pool(Pool(profiles=[prof("claude_acct"), prof("gpt", kind="codex", forced_for_subagents=True)]))
    gw = healthy_gateway()
    pool = load_pool()
    decision = gw._branch_decision(
        pool, gw._sync_snapshot(pool), datetime.now(timezone.utc),
        ("s1", "ag1"), True, None, set(), False, True)
    assert decision.profile_id == "gpt"
    assert decision.reason == "subagent_forced"


def test_the_main_branch_is_never_given_the_forced_profile(pool_env):
    # Guards the asymmetry that makes the feature useful.
    save_pool(Pool(profiles=[prof("claude_acct"), prof("gpt", kind="codex", forced_for_subagents=True)]))
    gw = healthy_gateway()
    pool = load_pool()
    decision = gw._branch_decision(
        pool, gw._sync_snapshot(pool), datetime.now(timezone.utc),
        ("s1", "main"), False, None, set(), False, True)
    assert decision.profile_id == "claude_acct"
    assert decision.reason != "subagent_forced"


# ---- pin stability (cache affinity) ---------------------------------------

def test_a_branch_stays_on_its_account_across_turns(pool_env):
    # The cache-affinity guarantee: same branch -> same account every turn.
    save_pool(Pool(profiles=[prof("a"), prof("b")]))
    gw = healthy_gateway()
    first = serve(gw, hdrs(agent="ag1"), distribute=True).profile_id
    for _ in range(4):
        assert serve(gw, hdrs(agent="ag1"), distribute=True).profile_id == first


def test_different_branches_of_one_session_spread_across_accounts(pool_env):
    save_pool(Pool(profiles=[prof("a"), prof("b")]))
    gw = healthy_gateway()
    main = serve(gw, hdrs(), distribute=True).profile_id
    sub1 = serve(gw, hdrs(agent="ag1"), distribute=True).profile_id
    assert {main, sub1} == {"a", "b"}  # one session, two accounts, simultaneously


def test_without_distribute_or_forcing_nothing_is_pinned(pool_env):
    # Default behavior must be byte-identical to before the feature.
    save_pool(Pool(profiles=[prof("a"), prof("b")]))
    gw = healthy_gateway()
    serve(gw, hdrs(agent="ag1"))
    assert gw.branch_pins() == []


def test_a_forced_profile_does_not_change_ordinary_sessions_for_the_main_agent(pool_env):
    """Setting forced_for_subagents on one Profile must not quietly move every
    OTHER session onto branch routing. A plain `cu code` main agent keeps the
    old sticky behaviour: same account turn after turn, no pin recorded, and
    the shared rotation pointer still following it."""
    save_pool(Pool(profiles=[prof("a", priority=1),
                             prof("b", priority=2),
                             prof("subs", priority=9, forced_for_subagents=True)]))
    gw = healthy_gateway()

    served = [serve(gw, hdrs(session="s1")).profile_id for _ in range(4)]
    assert served == ["a", "a", "a", "a"]   # sticky, lowest priority number
    # Only the subagent's branch is tracked; the main agent's is not.
    assert [pin[1] for pin in gw.branch_pins()] == []
    assert gw._current_profile_id == "a"    # pointer still moves with rotation


# ---- distribute_sessions_default (the global Settings toggle) -------------

def _distributing_pool(*profiles):
    return Pool(profiles=list(profiles), settings=Settings(distribute_sessions_default=True))


def test_the_global_setting_distributes_a_session_that_did_not_ask(pool_env):
    """Settings → "Balance sessions and subagents across accounts" makes plain `cu code` behave as if
    --distribute had been passed, with no flag and no relaunch."""
    save_pool(_distributing_pool(prof("a"), prof("b")))
    gw = healthy_gateway()
    main = serve(gw, hdrs()).profile_id
    sub = serve(gw, hdrs(agent="ag1")).profile_id
    assert {main, sub} == {"a", "b"}


def test_the_flag_still_works_while_the_global_setting_is_off(pool_env):
    # OR-ed, not assigned: the setting can only ever turn distribution ON.
    save_pool(Pool(profiles=[prof("a"), prof("b")],
                   settings=Settings(distribute_sessions_default=False)))
    gw = healthy_gateway()
    main = serve(gw, hdrs(), distribute=True).profile_id
    sub = serve(gw, hdrs(agent="ag1"), distribute=True).profile_id
    assert {main, sub} == {"a", "b"}


def test_an_explicit_profile_pin_beats_the_global_setting(pool_env):
    """--profile is the user saying "this one, nothing else"; a background
    setting must never quietly move a pinned session off its account."""
    save_pool(_distributing_pool(prof("a"), prof("b")))
    gw = healthy_gateway()
    for headers in (hdrs(), hdrs(agent="ag1"), hdrs(agent="ag2")):
        assert serve(gw, headers, forced_profile_id="b").profile_id == "b"


def test_turning_the_global_setting_off_restores_sticky_rotation(pool_env):
    """Read per request, so the toggle takes effect without a daemon restart —
    and turning it back off has to actually undo it."""
    save_pool(_distributing_pool(prof("a", priority=1), prof("b", priority=2)))
    gw = healthy_gateway()
    serve(gw, hdrs())
    serve(gw, hdrs(agent="ag1"))
    assert len(gw.branch_pins()) == 2

    save_pool(Pool(profiles=[prof("a", priority=1), prof("b", priority=2)],
                   settings=Settings(distribute_sessions_default=False)))
    # A brand-new branch now follows plain rotation and records no pin.
    assert serve(gw, hdrs(session="s2", agent="ag9")).profile_id == "a"
    assert not any(p["session_id"] == "s2" for p in gw.branch_pins())


def test_unidentifiable_traffic_routes_unpinned(pool_env):
    # A non-Claude-Code client sends no session id: never an error, no pin.
    save_pool(Pool(profiles=[prof("a")]))
    gw = healthy_gateway()
    assert serve(gw, {}, distribute=True).profile_id == "a"
    assert gw.branch_pins() == []


# ---- failover / re-pin ----------------------------------------------------

def test_a_branch_repins_when_its_account_stops_being_eligible(pool_env):
    save_pool(Pool(profiles=[prof("a"), prof("b")]))
    gw = healthy_gateway()
    first = serve(gw, hdrs(agent="ag1"), distribute=True).profile_id
    with gw._lock:
        gw._runtime[first].state = ProfileState.EXHAUSTED

    second = serve(gw, hdrs(agent="ag1"), distribute=True).profile_id
    assert second != first
    # ...and the pin now names the account that actually served it.
    assert [p["profile_id"] for p in gw.branch_pins() if p["agent_id"] == "ag1"] == [second]


# ---- the pointer/notification discipline ----------------------------------

def test_branch_routing_never_moves_the_shared_rotation_pointer(pool_env):
    # A subagent speaks for ONE branch; it must not yank the global pointer
    # that other concurrent sessions rely on.
    save_pool(Pool(profiles=[prof("a", priority=1), prof("subs", priority=9, forced_for_subagents=True)]))
    gw = healthy_gateway()
    serve(gw, hdrs())                     # main -> a, sets the pointer
    assert gw._current_profile_id == "a"
    serve(gw, hdrs(agent="ag1"))          # subagent -> subs
    assert gw._current_profile_id == "a"  # unchanged


# ---- store hygiene --------------------------------------------------------

def test_a_pin_expires_after_its_ttl(pool_env, monkeypatch):
    save_pool(Pool(profiles=[prof("a"), prof("b")]))
    gw = healthy_gateway()
    serve(gw, hdrs(agent="ag1"), distribute=True)
    assert gw.branch_pins()

    later = real_time.monotonic() + gateway_module.BRANCH_PIN_TTL_SECONDS + 1
    monkeypatch.setattr(gateway_module, "_pin_clock", lambda: later)
    assert gw.branch_pins() == []


def test_the_pin_map_is_capped(pool_env, monkeypatch):
    monkeypatch.setattr(gateway_module, "BRANCH_PIN_CAP", 3)
    save_pool(Pool(profiles=[prof("a")]))
    gw = healthy_gateway()
    for i in range(6):
        serve(gw, hdrs(agent=f"ag{i}"), distribute=True)
    assert len(gw.branch_pins()) <= 3


def test_pins_for_a_removed_profile_are_dropped(pool_env):
    save_pool(Pool(profiles=[prof("a"), prof("b")]))
    gw = healthy_gateway()
    serve(gw, hdrs(agent="ag1"), distribute=True)
    assert gw.branch_pins()

    save_pool(Pool(profiles=[prof("b")]))  # 'a' deleted
    serve(gw, hdrs(agent="ag2"), distribute=True)  # any request re-syncs
    assert all(p["profile_id"] == "b" for p in gw.branch_pins())


# ---- dashboard signals for per-branch routing -----------------------------

def test_live_agent_counts_show_where_agents_actually_are(pool_env):
    """Branch-routed traffic never moves the shared pointer, so the Dashboard
    reads these counts to show which accounts are really active."""
    save_pool(_distributing_pool(prof("a"), prof("b")))
    gw = healthy_gateway()
    serve(gw, hdrs())
    serve(gw, hdrs(agent="ag1"))
    serve(gw, hdrs(session="s2"))
    counts = gw.live_agent_counts()
    assert sum(counts.values()) == 3 and set(counts) == {"a", "b"}


def test_agents_moved_off_an_unavailable_account_are_logged_and_notified_once(pool_env, monkeypatch):
    """With every agent branch-routed, no request moves the pointer, so the
    old "Rotated" path never fired. Each move is logged; the notification fires
    once for the account, not once per agent."""
    import json
    notified = []
    monkeypatch.setattr(gateway_module.notifications, "notify_if_enabled",
                        lambda kind, title, message, settings: notified.append(kind))
    save_pool(_distributing_pool(prof("a", priority=1), prof("b", priority=2)))
    gw = healthy_gateway()
    agents = ["ag1", "ag2", "ag3", "ag4"]
    placed = {ag: serve(gw, hdrs(agent=ag)).profile_id for ag in agents}
    assert sorted(placed.values()) == ["a", "a", "b", "b"]

    with gw._lock:
        gw._runtime["a"].state = ProfileState.EXHAUSTED
    for ag in agents:
        assert serve(gw, hdrs(agent=ag)).profile_id == "b"

    events = [json.loads(line) for line in (pool_env / "activity.jsonl").read_text().splitlines()]
    moves = [e for e in events if any("Agent moved A → B" in str(v) for v in e.values())]
    assert len(moves) == 2                    # one per agent that actually moved
    assert notified.count("rotated") == 1     # one per account, not per agent


def test_the_request_body_is_not_parsed_when_no_per_branch_mode_applies(pool_env, monkeypatch):
    """Identifying a branch JSON-parses the whole (possibly multi-MB) body;
    ordinary rotation must not pay for a key it would ignore."""
    calls = []
    real_branch_key = gateway_module.project_attribution.branch_key
    monkeypatch.setattr(gateway_module.project_attribution, "branch_key",
                        lambda headers, body=b"": calls.append(1) or real_branch_key(headers, body))
    save_pool(Pool(profiles=[prof("a"), prof("b")]))
    gw = healthy_gateway()
    serve(gw, hdrs(agent="ag1"))
    assert calls == []
    serve(gw, hdrs(agent="ag1"), distribute=True)
    assert calls == [1]

