from datetime import datetime, timedelta, timezone

from claude_unlimited.observation import (
    AuthInvalid,
    ProviderUnavailable,
    QuotaExhausted,
    ShortRateLimit,
    UsageSnapshot,
)
from claude_unlimited.router import (
    PoolSnapshot,
    ProfileRuntime,
    ProfileState,
    RequestFit,
    choose,
    choose_for_new_branch,
    fable_spent,
    fits,
    must_leave,
    observe,
    recover_expired_cooldowns,
)

NOW = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)


def rt(profile_id, priority=1, automatic=True, state=ProfileState.ELIGIBLE, **kw) -> ProfileRuntime:
    return ProfileRuntime(profile_id=profile_id, priority=priority, switch_threshold=98.0,
                           automatic=automatic, state=state, **kw)


def test_sticky_stays_on_current_profile_while_eligible():
    pool = PoolSnapshot(profiles=[rt("a"), rt("b")], current_profile_id="a")
    decision = choose(pool, NOW)
    assert decision.profile_id == "a"
    assert decision.reason == "sticky"


def test_rotates_away_from_draining_current_profile():
    pool = PoolSnapshot(profiles=[rt("a", priority=1, state=ProfileState.DRAINING), rt("b", priority=2)],
                         current_profile_id="a")
    decision = choose(pool, NOW)
    assert decision.profile_id == "b"
    assert decision.reason == "rotated"


def test_tie_break_by_priority_lower_wins():
    pool = PoolSnapshot(profiles=[rt("a", priority=3), rt("b", priority=1)], current_profile_id=None)
    decision = choose(pool, NOW)
    assert decision.profile_id == "b"


def test_tie_break_by_soonest_reset_within_same_priority():
    soon = NOW + timedelta(hours=1)
    later = NOW + timedelta(hours=5)
    pool = PoolSnapshot(profiles=[
        rt("a", priority=1, resets_at=later),
        rt("b", priority=1, resets_at=soon),
    ], current_profile_id=None)
    decision = choose(pool, NOW)
    assert decision.profile_id == "b"


def test_no_eligible_profile_returns_none_not_a_crash():
    pool = PoolSnapshot(profiles=[
        rt("a", state=ProfileState.EXHAUSTED),
        rt("b", state=ProfileState.DISABLED),
    ], current_profile_id="a")
    decision = choose(pool, NOW)
    assert decision.profile_id is None
    assert decision.reason == "no_eligible_profile"


def test_non_automatic_profile_only_chosen_if_already_current():
    pool = PoolSnapshot(profiles=[rt("a", automatic=False)], current_profile_id=None)
    assert choose(pool, NOW).profile_id is None

    pool2 = PoolSnapshot(profiles=[rt("a", automatic=False)], current_profile_id="a")
    assert choose(pool2, NOW).profile_id == "a"


def test_usage_snapshot_below_threshold_stays_eligible():
    pool = PoolSnapshot(profiles=[rt("a")], current_profile_id="a")
    new_pool = observe(pool, "a", UsageSnapshot(percent=40.0, resets_at=None, confidence="measured"), NOW)
    assert new_pool.profiles[0].state == ProfileState.ELIGIBLE
    assert new_pool.profiles[0].last_usage_percent == 40.0


def test_usage_snapshot_at_or_above_threshold_goes_draining():
    pool = PoolSnapshot(profiles=[rt("a")], current_profile_id="a")
    new_pool = observe(pool, "a", UsageSnapshot(percent=98.0, resets_at=None, confidence="measured"), NOW)
    assert new_pool.profiles[0].state == ProfileState.DRAINING


def test_quota_exhausted_observation_sets_exhausted_state():
    pool = PoolSnapshot(profiles=[rt("a")], current_profile_id="a")
    resets = NOW + timedelta(hours=2)
    new_pool = observe(pool, "a", QuotaExhausted(resets_at=resets), NOW)
    assert new_pool.profiles[0].state == ProfileState.EXHAUSTED
    assert new_pool.profiles[0].resets_at == resets


