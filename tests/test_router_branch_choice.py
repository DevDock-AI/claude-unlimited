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


def test_priority_decides_who_goes_first_when_nothing_is_pinned_yet():
    # With no branches assigned, the counts are all 0 and the user's
    # preference order picks the opener: priority 1 at 90% still opens ahead
    # of priority 2 at 0%.
    decision = choose_for_new_branch(pool(p("hi", priority=1, usage=90.0),
                                          p("lo", priority=2, usage=0.0)), NOW)
    assert decision.profile_id == "hi"


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


def test_the_least_loaded_account_wins_however_lopsided_the_pool_is():
    # Two accounts must ALTERNATE, not both take every branch — and that has to
    # hold when they differ in usage and priority, not only when they're twins.
    snapshot = pool(p("a", priority=1, usage=20.0), p("b", priority=2, usage=80.0))
    assert choose_for_new_branch(snapshot, NOW, {"a": 2, "b": 0}).profile_id == "b"
    assert choose_for_new_branch(snapshot, NOW, {"a": 0, "b": 2}).profile_id == "a"


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
