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


def test_a_pinned_session_keeps_its_main_agent_and_sends_subagents_to_the_forced_profile(pool_env):
    # `cu code --profile a` with "Forced in subagents" on `subs`: orchestrate
    # on one account, run the workers on another. Both held exactly.
    save_pool(Pool(profiles=[prof("a"), prof("b"), prof("subs", forced_for_subagents=True)]))
    gw = healthy_gateway()
    assert serve(gw, hdrs(), forced_profile_id="a").profile_id == "a"
    assert serve(gw, hdrs(agent="ag1"), forced_profile_id="a").profile_id == "subs"
    assert serve(gw, hdrs(agent="ag2", parent="ag1"), forced_profile_id="a").profile_id == "subs"
    assert serve(gw, hdrs(), forced_profile_id="a").profile_id == "a"


def test_in_a_pinned_session_an_unavailable_forced_profile_fails_the_subagent_instead_of_rerouting(pool_env):
    # Strict like the pin itself: nothing is rerouted, not even to the
    # session's own account.
    save_pool(Pool(profiles=[prof("a"), prof("b"), prof("subs", forced_for_subagents=True)]))
    gw = healthy_gateway()
    serve(gw, hdrs(), forced_profile_id="a")  # prime runtime
    with gw._lock:
        gw._runtime["subs"].state = ProfileState.AUTH_INVALID
    result = serve(gw, hdrs(agent="ag1"), forced_profile_id="a")
    assert result.profile_id is None and result.status >= 400
    assert serve(gw, hdrs(), forced_profile_id="a").profile_id == "a"   # main unaffected


def test_without_a_forced_profile_a_pinned_sessions_subagents_follow_the_pin(pool_env):
    save_pool(Pool(profiles=[prof("a"), prof("b")]))
    gw = healthy_gateway()
    assert serve(gw, hdrs(agent="ag1"), forced_profile_id="a").profile_id == "a"


def test_in_a_pinned_session_a_disabled_forced_profile_is_ignored_and_subagents_follow_the_pin(pool_env):
    save_pool(Pool(profiles=[prof("a"), prof("b"), prof("subs", enabled=False, forced_for_subagents=True)]))
    gw = healthy_gateway()
    assert serve(gw, hdrs(agent="ag1"), forced_profile_id="a").profile_id == "a"


def test_a_disabled_forced_profile_is_ignored(pool_env):
    save_pool(Pool(profiles=[prof("a"), prof("subs", enabled=False, forced_for_subagents=True)]))
    gw = healthy_gateway()
    assert serve(gw, hdrs(agent="ag1")).profile_id == "a"


def test_forced_subagents_works_for_a_codex_profile(pool_env):
    # A common setup is a codex account as the subagent target, so
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

    import claude_unlimited.activity as activity_module
    moves = [e for e in activity_module.list_events(limit=100) if "Agent moved A → B" in e.text]
    assert len(moves) == 2                    # one per agent that actually moved
    assert notified.count("rotated") == 1     # one per account, not per agent


def _count_body_parses(monkeypatch):
    """Every json.loads of the inbound body goes through _parsed_request, so
    counting it counts the parses. A conversation body can be megabytes."""
    calls = []
    real = gateway_module._parsed_request
    monkeypatch.setattr(gateway_module, "_parsed_request",
                        lambda body: calls.append(1) or real(body))
    return calls


def test_the_request_body_is_not_parsed_when_nothing_needs_it(pool_env, monkeypatch):
    """Identifying a branch JSON-parses the whole (possibly multi-MB) body;
    ordinary rotation must not pay for a key it would ignore. Routing no
    longer reads the requested model either — a spent Fable week moves the
    whole session, whatever the model — so plain rotation parses nothing,
    even with the leave-on-Fable switches on."""
    from claude_unlimited.config import Settings

    calls = _count_body_parses(monkeypatch)
    save_pool(Pool(profiles=[prof("a", leave_on_fable_limit=True), prof("b")],
                   settings=Settings(fable_limit_all_profiles=True)))
    gw = healthy_gateway()
    serve(gw, hdrs(agent="ag1"))
    assert calls == []


def test_the_body_is_parsed_at_most_once_per_request(pool_env, monkeypatch):
    """Branch pinning needs the session id out of the body — one parse, never
    one per reader."""
    calls = _count_body_parses(monkeypatch)
    save_pool(Pool(profiles=[prof("a"), prof("b")]))
    gw = healthy_gateway()

    serve(gw, hdrs(agent="ag1"), distribute=True)
    assert calls == [1], "branch pinning: one parse, not two"