def test_short_rate_limit_never_produces_exhausted_or_draining():
    pool = PoolSnapshot(profiles=[rt("a")], current_profile_id="a")
    new_pool = observe(pool, "a", ShortRateLimit(retry_after_seconds=5.0), NOW)
    assert new_pool.profiles[0].state == ProfileState.COOLDOWN
    assert new_pool.profiles[0].state not in (ProfileState.EXHAUSTED, ProfileState.DRAINING)


def test_provider_unavailable_is_cooldown_not_quota_rotation():
    pool = PoolSnapshot(profiles=[rt("a")], current_profile_id="a")
    new_pool = observe(pool, "a", ProviderUnavailable(retry_after_seconds=None), NOW)
    assert new_pool.profiles[0].state == ProfileState.COOLDOWN


def test_a_real_long_retry_after_is_honored_not_clamped_to_60s():
    # Clamping Retry-After to a flat 60s would return a Profile to ELIGIBLE,
    # and let it take another request, before Anthropic's window has closed.
    pool = PoolSnapshot(profiles=[rt("a")], current_profile_id="a")
    new_pool = observe(pool, "a", ShortRateLimit(retry_after_seconds=300.0), NOW)
    assert new_pool.profiles[0].cooldown_until == NOW + timedelta(seconds=300)


def test_absurd_retry_after_is_still_capped_defensively():
    pool = PoolSnapshot(profiles=[rt("a")], current_profile_id="a")
    new_pool = observe(pool, "a", ShortRateLimit(retry_after_seconds=999_999.0), NOW)
    assert new_pool.profiles[0].cooldown_until == NOW + timedelta(seconds=1800)


def test_repeated_failures_with_no_retry_after_escalate_the_cooldown():
    # Anthropic sends no Retry-After on a spend-cap/billing 429, which "keeps
    # failing until access resumes". A flat 30s default would retry a stuck
    # Profile every 30s forever, so the cooldown must escalate instead.
    pool = PoolSnapshot(profiles=[rt("a")], current_profile_id="a")

    pool = observe(pool, "a", ShortRateLimit(retry_after_seconds=None), NOW)
    assert pool.profiles[0].cooldown_until == NOW + timedelta(seconds=30)
    assert pool.profiles[0].consecutive_unretryable_failures == 1

    pool = observe(pool, "a", ShortRateLimit(retry_after_seconds=None), NOW)
    assert pool.profiles[0].cooldown_until == NOW + timedelta(seconds=60)
    assert pool.profiles[0].consecutive_unretryable_failures == 2

    pool = observe(pool, "a", ShortRateLimit(retry_after_seconds=None), NOW)
    assert pool.profiles[0].cooldown_until == NOW + timedelta(seconds=120)
    assert pool.profiles[0].consecutive_unretryable_failures == 3

    # A success resets the streak entirely: the Profile works again.
    pool = observe(pool, "a", UsageSnapshot(percent=10.0, resets_at=None, confidence="measured"), NOW)
    assert pool.profiles[0].consecutive_unretryable_failures == 0

    pool = observe(pool, "a", ShortRateLimit(retry_after_seconds=None), NOW)
    assert pool.profiles[0].cooldown_until == NOW + timedelta(seconds=30)  # back to the start, not still escalated


def test_no_retry_after_streak_eventually_hits_the_same_defensive_ceiling():
    pool = PoolSnapshot(profiles=[rt("a")], current_profile_id="a")
    for _ in range(10):
        pool = observe(pool, "a", ShortRateLimit(retry_after_seconds=None), NOW)
    assert pool.profiles[0].cooldown_until == NOW + timedelta(seconds=1800)


