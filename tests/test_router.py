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
    choose,
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