# ---- "serving for" in the widget ------------------------------------------

def test_agents_on_account_reports_the_longest_serving_live_agent(pool_env, monkeypatch):
    save_pool(Pool(profiles=[prof("a")]))
    gw = healthy_gateway()
    start = real_time.monotonic()
    monkeypatch.setattr(gateway_module, "_pin_clock", lambda: start)
    serve(gw, hdrs(agent="ag1"), distribute=True)
    monkeypatch.setattr(gateway_module, "_pin_clock", lambda: start + 600)
    serve(gw, hdrs(agent="ag2"), distribute=True)   # a later agent must not shorten it
    monkeypatch.setattr(gateway_module, "_pin_clock", lambda: start + 900)
    assert round(gw.agents_on_account_seconds()["a"]) == 900


def test_agents_on_account_forgets_expired_pins(pool_env, monkeypatch):
    save_pool(Pool(profiles=[prof("a")]))
    gw = healthy_gateway()
    serve(gw, hdrs(agent="ag1"), distribute=True)
    later = real_time.monotonic() + gateway_module.BRANCH_PIN_TTL_SECONDS + 1
    monkeypatch.setattr(gateway_module, "_pin_clock", lambda: later)
    assert gw.agents_on_account_seconds() == {}


def test_a_repinned_agent_starts_its_time_on_the_new_account_from_zero(pool_env, monkeypatch):
    # 40 minutes on A, then A runs out: B must not claim those 40 minutes.
    save_pool(Pool(profiles=[prof("a"), prof("b")]))
    gw = healthy_gateway()
    start = real_time.monotonic()
    monkeypatch.setattr(gateway_module, "_pin_clock", lambda: start)
    first = serve(gw, hdrs(agent="ag1"), distribute=True).profile_id
    with gw._lock:
        gw._runtime[first].state = ProfileState.EXHAUSTED
    monkeypatch.setattr(gateway_module, "_pin_clock", lambda: start + 2400)
    second = serve(gw, hdrs(agent="ag1"), distribute=True).profile_id
    assert second != first
    monkeypatch.setattr(gateway_module, "_pin_clock", lambda: start + 2460)
    assert round(gw.agents_on_account_seconds()[second]) == 60


# ---- an app that is not Claude Code ------------------------------------------

def app_hdrs(session="cv-worker-1"):
    from claude_unlimited.project_attribution import APP_SESSION_HEADER
    return {APP_SESSION_HEADER: session}


def test_an_app_naming_its_session_keeps_one_account(pool_env):
    """A CV-screening app firing hundreds of concurrent requests used to ride
    the pool's single rotation pointer, so consecutive requests landed on
    different accounts and no provider-side prompt cache ever warmed."""
    save_pool(Pool(profiles=[prof("a"), prof("b")]))
    gw = healthy_gateway()
    first = serve(gw, app_hdrs()).profile_id
    for _ in range(5):
        assert serve(gw, app_hdrs()).profile_id == first


def test_two_workers_spread_across_accounts_and_each_stays_put(pool_env):
    save_pool(Pool(profiles=[prof("a"), prof("b")]))
    gw = healthy_gateway()
    one, two = serve(gw, app_hdrs("w1")).profile_id, serve(gw, app_hdrs("w2")).profile_id
    assert {one, two} == {"a", "b"}
    assert serve(gw, app_hdrs("w1")).profile_id == one
    assert serve(gw, app_hdrs("w2")).profile_id == two


def test_a_worker_still_rotates_when_its_account_stops_being_usable(pool_env):
    save_pool(Pool(profiles=[prof("a"), prof("b")]))
    gw = healthy_gateway()
    first = serve(gw, app_hdrs()).profile_id
    with gw._lock:
        gw._runtime[first].state = ProfileState.EXHAUSTED
    assert serve(gw, app_hdrs()).profile_id != first


def test_a_client_without_the_header_is_unchanged(pool_env):
    # No opt-in, no behaviour change: the shared sticky pointer, as before.
    save_pool(Pool(profiles=[prof("a"), prof("b")]))
    gw = healthy_gateway()
    assert serve(gw, {}).profile_id == "a"
    assert gw.branch_pins() == []


# ---- take over holds under concurrency ---------------------------------------