def test_a_real_retry_after_resets_the_no_retry_after_streak():
    # A Retry-After header is a trustworthy signal, so it must not inherit an
    # escalated streak built from earlier headerless failures.
    pool = PoolSnapshot(profiles=[rt("a")], current_profile_id="a")
    pool = observe(pool, "a", ShortRateLimit(retry_after_seconds=None), NOW)
    pool = observe(pool, "a", ShortRateLimit(retry_after_seconds=None), NOW)
    assert pool.profiles[0].consecutive_unretryable_failures == 2

    pool = observe(pool, "a", ShortRateLimit(retry_after_seconds=5.0), NOW)
    assert pool.profiles[0].consecutive_unretryable_failures == 0
    assert pool.profiles[0].cooldown_until == NOW + timedelta(seconds=5)


def test_auth_invalid_observation_sets_auth_invalid_state():
    pool = PoolSnapshot(profiles=[rt("a")], current_profile_id="a")
    new_pool = observe(pool, "a", AuthInvalid(), NOW)
    assert new_pool.profiles[0].state == ProfileState.AUTH_INVALID


def test_observe_does_not_mutate_input_snapshot():
    pool = PoolSnapshot(profiles=[rt("a")], current_profile_id="a")
    observe(pool, "a", QuotaExhausted(resets_at=None), NOW)
    assert pool.profiles[0].state == ProfileState.ELIGIBLE


def test_recover_expired_cooldown_returns_to_eligible():
    past = NOW - timedelta(seconds=1)
    pool = PoolSnapshot(profiles=[rt("a", state=ProfileState.COOLDOWN, cooldown_until=past)],
                         current_profile_id="a")
    recovered = recover_expired_cooldowns(pool, NOW)
    assert recovered.profiles[0].state == ProfileState.ELIGIBLE


def test_recover_leaves_unexpired_cooldown_alone():
    future = NOW + timedelta(seconds=30)
    pool = PoolSnapshot(profiles=[rt("a", state=ProfileState.COOLDOWN, cooldown_until=future)],
                         current_profile_id="a")
    recovered = recover_expired_cooldowns(pool, NOW)
    assert recovered.profiles[0].state == ProfileState.COOLDOWN


def test_recover_expired_exhausted_clears_usage_percent():
    past = NOW - timedelta(seconds=1)
    pool = PoolSnapshot(profiles=[rt("a", state=ProfileState.EXHAUSTED, resets_at=past, last_usage_percent=99.0)],
                         current_profile_id="a")
    recovered = recover_expired_cooldowns(pool, NOW)
    assert recovered.profiles[0].state == ProfileState.ELIGIBLE
    assert recovered.profiles[0].last_usage_percent is None


# ---------------------------------------------------------------------------
# Issue #2 (reworked) — "leave this profile when its Fable limit is spent".
#
# Per profile, off by default. While the resolved switch is on and the Fable
# bucket is spent, the WHOLE session leaves the account — every model — and
# the account is not a candidate until the bucket resets. Derived per request,
# never written into `state`.
# ---------------------------------------------------------------------------

def _rt_with_fable(pid, priority, percent, *, threshold=98.0, resets_at=None,
                   state=None, leave=True):
    from claude_unlimited.observation import ModelWindow

    rt = ProfileRuntime(profile_id=pid, priority=priority, switch_threshold=threshold,
                        automatic=True, state=state or ProfileState.ELIGIBLE,
                        leave_on_fable_limit=leave)
    rt.model_usage = (ModelWindow(name="Fable", percent=percent, resets_at=resets_at),)
    return rt


LATER = datetime(2030, 1, 1, tzinfo=timezone.utc)
EARLIER = datetime(2020, 1, 1, tzinfo=timezone.utc)
NOW_ = datetime(2026, 9, 20, tzinfo=timezone.utc)


def test_fable_spent_reads_the_fable_window_only():
    spent = _rt_with_fable("a", 1, 100.0, resets_at=LATER)
    assert fable_spent(spent, NOW_) is True
    # Not a state change: the account itself is fine.
    assert spent.state == ProfileState.ELIGIBLE

    from claude_unlimited.observation import ModelWindow
    other = ProfileRuntime(profile_id="b", priority=1, switch_threshold=98.0, automatic=True)
    other.model_usage = (ModelWindow(name="Opus", percent=100.0, resets_at=LATER),)
    assert fable_spent(other, NOW_) is False, "a different model's bucket is not Fable"
    # The provider's display name is matched case-insensitively.
    lower = _rt_with_fable("c", 1, 100.0, resets_at=LATER)
    lower.model_usage = (ModelWindow(name="fable", percent=100.0, resets_at=LATER),)
    assert fable_spent(lower, NOW_) is True


