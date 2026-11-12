"""router.choose_for_new_branch — the selector that assigns an account to a NEW
conversation branch (main agent or a subagent) in distribute mode.

Pure-function tests: no Gateway, no clock beyond the passed `now`, no I/O.
Its job is to SPREAD branches — fewest-pinned first, then priority order
within a round — which is what stops one account being drained while another
sits idle.
"""

from datetime import datetime, timedelta, timezone

from claude_unlimited.router import (
    PoolSnapshot,
    ProfileRuntime,
    ProfileState,
    choose_for_new_branch,
)

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def p(pid, *, priority=1, usage=None, state=ProfileState.ELIGIBLE, automatic=True, resets_at=None):
    return ProfileRuntime(
        profile_id=pid, priority=priority, switch_threshold=98.0, automatic=automatic,
        state=state, last_usage_percent=usage, resets_at=resets_at,
    )


def pool(*profiles):
    return PoolSnapshot(profiles=list(profiles))


def test_picks_the_least_utilized_account():
    # The whole point: don't pile onto the one that's already half spent.
    decision = choose_for_new_branch(pool(p("a", usage=80.0), p("b", usage=10.0), p("c", usage=45.0)), NOW)
    assert decision.profile_id == "b"
    assert decision.reason == "branch_assigned"


def test_usage_not_priority_picks_the_opener_in_distribute_mode():
    # Distribute mode is for spending the pool evenly: priority 2 at 0% opens
    # ahead of priority 1 at 90%.
    decision = choose_for_new_branch(pool(p("hi", priority=1, usage=90.0),
                                          p("lo", priority=2, usage=0.0)), NOW)
    assert decision.profile_id == "lo"


def test_priority_still_breaks_a_tie_between_accounts_usage_cannot_tell_apart():
    decision = choose_for_new_branch(pool(p("second", priority=2), p("first", priority=1)), NOW)
    assert decision.profile_id == "first"


def test_branch_count_outranks_priority_so_branches_actually_spread():
    """The regression that mattered: with `priority` ahead of the branch count,
    every branch of a session picked the same winner — the flag promised "each
    subagent gets its own account" and delivered one account for all of them.
    A pool with DISTINCT priorities is the normal case, so this is the case the
    ordering has to survive."""
    snapshot = pool(p("first", priority=1, usage=10.0),
                    p("second", priority=2, usage=20.0),
                    p("third", priority=3, usage=30.0))
    counts: dict = {}
    picks = []
    for _ in range(3):
        pid = choose_for_new_branch(snapshot, NOW, counts).profile_id
        counts[pid] = counts.get(pid, 0) + 1
        picks.append(pid)
    # One branch each, in priority order — not three copies of "first".
    assert picks == ["first", "second", "third"]


def test_a_fourth_branch_wraps_back_round_in_priority_order():
    snapshot = pool(p("first", priority=1), p("second", priority=2))
    counts = {"first": 1, "second": 1}
    assert choose_for_new_branch(snapshot, NOW, counts).profile_id == "first"


def test_priority_still_orders_accounts_that_carry_the_same_load():
    snapshot = pool(p("lo", priority=5), p("hi", priority=1))
    assert choose_for_new_branch(snapshot, NOW, {"lo": 1, "hi": 1}).profile_id == "hi"


def test_a_never_observed_account_is_used_before_a_part_spent_one():
    # usage None -> 0.0, so a fresh account is preferred.
    decision = choose_for_new_branch(pool(p("used", usage=30.0), p("fresh", usage=None)), NOW)
    assert decision.profile_id == "fresh"


def test_twins_alternate_and_a_lopsided_pool_still_never_starves_an_account():
    # Two identical accounts must ALTERNATE, not both take every branch.
    twins = pool(p("a", usage=20.0), p("b", usage=20.0))
    assert choose_for_new_branch(twins, NOW, {"a": 1, "b": 0}).profile_id == "b"
    assert choose_for_new_branch(twins, NOW, {"a": 0, "b": 1}).profile_id == "a"
    # A busier one takes fewer, but load keeps growing on the winner, so the
    # other is picked once its share is used up.
    lopsided = pool(p("a", priority=1, usage=20.0), p("b", priority=2, usage=80.0))
    assert choose_for_new_branch(lopsided, NOW, {"a": 2, "b": 0}).profile_id == "a"
    assert choose_for_new_branch(lopsided, NOW, {"a": 5, "b": 0}).profile_id == "b"