def test_take_over_holds_while_other_traffic_keeps_arriving(pool_env):
    """The reported bug: with hundreds of requests in flight, requests that had
    already chosen another account set the pointer back, so the pool flapped
    between accounts within the same second after a Take Over."""
    save_pool(Pool(profiles=[prof("a", priority=1), prof("b", priority=2)]))
    gw = healthy_gateway()
    assert serve(gw, {}).profile_id == "a"
    gw._current_profile_id = "a"

    assert gw.force_active("b") is True
    for _ in range(10):
        assert serve(gw, {}).profile_id == "b"
    # Even if something moves the shared pointer underneath it.
    gw._current_profile_id = "a"
    assert serve(gw, {}).profile_id == "b"


def test_a_taken_over_account_that_stops_being_usable_releases_the_override(pool_env):
    save_pool(Pool(profiles=[prof("a", priority=1), prof("b", priority=2)]))
    gw = healthy_gateway()
    gw.force_active("b")
    assert serve(gw, {}).profile_id == "b"
    with gw._lock:
        gw._runtime["b"].state = ProfileState.EXHAUSTED
    assert serve(gw, {}).profile_id == "a"
    assert gw._manual_profile_id is None, "a stuck override would strand the pool"


def test_taking_over_another_account_replaces_the_override(pool_env):
    save_pool(Pool(profiles=[prof("a"), prof("b")]))
    gw = healthy_gateway()
    gw.force_active("b")
    gw.force_active("a")
    assert serve(gw, {}).profile_id == "a"


# ---- "leave this profile when its Fable limit is spent" (issue #2) ----------

def _spend_fable_on(gw, profile_id, percent=100.0):
    from claude_unlimited.observation import ModelWindow

    with gw._lock:
        gw._runtime[profile_id].model_usage = (
            ModelWindow(name="Fable", percent=percent,
                        resets_at=datetime(2030, 1, 1, tzinfo=timezone.utc)),
        )


def _serve_model(gw, headers, model, **kw):
    body = ('{"model": "%s"}' % model).encode()
    result = gw.handle("POST", "/v1/messages", headers, body, **kw)
    if result.body_chunks:
        list(result.body_chunks)
    return result


def _leaving(pid, **kw):
    return prof(pid, leave_on_fable_limit=True, **kw)


def test_the_resolved_switch_survives_the_per_tick_runtime_rebuild(pool_env):
    """_sync_snapshot reconstructs ProfileRuntime field by field in TWO places
    (first seen, and every later tick). A field missing from either is
    silently dropped a second later — the trap that has already lost
    model_usage, the credit balance and blocked_models."""
    from claude_unlimited.config import Settings

    save_pool(Pool(profiles=[_leaving("a"), prof("b")], settings=Settings()))
    gw = healthy_gateway()
    for _ in range(3):        # several ticks, as an open dashboard would cause
        rts = gw.runtime_snapshot()
        assert rts["a"].leave_on_fable_limit is True
        assert rts["b"].leave_on_fable_limit is False

    # The global override resolves to True for a profile whose own flag is off,
    # and flipping it applies on the next tick, like any other configuration.
    save_pool(Pool(profiles=[_leaving("a"), prof("b")], settings=Settings(fable_limit_all_profiles=True)))
    for _ in range(3):
        rts = gw.runtime_snapshot()
        assert rts["b"].leave_on_fable_limit is True
    save_pool(Pool(profiles=[_leaving("a"), prof("b")], settings=Settings()))
    assert gw.runtime_snapshot()["b"].leave_on_fable_limit is False


def test_a_spent_fable_week_moves_the_whole_session_when_the_switch_is_on(pool_env):
    """Every model, not just Fable requests — an Opus turn leaves too."""
    save_pool(Pool(profiles=[_leaving("a", priority=1), _leaving("b", priority=2)]))
    gw = healthy_gateway()
    assert _serve_model(gw, hdrs(), "claude-opus-5").profile_id == "a"
    _spend_fable_on(gw, "a")

    assert _serve_model(gw, hdrs(), "claude-opus-5").profile_id == "b"
    assert _serve_model(gw, hdrs(), "claude-fable-5-1").profile_id == "b"
    # ...and it stays there (sticky on the new account), rather than bouncing.
    assert _serve_model(gw, hdrs(), "claude-sonnet-5").profile_id == "b"

    import claude_unlimited.activity as activity_module
    texts = [e.text for e in activity_module.list_events(limit=50)]
    assert any("A has spent its Fable weekly limit — handed over to B" in t for t in texts)
    assert not any(t.startswith("Rotated A") for t in texts), "it did not run out; its Fable week did"