def test_an_account_with_no_fable_bucket_is_never_spent():
    """Parity across kinds: an api account reports no per-model buckets and
    must route exactly as it does today."""
    plain = ProfileRuntime(profile_id="b", priority=2, switch_threshold=98.0, automatic=True,
                           leave_on_fable_limit=True)
    assert fable_spent(plain, NOW_) is False
    assert must_leave(plain, NOW_) is False


def test_a_bucket_past_its_own_reset_is_stale_not_spent():
    """Holding an account back on a window that has already refilled would be
    wrong in the unsafe direction — it idles capacity that exists."""
    stale = _rt_with_fable("a", 1, 100.0, resets_at=EARLIER)
    assert fable_spent(stale, NOW_) is False
    assert must_leave(stale, NOW_) is False


def test_a_bucket_below_the_threshold_is_not_spent():
    assert fable_spent(_rt_with_fable("a", 1, 97.9, resets_at=LATER), NOW_) is False
    assert fable_spent(_rt_with_fable("a", 1, 98.0, resets_at=LATER), NOW_) is True


def test_a_codex_account_is_spent_through_its_blocked_models():
    """OpenAI reports availability by GPT id; the gateway resolves that into
    Claude base ids. A blocked Fable model means the account is spent; a
    blocked model of another family does not."""
    codex = ProfileRuntime(profile_id="c", priority=1, switch_threshold=98.0, automatic=True,
                           leave_on_fable_limit=True, blocked_models=frozenset({"claude-fable-5"}))
    assert fable_spent(codex, NOW_) is True
    assert must_leave(codex, NOW_) is True
    opus_only = ProfileRuntime(profile_id="c", priority=1, switch_threshold=98.0, automatic=True,
                               leave_on_fable_limit=True, blocked_models=frozenset({"claude-opus-5"}))
    assert fable_spent(opus_only, NOW_) is False


def test_with_the_switch_off_a_spent_fable_week_changes_nothing():
    """Off by default: the account stays exactly as it always was — sticky,
    a candidate, and a place for new branches."""
    a = _rt_with_fable("a", 1, 100.0, resets_at=LATER, leave=False)
    b = _rt_with_fable("b", 2, 10.0, resets_at=LATER, leave=False)
    assert fable_spent(a, NOW_) is True
    assert must_leave(a, NOW_) is False
    pool = PoolSnapshot(profiles=[a, b], current_profile_id="a")
    assert choose(pool, NOW_) == choose(pool, NOW_)
    assert choose(pool, NOW_).profile_id == "a"
    assert choose(pool, NOW_).reason == "sticky"
    assert choose_for_new_branch(PoolSnapshot(profiles=[a, b]), NOW_, {}).profile_id == "a"


def test_with_the_switch_on_the_whole_session_moves():
    """Not just Fable requests: choose() takes no model at all, so the same
    decision applies to a Sonnet turn, a Haiku helper call, anything."""
    pool = PoolSnapshot(profiles=[_rt_with_fable("a", 1, 100.0, resets_at=LATER),
                                  _rt_with_fable("b", 2, 10.0, resets_at=LATER)],
                        current_profile_id="a")
    decision = choose(pool, NOW_)
    assert decision.profile_id == "b"
    # Not "rotated": the account did not run out, its Fable week did.
    assert decision.reason == "fable_limit_handover"
    # And it is not a candidate for anything new either.
    assert choose_for_new_branch(PoolSnapshot(profiles=pool.profiles), NOW_, {}).profile_id == "b"


