"""Pure Rotation decision logic. No I/O, no credentials, no network.

choose() picks which Profile should serve the next request. observe() folds
an Observation (see observation.py) into updated per-profile state. Neither
touches an upstream, a keychain, or a clock beyond what is passed in, which
is what keeps them deterministically testable.

Rotation rules:
  - Sticky: stay on the current Profile until its switch_threshold is crossed
    or it returns quota-exhausted.
  - Never rotate on a bare short rate-limit; that Profile gets a cooldown,
    not a state change away from ELIGIBLE.
  - Tie-break among equally-eligible Profiles: lowest priority number wins;
    among equal priority, whichever has the freshest (or no) usage snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from .observation import (
    AuthInvalid,
    Observation,
    ProviderUnavailable,
    QuotaExhausted,
    ShortRateLimit,
    Unknown,
    UsageSnapshot,
)


class ProfileState(str, Enum):
    ELIGIBLE = "eligible"
    DRAINING = "draining"  # threshold crossed; finish in-flight, no new requests
    EXHAUSTED = "exhausted"  # explicit hard quota
    COOLDOWN = "cooldown"  # short rate-limit or provider-unavailable, temporary
    AUTH_INVALID = "auth_invalid"  # needs user action
    DISABLED = "disabled"  # user turned it off


@dataclass
class ProfileRuntime:
    """Per-Profile state the Router tracks: live and observed, as opposed to
    the configured, persisted claude_unlimited.config.Profile."""

    profile_id: str
    priority: int
    switch_threshold: float
    automatic: bool
    state: ProfileState = ProfileState.ELIGIBLE
    last_usage_percent: Optional[float] = None
    cooldown_until: Optional[datetime] = None
    resets_at: Optional[datetime] = None
    last_usage_percent_7d: Optional[float] = None  # display-only, never drives Rotation
    resets_at_7d: Optional[datetime] = None
    window_label: Optional[str] = None  # None means "assume 5h"; see observation.UsageSnapshot
    window_label_7d: Optional[str] = None  # None means "assume 7d" (Anthropic) or "no second window" (codex); the Dashboard tells them apart by whether last_usage_percent_7d is also None
    credential_seen: Optional[str] = None  # last config.Profile.credential_updated_at this runtime reacted to; see gateway.py's _sync_snapshot
    # Consecutive ShortRateLimit/ProviderUnavailable observations carrying no
    # retry_after_seconds, driving _cooldown_deadline's escalation. Reset to
    # 0 by a successful UsageSnapshot, or by an observation that does carry a
    # retry_after_seconds.
    consecutive_unretryable_failures: int = 0


@dataclass
class PoolSnapshot:
    profiles: list[ProfileRuntime] = field(default_factory=list)
    current_profile_id: Optional[str] = None


@dataclass(frozen=True)
class RoutingDecision:
    profile_id: Optional[str]
    reason: str  # "sticky" | "rotated" | "no_eligible_profile"


def choose(pool: PoolSnapshot, now: datetime) -> RoutingDecision:
    current = _find(pool, pool.current_profile_id)
    if current is not None and current.state == ProfileState.ELIGIBLE:
        return RoutingDecision(profile_id=current.profile_id, reason="sticky")

    candidates = [
        p
        for p in pool.profiles
        if p.state == ProfileState.ELIGIBLE and (p.automatic or p.profile_id == pool.current_profile_id)
    ]
    if not candidates:
        return RoutingDecision(profile_id=None, reason="no_eligible_profile")

    candidates.sort(key=lambda p: (p.priority, _reset_sort_key(p)))
    return RoutingDecision(profile_id=candidates[0].profile_id, reason="rotated")


def observe(pool: PoolSnapshot, profile_id: str, observation: Observation, now: datetime) -> PoolSnapshot:
    """Returns a NEW PoolSnapshot (the input is not mutated) with
    profile_id's runtime state folded in per the observation. Callers own
    persisting it."""

    new_profiles = []
    for p in pool.profiles:
        if p.profile_id != profile_id:
            new_profiles.append(p)
            continue
        new_profiles.append(_apply(p, observation, now))
    return PoolSnapshot(profiles=new_profiles, current_profile_id=pool.current_profile_id)


def _apply(p: ProfileRuntime, observation: Observation, now: datetime) -> ProfileRuntime:
    if isinstance(observation, UsageSnapshot):
        state = ProfileState.DRAINING if observation.percent >= p.switch_threshold else ProfileState.ELIGIBLE
        return _replace(p, state=state, last_usage_percent=observation.percent, resets_at=observation.resets_at,
                         last_usage_percent_7d=observation.percent_7d, resets_at_7d=observation.resets_at_7d,
                         window_label=observation.window_label, window_label_7d=observation.window_label_7d,
                         consecutive_unretryable_failures=0)  # a success: this Profile works again

    if isinstance(observation, QuotaExhausted):
        return _replace(p, state=ProfileState.EXHAUSTED, resets_at=observation.resets_at)

    if isinstance(observation, ShortRateLimit):
        streak = _next_unretryable_streak(p, observation.retry_after_seconds)
        cooldown_until = _cooldown_deadline(now, observation.retry_after_seconds, streak)
        return _replace(p, state=ProfileState.COOLDOWN, cooldown_until=cooldown_until,
                         consecutive_unretryable_failures=streak)

    if isinstance(observation, ProviderUnavailable):
        # Availability failover is a separate policy, not quota Rotation.
        # This module only marks a cooldown; whether that cooldown causes a
        # routing change is a Proxy-layer decision.
        streak = _next_unretryable_streak(p, observation.retry_after_seconds)
        cooldown_until = _cooldown_deadline(now, observation.retry_after_seconds, streak)
        return _replace(p, state=ProfileState.COOLDOWN, cooldown_until=cooldown_until,
                         consecutive_unretryable_failures=streak)

    if isinstance(observation, AuthInvalid):
        return _replace(p, state=ProfileState.AUTH_INVALID)

    if isinstance(observation, Unknown):
        return p

    return p


def recover_expired_cooldowns(pool: PoolSnapshot, now: datetime) -> PoolSnapshot:
    """Moves any COOLDOWN/EXHAUSTED profile whose deadline has passed back to
    ELIGIBLE. Call this before choose() on each request boundary."""

    new_profiles = []
    for p in pool.profiles:
        if p.state == ProfileState.COOLDOWN and p.cooldown_until is not None and now >= p.cooldown_until:
            new_profiles.append(_replace(p, state=ProfileState.ELIGIBLE, cooldown_until=None))
        elif p.state in (ProfileState.EXHAUSTED, ProfileState.DRAINING) and p.resets_at is not None and now >= p.resets_at:
            new_profiles.append(_replace(p, state=ProfileState.ELIGIBLE, resets_at=None, last_usage_percent=None))
        else:
            new_profiles.append(p)
    return PoolSnapshot(profiles=new_profiles, current_profile_id=pool.current_profile_id)


def _find(pool: PoolSnapshot, profile_id: Optional[str]) -> Optional[ProfileRuntime]:
    if profile_id is None:
        return None
    return next((p for p in pool.profiles if p.profile_id == profile_id), None)


def _reset_sort_key(p: ProfileRuntime) -> tuple:
    # Profiles with a known, sooner reset are preferred (spend the one that's
    # about to refill anyway); profiles with no reset info sort last within
    # their priority band, not first.
    return (0, p.resets_at) if p.resets_at is not None else (1, None)


def _next_unretryable_streak(p: ProfileRuntime, retry_after_seconds: Optional[float]) -> int:
    """The streak value _cooldown_deadline escalates against for this
    observation. Zero whenever the server sent a real retry_after_seconds,
    since that is a trustworthy signal to honor as-is; it only climbs for a
    429/503 with no Retry-After at all."""
    return 0 if retry_after_seconds is not None else p.consecutive_unretryable_failures + 1


def _cooldown_deadline(now: datetime, retry_after_seconds: Optional[float], unretryable_streak: int = 0):
    from datetime import timedelta

    if retry_after_seconds is not None:
        # Honor the server's requested backoff in full. Truncating it would
        # return the Profile to ELIGIBLE before the server's window closes
        # and walk straight back into the same rate limit. With several
        # Profiles in the pool a long cooldown costs nothing: rotation
        # prefers another one meanwhile. The ceiling is only a defensive cap
        # against an absurd header value.
        return now + timedelta(seconds=min(retry_after_seconds, 1800.0))
    # No Retry-After at all. Per Anthropic's docs this is what a
    # spend-cap/billing 429 looks like — it keeps failing until access
    # resumes — rather than a short blip. A flat default would retry a
    # spend-capped Profile at the same interval indefinitely, so back off
    # exponentially from 30s, doubling per consecutive unretryable failure
    # and capped at the same 1800s ceiling. A transient blip self-heals in
    # the first step or two; a stuck Profile reaches the ceiling in about
    # six failures.
    escalated = 30.0 * (2 ** max(0, unretryable_streak - 1))
    return now + timedelta(seconds=min(escalated, 1800.0))


def _replace(p: ProfileRuntime, **changes) -> ProfileRuntime:
    from dataclasses import replace

    return replace(p, **changes)