def test_with_the_switch_off_a_spent_fable_week_changes_nothing(pool_env):
    save_pool(Pool(profiles=[prof("a", priority=1), prof("b", priority=2)]))
    gw = healthy_gateway()
    assert _serve_model(gw, hdrs(), "claude-opus-5").profile_id == "a"
    _spend_fable_on(gw, "a")
    assert _serve_model(gw, hdrs(), "claude-fable-5-1").profile_id == "a"
    assert _serve_model(gw, hdrs(), "claude-opus-5").profile_id == "a"


def test_the_global_override_makes_a_profile_leave_whose_own_switch_is_off(pool_env):
    from claude_unlimited.config import Settings

    save_pool(Pool(profiles=[prof("a", priority=1), prof("b", priority=2)],
                   settings=Settings(fable_limit_all_profiles=True)))
    gw = healthy_gateway()
    assert _serve_model(gw, hdrs(), "claude-opus-5").profile_id == "a"
    _spend_fable_on(gw, "a")
    assert _serve_model(gw, hdrs(), "claude-opus-5").profile_id == "b"


def test_a_pinned_branch_MOVES_when_its_account_has_spent_its_fable_week(pool_env):
    """Invariant 4: a branch pin must not silently defeat the feature. The pin
    itself moves, so the branch's cache follows its work to the new account
    instead of one request being diverted and the next bouncing back."""
    save_pool(Pool(profiles=[_leaving("a", priority=1), _leaving("b", priority=2)]))
    gw = healthy_gateway()

    first = _serve_model(gw, hdrs(agent="ag1"), "claude-fable-5-1", distribute=True)
    _spend_fable_on(gw, first.profile_id)

    moved = _serve_model(gw, hdrs(agent="ag1"), "claude-opus-5", distribute=True)
    assert moved.profile_id != first.profile_id

    # The pin moved, not just this request: the next turn stays on the new
    # account rather than bouncing back to the spent one.
    again = _serve_model(gw, hdrs(agent="ag1"), "claude-fable-5-1", distribute=True)
    assert again.profile_id == moved.profile_id

    import claude_unlimited.activity as activity_module
    moves = [e for e in activity_module.list_events(limit=50) if e.text.startswith("Agent moved")]
    assert moves and "has spent its Fable weekly limit" in (moves[0].meta or "")


def test_a_pinned_branch_does_not_move_when_nowhere_else_can_take_it(pool_env):
    """Giving up a warm cache buys nothing if the new account must equally be
    left."""
    save_pool(Pool(profiles=[_leaving("a", priority=1), _leaving("b", priority=2)]))
    gw = healthy_gateway()

    first = _serve_model(gw, hdrs(agent="ag1"), "claude-fable-5-1", distribute=True)
    _spend_fable_on(gw, "a")
    _spend_fable_on(gw, "b")

    assert _serve_model(gw, hdrs(agent="ag1"), "claude-fable-5-1",
                        distribute=True).profile_id == first.profile_id


def test_a_pinned_branch_stays_when_the_spent_account_has_its_switch_off(pool_env):
    save_pool(Pool(profiles=[prof("a", priority=1), prof("b", priority=2)]))
    gw = healthy_gateway()

    first = _serve_model(gw, hdrs(agent="ag1"), "claude-fable-5-1", distribute=True)
    _spend_fable_on(gw, first.profile_id)

    assert _serve_model(gw, hdrs(agent="ag1"), "claude-fable-5-1",
                        distribute=True).profile_id == first.profile_id


def test_an_explicit_profile_pin_is_honoured_even_when_that_account_must_leave(pool_env):
    """The user named one account. Silently serving a
    different one is exactly what pinning exists to prevent. One Activity
    line says why the session may start failing on Fable — not one per turn."""
    save_pool(Pool(profiles=[_leaving("a", priority=1), _leaving("b", priority=2)]))
    gw = healthy_gateway()
    _serve_model(gw, hdrs(), "claude-fable-5-1")   # prime the runtime
    _spend_fable_on(gw, "a")

    for _ in range(3):
        result = _serve_model(gw, hdrs(), "claude-fable-5-1", forced_profile_id="a")
        assert result.profile_id == "a"

    import claude_unlimited.activity as activity_module
    notes = [e for e in activity_module.list_events(limit=50)
             if e.text == "A has spent its Fable weekly limit"]
    assert len(notes) == 1
    assert "pinned session" in (notes[0].meta or "")