def test_the_global_override_turns_it_on_for_a_profile_whose_own_flag_is_off():
    """router.py sees only the RESOLVED value (gateway._sync_snapshot ORs the
    profile flag with Settings.fable_limit_all_profiles). A runtime built
    with the resolved True leaves; the same account resolved False stays."""
    resolved_on = _rt_with_fable("a", 1, 100.0, resets_at=LATER, leave=True)
    resolved_off = _rt_with_fable("a", 1, 100.0, resets_at=LATER, leave=False)
    b = _rt_with_fable("b", 2, 10.0, resets_at=LATER, leave=False)
    assert choose(PoolSnapshot(profiles=[resolved_on, b], current_profile_id="a"), NOW_).profile_id == "b"
    assert choose(PoolSnapshot(profiles=[resolved_off, b], current_profile_id="a"), NOW_).profile_id == "a"


def test_with_no_alternative_the_session_stays_and_is_served_anyway():
    """Decision 8: degrade honestly. Refusing locally invents an error; going
    ahead gets the user the provider's own."""
    pool = PoolSnapshot(profiles=[_rt_with_fable("a", 1, 100.0, resets_at=LATER),
                                  _rt_with_fable("b", 2, 100.0, resets_at=LATER)],
                        current_profile_id="a")
    decision = choose(pool, NOW_)
    assert decision.profile_id == "a"
    assert decision.reason == "fable_limit_no_alternative"
    # With no current pointer at all, the best-ranked spent account is used.
    fresh = PoolSnapshot(profiles=pool.profiles, current_profile_id=None)
    assert choose(fresh, NOW_).profile_id == "a"
    assert choose(fresh, NOW_).reason == "fable_limit_no_alternative"
    # ...and a new branch is still placed rather than refused.
    assert choose_for_new_branch(fresh, NOW_, {}).profile_id is not None


def test_a_spent_account_becomes_a_candidate_again_when_its_bucket_shows_room():
    """Nothing is written into `state`, so a fresh usage read is all it takes."""
    from claude_unlimited.observation import ModelWindow, UsageSnapshot

    a = _rt_with_fable("a", 1, 100.0, resets_at=LATER)
    b = _rt_with_fable("b", 2, 10.0, resets_at=LATER)
    pool = PoolSnapshot(profiles=[a, b], current_profile_id="a")
    assert choose(pool, NOW_).profile_id == "b"
    refreshed = observe(pool, "a", UsageSnapshot(
        percent=10.0, resets_at=LATER, confidence="measured",
        model_windows=(ModelWindow(name="Fable", percent=5.0, resets_at=LATER),)), NOW_)
    assert must_leave(refreshed.profiles[0], NOW_) is False
    assert choose(refreshed, NOW_).profile_id == "a"


def test_an_exhausted_spent_account_is_not_revived_by_the_fable_rule():
    """must_leave() is ANDed onto the ordinary ELIGIBLE filter, never a
    replacement for it."""
    pool = PoolSnapshot(profiles=[_rt_with_fable("a", 1, 10.0, resets_at=LATER, state=ProfileState.EXHAUSTED),
                                  _rt_with_fable("b", 2, 100.0, resets_at=LATER)],
                        current_profile_id="a")
    decision = choose(pool, NOW_)
    assert decision.profile_id == "b"
    assert decision.reason == "fable_limit_no_alternative"


# --- the per-request capacity fit (docs/adr/0009) ---------------------------

def _over(*ids, tokens=300_000) -> RequestFit:
    return RequestFit(estimated_tokens=tokens, over_capacity=frozenset(ids))


def test_no_fit_means_fits_everywhere_and_changes_nothing():
    pool = PoolSnapshot(profiles=[rt("c"), rt("a", priority=2)], current_profile_id="c")
    assert fits(rt("c"), None)
    assert choose(pool, NOW, None) == choose(pool, NOW)
    assert choose(pool, NOW, RequestFit()).profile_id == "c"