def test_excluded_accounts_are_skipped_so_failover_moves_on():
    # `exclude` carries what already failed this request; the re-pin must land
    # somewhere genuinely different.
    snapshot = pool(p("a", usage=5.0), p("b", usage=50.0))
    assert choose_for_new_branch(snapshot, NOW).profile_id == "a"
    assert choose_for_new_branch(snapshot, NOW, exclude=frozenset({"a"})).profile_id == "b"


def test_only_eligible_accounts_are_candidates():
    for bad in (ProfileState.DRAINING, ProfileState.EXHAUSTED, ProfileState.COOLDOWN,
                ProfileState.AUTH_INVALID, ProfileState.DISABLED):
        decision = choose_for_new_branch(pool(p("bad", usage=0.0, state=bad), p("ok", usage=90.0)), NOW)
        assert decision.profile_id == "ok", bad


def test_a_manual_only_profile_is_never_auto_assigned_to_a_branch():
    # `automatic=False` means "only reachable by an explicit switch" — a branch
    # assignment is automatic by definition, so it must not be picked.
    decision = choose_for_new_branch(pool(p("manual", usage=0.0, automatic=False),
                                          p("auto", usage=70.0)), NOW)
    assert decision.profile_id == "auto"


def test_no_candidates_reports_no_eligible_profile():
    decision = choose_for_new_branch(pool(p("x", state=ProfileState.EXHAUSTED)), NOW)
    assert decision.profile_id is None
    assert decision.reason == "no_eligible_profile"
    # ...and excluding everything is the same story.
    assert choose_for_new_branch(pool(p("x")), NOW, exclude=frozenset({"x"})).profile_id is None


def test_utilization_still_decides_between_equal_load_and_equal_priority():
    snapshot = pool(p("spent", priority=1, usage=70.0), p("fresh", priority=1, usage=5.0))
    assert choose_for_new_branch(snapshot, NOW, {"spent": 1, "fresh": 1}).profile_id == "fresh"


def test_load_spreading_never_reaches_an_ineligible_account():
    """Count-first must not drag in an account rotation has taken out of
    service: a fully-loaded eligible account still beats an idle exhausted
    one."""
    snapshot = pool(p("busy", priority=1, usage=50.0),
                    p("gone", priority=1, usage=0.0, state=ProfileState.EXHAUSTED))
    assert choose_for_new_branch(snapshot, NOW, {"busy": 9}).profile_id == "busy"


def test_sooner_reset_wins_when_usage_and_load_are_equal():
    soon = NOW + timedelta(minutes=5)
    later = NOW + timedelta(hours=3)
    decision = choose_for_new_branch(pool(p("later", usage=10.0, resets_at=later),
                                          p("soon", usage=10.0, resets_at=soon)), NOW)
    assert decision.profile_id == "soon"


def test_fully_tied_candidates_are_deterministic():
    # Same band, same usage, same load, no reset info: still stable, so the
    # same pool never flip-flops between two identical accounts.
    snapshot = pool(p("b", usage=10.0), p("a", usage=10.0))
    picks = {choose_for_new_branch(snapshot, NOW).profile_id for _ in range(5)}
    assert picks == {"a"}


def test_spreading_two_branches_across_two_equal_accounts():
    # End-to-end of the intent: assign branch 1, record it, assign branch 2 ->
    # they land on different accounts.
    snapshot = pool(p("a", usage=0.0), p("b", usage=0.0))
    counts: dict = {}
    first = choose_for_new_branch(snapshot, NOW, counts).profile_id
    counts[first] = counts.get(first, 0) + 1
    second = choose_for_new_branch(snapshot, NOW, counts).profile_id
    assert first != second


def weekly(pid, percent_7d, resets_in_hours, *, usage=None, priority=1):
    return ProfileRuntime(
        profile_id=pid, priority=priority, switch_threshold=98.0, automatic=True,
        last_usage_percent=usage, last_usage_percent_7d=percent_7d,
        resets_at_7d=NOW + timedelta(hours=resets_in_hours),
    )