def test_take_over_is_honoured_even_when_that_account_must_leave(pool_env):
    save_pool(Pool(profiles=[_leaving("a", priority=1), _leaving("b", priority=2)]))
    gw = healthy_gateway()
    _serve_model(gw, hdrs(), "claude-opus-5")
    assert gw.force_active("a") is True
    _spend_fable_on(gw, "a")

    for _ in range(3):
        assert _serve_model(gw, hdrs(), "claude-opus-5").profile_id == "a"
    assert gw._manual_profile_id == "a", "a spent Fable week is not 'stopped being usable'"

    import claude_unlimited.activity as activity_module
    notes = [e for e in activity_module.list_events(limit=50)
             if e.text == "A has spent its Fable weekly limit"]
    assert len(notes) == 1
    assert "Take over" in (notes[0].meta or "")


def test_a_forced_subagent_profile_keeps_its_subagents_when_it_must_leave(pool_env):
    """Same rule as a --profile pin: "forced in subagents" falls back only
    when that account cannot serve at all, and a spent Fable week is not
    that."""
    save_pool(Pool(profiles=[prof("main", priority=1),
                             _leaving("subs", priority=9, forced_for_subagents=True)]))
    gw = healthy_gateway()
    assert _serve_model(gw, hdrs(agent="ag1"), "claude-opus-5").profile_id == "subs"
    _spend_fable_on(gw, "subs")
    assert _serve_model(gw, hdrs(agent="ag1"), "claude-opus-5").profile_id == "subs"
    assert _serve_model(gw, hdrs(agent="ag2"), "claude-fable-5-1").profile_id == "subs"


def test_with_no_alternative_the_session_stays_and_is_served_anyway(pool_env):
    save_pool(Pool(profiles=[_leaving("a", priority=1), _leaving("b", priority=2)]))
    gw = healthy_gateway()
    assert _serve_model(gw, hdrs(), "claude-opus-5").profile_id == "a"
    _spend_fable_on(gw, "a")
    _spend_fable_on(gw, "b")
    for _ in range(3):
        assert _serve_model(gw, hdrs(), "claude-opus-5").profile_id == "a"

    import claude_unlimited.activity as activity_module
    notes = [e for e in activity_module.list_events(limit=50)
             if e.text == "A has spent its Fable weekly limit"]
    assert len(notes) == 1
    assert "no other account" in (notes[0].meta or "")


def test_a_spent_account_comes_back_when_its_bucket_shows_room_again(pool_env):
    save_pool(Pool(profiles=[_leaving("a", priority=1), _leaving("b", priority=2)]))
    gw = healthy_gateway()
    assert _serve_model(gw, hdrs(), "claude-opus-5").profile_id == "a"
    _spend_fable_on(gw, "a")
    assert _serve_model(gw, hdrs(), "claude-opus-5").profile_id == "b"
    _spend_fable_on(gw, "a", percent=5.0)
    # Sticky on "b" while it works — but "a" is a candidate again, so when
    # "b" must leave, the session comes back to it instead of staying put.
    _spend_fable_on(gw, "b")
    assert _serve_model(gw, hdrs(), "claude-opus-5").profile_id == "a"


def test_the_pinned_session_note_is_repeated_for_the_next_spent_week(pool_env):
    save_pool(Pool(profiles=[_leaving("a", priority=1), _leaving("b", priority=2)]))
    gw = healthy_gateway()
    _serve_model(gw, hdrs(), "claude-opus-5")
    _spend_fable_on(gw, "a")
    _serve_model(gw, hdrs(), "claude-opus-5", forced_profile_id="a")
    _spend_fable_on(gw, "a", percent=3.0)       # the week reset
    _serve_model(gw, hdrs(), "claude-opus-5", forced_profile_id="a")
    _spend_fable_on(gw, "a")                    # spent again
    _serve_model(gw, hdrs(), "claude-opus-5", forced_profile_id="a")
    _serve_model(gw, hdrs(), "claude-opus-5", forced_profile_id="a")

    import claude_unlimited.activity as activity_module
    notes = [e for e in activity_module.list_events(limit=50)
             if e.text == "A has spent its Fable weekly limit"]
    assert len(notes) == 2