def test_the_sticky_current_is_not_sticky_when_it_cannot_hold_the_request():
    """The account is fine for every smaller conversation; this one moves,
    and the reason says why rather than "rotated" (which reads as ran out)."""
    pool = PoolSnapshot(profiles=[rt("c", priority=1), rt("a", priority=2)], current_profile_id="c")
    decision = choose(pool, NOW, _over("c"))
    assert (decision.profile_id, decision.reason) == ("a", "window_handover")


def test_an_over_capacity_account_is_never_chosen_even_by_priority():
    pool = PoolSnapshot(profiles=[rt("c", priority=1), rt("a", priority=2)], current_profile_id=None)
    decision = choose(pool, NOW, _over("c"))
    assert (decision.profile_id, decision.reason) == ("a", "rotated")


def test_when_nothing_fits_the_reason_is_distinct_from_no_eligible_profile():
    """Capacity exists — the gateway turns this into the prompt-too-long
    the client compacts on, NOT the 503 it would hold ten minutes for."""
    pool = PoolSnapshot(profiles=[rt("c"), rt("d")], current_profile_id="c")
    decision = choose(pool, NOW, _over("c", "d"))
    assert (decision.profile_id, decision.reason) == (None, "no_profile_fits_request")
    empty = PoolSnapshot(profiles=[rt("c", state=ProfileState.EXHAUSTED)], current_profile_id=None)
    assert choose(empty, NOW, _over("c")).reason == "no_eligible_profile"


def test_over_capacity_is_not_a_fable_style_serve_anyway_degrade():
    """A spent Fable week has "serve anyway, the provider says no" as its
    honest degrade; an overflow has none (the backend's 400 is one the client
    cannot recover from). So the no-alternative path never lands on an
    over-capacity account, current or otherwise."""
    pool = PoolSnapshot(profiles=[rt("c", leave_on_fable_limit=True, blocked_models=frozenset({"claude-fable-5"})),
                                  rt("a", priority=2)],
                         current_profile_id="c")
    # a fits but must leave; c is over capacity: a is still the only choice.
    decision = choose(pool, NOW, _over("c"))
    assert decision.profile_id == "a"
    both_leaving = PoolSnapshot(
        profiles=[rt("c", leave_on_fable_limit=True, blocked_models=frozenset({"claude-fable-5"})),
                  rt("a", priority=2, leave_on_fable_limit=True, blocked_models=frozenset({"claude-fable-5"}))],
        current_profile_id="c")
    decision = choose(both_leaving, NOW, _over("c"))
    assert (decision.profile_id, decision.reason) == ("a", "fable_limit_no_alternative")


def test_a_manual_only_current_over_capacity_hands_over_to_an_automatic_one():
    pool = PoolSnapshot(profiles=[rt("c", automatic=False), rt("a", priority=2)], current_profile_id="c")
    assert choose(pool, NOW, _over("c")).profile_id == "a"


def test_a_new_branch_never_lands_on_an_over_capacity_account():
    pool = PoolSnapshot(profiles=[rt("c", priority=1), rt("a", priority=2)])
    decision = choose_for_new_branch(pool, NOW, {}, fit=_over("c"))
    assert (decision.profile_id, decision.reason) == ("a", "branch_assigned")
    nothing = choose_for_new_branch(pool, NOW, {}, fit=_over("c", "a"))
    assert (nothing.profile_id, nothing.reason) == (None, "no_profile_fits_request")
    assert choose_for_new_branch(pool, NOW, {}, exclude=frozenset({"c", "a"}), fit=_over("c")).reason \
        == "no_eligible_profile"


def test_a_new_branch_prefers_a_fitting_account_over_a_leaving_one_that_fits():
    """Both filters compose: fit first (hard), then the soft must_leave."""
    pool = PoolSnapshot(profiles=[
        rt("c", priority=1),
        rt("a", priority=2, leave_on_fable_limit=True, blocked_models=frozenset({"claude-fable-5"})),
        rt("b", priority=3),
    ])
    assert choose_for_new_branch(pool, NOW, {}, fit=_over("c")).profile_id == "b"