def test_same_weekly_usage_prefers_the_week_that_resets_sooner():
    # Both half spent: the one resetting tomorrow would waste its leftover,
    # the one resetting in three days can wait.
    decision = choose_for_new_branch(pool(weekly("later", 50.0, 72), weekly("tomorrow", 50.0, 24)), NOW)
    assert decision.profile_id == "tomorrow"


def test_an_account_nearly_out_for_the_week_goes_last_despite_a_quiet_5h_window():
    decision = choose_for_new_branch(pool(
        weekly("almost_out", 95.0, 96, usage=5.0),
        weekly("roomy", 40.0, 96, usage=60.0),
    ), NOW)
    assert decision.profile_id == "roomy"


def test_weekly_only_accounts_are_ranked_by_their_primary_window():
    soon = ProfileRuntime(profile_id="soon", priority=1, switch_threshold=98.0, automatic=True,
                          last_usage_percent=50.0, resets_at=NOW + timedelta(hours=24), window_label="weekly")
    later = ProfileRuntime(profile_id="later", priority=1, switch_threshold=98.0, automatic=True,
                           last_usage_percent=50.0, resets_at=NOW + timedelta(hours=120), window_label="weekly")
    assert choose_for_new_branch(pool(later, soon), NOW).profile_id == "soon"


def test_near_equal_weekly_pressure_falls_through_to_the_5h_window():
    decision = choose_for_new_branch(pool(
        weekly("busy_5h", 50.0, 100, usage=70.0),
        weekly("quiet_5h", 50.0, 101, usage=10.0),
    ), NOW)
    assert decision.profile_id == "quiet_5h"


def test_load_follows_weekly_pressure_in_proportion():
    # Half the week left, resetting in 24h (~2.1 %/h) vs 72h (~0.7 %/h): the
    # urgent account takes about three branches for every one the other does.
    profiles = pool(weekly("tomorrow", 50.0, 24, priority=2), weekly("later", 50.0, 72, priority=1))
    counts = {"tomorrow": 0, "later": 0}
    for _ in range(12):
        counts[choose_for_new_branch(profiles, NOW, branch_counts=counts).profile_id] += 1
    assert counts == {"tomorrow": 9, "later": 3}


def test_an_account_about_to_hit_its_5h_limit_takes_less_however_urgent_its_week():
    profiles = pool(weekly("urgent_but_5h_full", 50.0, 24, usage=95.0), weekly("calm", 30.0, 120, usage=10.0))
    assert choose_for_new_branch(profiles, NOW).profile_id == "calm"


def test_branch_shares_sum_to_one_and_follow_weekly_pressure():
    from claude_unlimited.router import branch_shares
    shares = branch_shares(pool(weekly("tomorrow", 50.0, 24), weekly("later", 50.0, 72),
                                p("off", state=ProfileState.EXHAUSTED)), NOW)
    assert set(shares) == {"tomorrow", "later"}
    assert abs(sum(shares.values()) - 1.0) < 1e-9
    assert abs(shares["tomorrow"] - 0.75) < 0.01


def _rt(pid, **kw):
    kw.setdefault("priority", 1)
    kw.setdefault("switch_threshold", 98.0)
    kw.setdefault("automatic", True)
    return ProfileRuntime(profile_id=pid, **kw)


def _place(snapshot, n):
    counts = {}
    for _ in range(n):
        pid = choose_for_new_branch(snapshot, NOW, branch_counts=dict(counts)).profile_id
        counts[pid] = counts.get(pid, 0) + 1
    return counts


def test_accounts_without_a_weekly_window_take_only_a_small_share():
    # A metered API key, and a Codex plan whose only window is 5h, must not
    # out-draw a part-spent subscription: nothing of theirs expires weekly.
    from claude_unlimited.router import branch_shares
    snapshot = pool(
        _rt("sub", kind="oauth", last_usage_percent_7d=40.0, resets_at_7d=NOW + timedelta(days=4)),
        _rt("key", kind="api"),
        _rt("codex_5h", kind="codex", last_usage_percent=20.0, window_label="5h",
            resets_at=NOW + timedelta(hours=3)),
    )
    shares = branch_shares(snapshot, NOW)
    assert shares["sub"] > 0.6
    assert shares["key"] < 0.2 and shares["codex_5h"] < 0.2
    counts = _place(snapshot, 10)
    assert counts["sub"] > counts.get("key", 0) + counts.get("codex_5h", 0)


def test_an_unobserved_subscription_counts_as_a_fresh_week():
    shares = __import__("claude_unlimited.router", fromlist=["branch_shares"]).branch_shares(
        pool(_rt("new_sub", kind="oauth"), _rt("key", kind="api")), NOW)
    assert shares["new_sub"] > shares["key"]


def test_a_reset_within_the_hour_cannot_take_the_whole_fan_out():
    # Uncapped, 50% left in 1h weighs ~50 %/h against ~0.4: every subagent
    # would land on it and move again once its 5h window drains.
    snapshot = pool(
        weekly("imminent", 50.0, 1),
        weekly("b", 40.0, 140), weekly("c", 40.0, 150), weekly("d", 45.0, 130),
    )
    counts = _place(snapshot, 12)
    assert counts["imminent"] <= 7
    assert all(counts.get(pid, 0) >= 1 for pid in ("b", "c", "d"))


def test_readings_past_their_reset_count_as_refilled():
    # Both stale: the 5h reading and the weekly one already reset, so this
    # account weighs like a fresh one, not a nearly-spent one.
    stale = _rt("stale", last_usage_percent=96.0, resets_at=NOW - timedelta(minutes=5),
                last_usage_percent_7d=97.0, resets_at_7d=NOW - timedelta(hours=1))
    fresh = _rt("fresh")
    from claude_unlimited.router import branch_shares
    shares = branch_shares(pool(stale, fresh), NOW)
    assert abs(shares["stale"] - shares["fresh"]) < 1e-9


def test_non_finite_usage_never_reaches_the_shares():
    import json
    import math
    from claude_unlimited.router import branch_shares
    odd = _rt("odd", last_usage_percent=float("nan"), last_usage_percent_7d=float("inf"),
              resets_at_7d=NOW + timedelta(days=2))
    shares = branch_shares(pool(odd, _rt("ok")), NOW)
    assert all(math.isfinite(v) for v in shares.values())
    json.dumps(shares, allow_nan=False)


def test_a_weekly_only_account_uses_its_switch_threshold_as_the_ceiling():
    from claude_unlimited.router import _weekly_spend_rate
    acct = _rt("w", switch_threshold=90.0, last_usage_percent=60.0, window_label="weekly",
               resets_at=NOW + timedelta(hours=30))
    assert abs(_weekly_spend_rate(acct, NOW) - 1.0) < 1e-9  # (90 - 60) / 30h


def test_5h_scaling_skips_daily_and_monthly_primary_windows():
    from claude_unlimited.router import _five_hour_room
    daily = _rt("d", kind="codex", last_usage_percent=90.0, window_label="daily",
                resets_at=NOW + timedelta(hours=10))
    five = _rt("f", kind="codex", last_usage_percent=90.0, window_label="5h",
               resets_at=NOW + timedelta(hours=2))
    assert _five_hour_room(daily, NOW) == 1.0
    assert _five_hour_room(five, NOW) < 0.1


def test_an_api_key_never_outweighs_a_subscription_low_on_5h_room():
    from claude_unlimited.router import _branch_weights
    sub = _rt("sub", kind="oauth", last_usage_percent=95.0, window_label="5h",
              resets_at=NOW + timedelta(hours=2),
              last_usage_percent_7d=40.0, resets_at_7d=NOW + timedelta(days=4))
    key = _rt("key", kind="api")
    weights = _branch_weights([sub, key], NOW)
    assert weights["key"] <= weights["sub"]


def test_a_two_account_pool_is_capped():
    from claude_unlimited.router import branch_shares
    shares = branch_shares(pool(weekly("imminent", 50.0, 1), weekly("b", 40.0, 140)), NOW)
    assert abs(shares["imminent"] - 0.75) < 0.01
    assert abs(shares["b"] - 0.25) < 0.01
