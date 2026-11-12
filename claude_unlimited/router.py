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

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from .observation import (
    AuthInvalid,
    BudgetUnavailable,
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
    last_usage_percent_7d: Optional[float] = None  # weights distribute-mode placement (_branch_weights); never a switch threshold
    resets_at_7d: Optional[datetime] = None
    window_label: Optional[str] = None  # None means "assume 5h"; see observation.UsageSnapshot
    kind: Optional[str] = None  # the Profile's kind (oauth/api/codex); None reads as oauth
    window_label_7d: Optional[str] = None  # None means "assume 7d" (Anthropic) or "no second window" (codex); the Dashboard tells them apart by whether last_usage_percent_7d is also None
    # Per-model weekly windows (e.g. Fable), from usage reads only. The Fable
    # one drives fable_spent(); the rest are display-only.
    model_usage: tuple = ()
    # Prepaid credits a codex backend reported (issue #6). None = never seen
    # (any non-codex Profile, or one whose backend says nothing about them),
    # which is NOT the same as a reported balance of zero.
    credits_has: Optional[bool] = None
    credits_balance: Optional[float] = None
    # Mirrors Settings.codex_spend_credits for this Profile: may it keep
    # serving on paid credits once its plan window is spent? Plan windows are
    # prepaid, credits are real money per request, so this is off unless the
    # user turned it on.
    may_spend_credits: bool = False
    # Claude model BASE ids this Profile cannot currently serve, precomputed
    # where the parity map and a clock are available (gateway._sync_snapshot).
    #
    # Only codex Profiles populate it. Anthropic reports a PERCENTAGE per
    # model, which fable_spent() can test directly against the threshold;
    # OpenAI reports only availability, keyed by the GPT model id — and
    # resolving a Claude id to its GPT target needs the parity list and the
    # model catalogue, which is file I/O this module must not do.
    blocked_models: frozenset = frozenset()
    # RESOLVED "leave when Fable is spent" switch: the Profile's own
    # `leave_on_fable_limit` OR the pool-wide `fable_limit_all_profiles`
    # override, computed by gateway._sync_snapshot. While True and the Fable
    # bucket is spent (fable_spent), this account is not a rotation candidate
    # and a session on it moves — every model, not only Fable requests.
    leave_on_fable_limit: bool = False
    credential_seen: Optional[str] = None  # last config.Profile.credential_updated_at this runtime reacted to; see gateway.py's _sync_snapshot
    # Consecutive ShortRateLimit/ProviderUnavailable observations carrying no
    # retry_after_seconds, driving _cooldown_deadline's escalation. Reset to
    # 0 by a successful UsageSnapshot, or by an observation that does carry a
    # retry_after_seconds.
    consecutive_unretryable_failures: int = 0
    budget_unavailable: bool = False


@dataclass
class PoolSnapshot:
    profiles: list[ProfileRuntime] = field(default_factory=list)
    current_profile_id: Optional[str] = None


@dataclass(frozen=True)
class RoutingDecision:
    profile_id: Optional[str]
    reason: str  # "sticky" | "rotated" | "no_eligible_profile" | "fable_limit_handover" | "fable_limit_no_alternative" | "branch_assigned" | "window_handover" | "no_profile_fits_request"


@dataclass(frozen=True)
class RequestFit:
    """What this ONE request needs from an account, decided by the gateway
    (it needs the body, the parity map and the window table — none of which
    this module may read; docs/adr/0009).

    `estimated_tokens` is the conversation's estimated input size on an
    OpenAI backend; `over_capacity` the profile_ids whose backend window it
    exceeds. Only codex Profiles can ever be in the set: Claude Code sizes an
    Anthropic account's window itself. None (the common case, a body under
    the cheap byte floor) means "fits everywhere" and costs nothing.

    Per REQUEST, not per snapshot: the fact varies with every body, so it
    cannot live on ProfileRuntime, and it is never a state transition — the
    same account serves every smaller conversation."""
    estimated_tokens: int = 0
    over_capacity: frozenset = frozenset()


def fits(p: ProfileRuntime, fit: Optional[RequestFit]) -> bool:
    """Whether this account's backend can hold this request at all."""
    return fit is None or p.profile_id not in fit.over_capacity


# The bucket every "leave when Fable is spent" decision is about, expressed as
# a model id so openai_models' own family/bucket resolution names it rather
# than a second hardcoded string: model_bucket_name("claude-fable") == "Fable".
FABLE_MODEL_ID = "claude-fable"


def fable_spent(p: ProfileRuntime, now: datetime) -> bool:
    """Whether THIS account's Fable weekly limit is spent — the one per-model
    bucket the pool acts on (issue #2, reworked: the whole session leaves).

    Derived per request and never a state transition: nothing is written to
    `state`, and the account becomes a candidate again the moment a usage read
    shows room or the bucket's own reset passes.

      * Anthropic reports a PERCENTAGE per model: a `model_usage` window named
        Fable (case-insensitive) at or past this Profile's switch_threshold is
        spent — unless its own resets_at has already passed, in which case it
        is stale, not spent: the next read replaces it, and until then it must
        not hold an account back.
      * Codex reports only AVAILABILITY, keyed by the GPT id, resolved by the
        gateway into `blocked_models` (Claude base ids): the account is spent
        when the Claude model it cannot serve is a Fable one.

    No bucket at all (api Profiles, an account whose plan has no Fable limit)
    reads as NOT spent."""
    from .openai_models import bucket_matches, model_bucket_name

    fable_bucket = model_bucket_name(FABLE_MODEL_ID)
    if p.blocked_models and any(bucket_matches(m, fable_bucket) for m in p.blocked_models):
        return True

    for window in p.model_usage:
        if not bucket_matches(FABLE_MODEL_ID, getattr(window, "name", None)):
            continue
        if window.percent < p.switch_threshold:
            return False
        resets_at = getattr(window, "resets_at", None)
        return resets_at is None or now < resets_at
    return False


def must_leave(p: ProfileRuntime, now: datetime) -> bool:
    """Whether Rotation must treat this account as not-a-candidate right now:
    its resolved "leave when Fable is spent" switch is on AND its Fable weekly
    limit is spent. With the switch off, a spent Fable bucket changes nothing —
    the account routes exactly as it always has."""
    return bool(p.leave_on_fable_limit) and fable_spent(p, now)


def choose(pool: PoolSnapshot, now: datetime, fit: Optional[RequestFit] = None) -> RoutingDecision:
    current = _find(pool, pool.current_profile_id)
    if current is not None and current.state == ProfileState.ELIGIBLE \
            and not must_leave(current, now) and fits(current, fit):
        return RoutingDecision(profile_id=current.profile_id, reason="sticky")

    eligible = [
        p
        for p in pool.profiles
        if p.state == ProfileState.ELIGIBLE and (p.automatic or p.profile_id == pool.current_profile_id)
    ]
    # Over capacity is treated like not-ELIGIBLE, NOT like must_leave: a
    # spent Fable week has "serve anyway, the provider says no" as its honest
    # degrade, but a conversation a backend cannot hold has none — the
    # provider's error would be an OpenAI-shaped 400 the client cannot
    # recover from. So nothing below ever lands on an over-capacity account;
    # when no candidate fits, the gateway turns this reason into the one
    # error the client does recover from (prompt too long → compaction).
    candidates = [p for p in eligible if fits(p, fit)]
    if not candidates and eligible:
        return RoutingDecision(profile_id=None, reason="no_profile_fits_request")
    staying = [p for p in candidates if not must_leave(p, now)]
    if not staying:
        # Degrade honestly. If every account that could serve
        # has spent its Fable week, serving anyway gets the user the
        # provider's own error, which is the truth; refusing locally invents
        # one. The reason is distinct so the Dashboard can say which happened.
        if current is not None and current.state == ProfileState.ELIGIBLE and fits(current, fit):
            return RoutingDecision(profile_id=current.profile_id, reason="fable_limit_no_alternative")
        if not candidates:
            return RoutingDecision(profile_id=None, reason="no_eligible_profile")
        candidates.sort(key=lambda p: (p.priority, _reset_sort_key(p)))
        return RoutingDecision(profile_id=candidates[0].profile_id, reason="fable_limit_no_alternative")

    staying.sort(key=lambda p: (p.priority, _reset_sort_key(p)))
    # Naming the reason matters: "rotated" reads as "the account ran out", and
    # this account has not — its Fable week has and it asked to leave, or the
    # conversation outgrew its backend's window.
    reason = "rotated"
    if current is not None and current.state == ProfileState.ELIGIBLE:
        reason = "window_handover" if not fits(current, fit) else "fable_limit_handover"
    return RoutingDecision(profile_id=staying[0].profile_id, reason=reason)


def choose_for_new_branch(pool: PoolSnapshot, now: datetime,
                          branch_counts: Optional[dict] = None,
                          exclude: frozenset = frozenset(),
                          fit: Optional[RequestFit] = None) -> RoutingDecision:
    """Pick the account for a NEW conversation branch (a session's main agent
    or one of its subagents) — used only by distribute-mode routing.

    Unlike choose(), this is deliberately NOT sticky and NOT "first by
    priority": its job is to spend the pool well, so one account isn't
    drained while another sits idle and no account's week goes to waste.

    Each account gets a share of branches proportional to its weight
    (`_branch_weights`): how urgently its unused weekly quota needs spending
    (`_weekly_spend_rate`), capped against the pool and scaled down as its 5h
    window fills. The next
    branch goes to the account with the lowest load per unit of weight,
    (pinned branches + 1) / weight — weighted round-robin. An account whose
    week resets tomorrow with half its quota left takes several branches for
    every one a barely-used account with a week to go takes; one nearly out
    for the week takes few. With equal weights (a fresh pool) this is plain
    round-robin: each account takes a branch before any takes a second.

    Load has to come first. Any key that is the same for every branch of a
    session (priority, utilization) would, as the leading key, send every
    branch to the same winner, and "each subagent gets its own account"
    would quietly become "all of them share one". Load grows with each
    branch placed, so the winner changes.

    Ties then go to the least-used 5h window, then priority, then
    `_reset_sort_key`, then profile id.

    A depleted account can't be pulled in by this: candidates are ELIGIBLE
    only, so exhausted, draining and cooling-down accounts are already out.

    `branch_counts` is passed IN (derived by Gateway from its pin map) rather
    than read here: this module stays pure — no I/O, no clock, no mutable
    cross-request state — so it remains deterministically testable.

    Candidates must be ELIGIBLE and `automatic`: a manual-only Profile is
    never auto-assigned to a branch (that's what `automatic` means), which is
    a deliberate difference from choose()'s current-pointer exception.
    `exclude` carries the ids already tried and failed for this request, so
    failover picks a genuinely different account. `fit` (see RequestFit)
    rules out the accounts whose backend cannot hold this conversation, the
    same way choose() does."""
    counts = branch_counts or {}
    eligible = [
        p
        for p in pool.profiles
        if p.state == ProfileState.ELIGIBLE and p.automatic and p.profile_id not in exclude
    ]
    if not eligible:
        return RoutingDecision(profile_id=None, reason="no_eligible_profile")
    candidates = [p for p in eligible if fits(p, fit)]
    if not candidates:
        return RoutingDecision(profile_id=None, reason="no_profile_fits_request")
    # Placing a new branch on an account that has asked to be left (its
    # Fable week is spent) would simply bounce it on the next request. If
    # every account is in that position, place it anyway and let the provider
    # answer.
    staying = [p for p in candidates if not must_leave(p, now)]
    if staying:
        candidates = staying
    weights = _branch_weights(candidates, now)
    candidates.sort(key=lambda p: (
        round((counts.get(p.profile_id, 0) + 1) / weights[p.profile_id], 6),
        p.last_usage_percent if p.last_usage_percent is not None else 0.0,
        p.priority,
        _reset_sort_key(p),
        p.profile_id,  # final tie-break so equal candidates order deterministically
    ))
    return RoutingDecision(profile_id=candidates[0].profile_id, reason="branch_assigned")


def branch_shares(pool: PoolSnapshot, now: datetime) -> dict[str, float]:
    """Each account's long-run share (0..1) of NEW branches in distribute
    mode: its `_branch_weights` entry over the pool's total, among the accounts
    choose_for_new_branch would consider. For the Dashboard, so the accounts
    taking most of the load are visible at a glance; routing never reads it."""
    eligible = [p for p in pool.profiles if p.state == ProfileState.ELIGIBLE and p.automatic]
    candidates = [p for p in eligible if not must_leave(p, now)] or eligible
    weights = _branch_weights(candidates, now)
    total = sum(weights.values())
    return {pid: w / total for pid, w in weights.items()} if total > 0 else {}


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
        if state is ProfileState.DRAINING and _credits_can_serve(p):
            # Same reasoning as the QuotaExhausted branch: a credit-funded
            # account is not draining towards unavailability, so the switch
            # threshold — which exists to leave plan headroom — does not
            # apply to it.
            state = ProfileState.ELIGIBLE
        # A real response carries no per-model windows (model_windows None):
        # keep the last usage read's, or every request would erase them.
        model_usage = p.model_usage if observation.model_windows is None else tuple(observation.model_windows)
        return _replace(p, state=state, last_usage_percent=observation.percent, resets_at=observation.resets_at,
                         last_usage_percent_7d=observation.percent_7d, resets_at_7d=observation.resets_at_7d,
                         window_label=observation.window_label, window_label_7d=observation.window_label_7d,
                         model_usage=model_usage,
                         budget_unavailable=False, consecutive_unretryable_failures=0)  # a success: this Profile works again

    if isinstance(observation, QuotaExhausted):
        if _credits_can_serve(p):
            # The plan window is spent, but this Profile has prepaid credits
            # and the user has opted into spending them. The account can
            # still serve, so marking it EXHAUSTED would idle an account that
            # works. resets_at is still recorded: when the window refills we
            # want the number, and spend_on_credits() below is what the UI
            # reads to say the requests are being paid for.
            return _replace(p, state=ProfileState.ELIGIBLE, resets_at=observation.resets_at,
                             last_usage_percent=100.0)
        return _replace(p, state=ProfileState.EXHAUSTED, resets_at=observation.resets_at)

    if isinstance(observation, ShortRateLimit):
        streak = _next_unretryable_streak(p, observation.retry_after_seconds)
        cooldown_until = _cooldown_deadline(now, observation.retry_after_seconds, streak)
        return _replace(p, state=ProfileState.COOLDOWN, cooldown_until=cooldown_until,
                         consecutive_unretryable_failures=streak)

    if isinstance(observation, BudgetUnavailable):
        from datetime import timedelta
        # Funding requires provider-side action. Do not retry on every client
        # request; permit one new check after a bounded hour.
        return _replace(p, state=ProfileState.COOLDOWN, budget_unavailable=True,
                        cooldown_until=now + timedelta(hours=1))

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


def record_credits(pool: PoolSnapshot, profile_id: str,
                   has_credits: bool, balance: Optional[float]) -> PoolSnapshot:
    """Folds a credit report into one Profile's runtime, returning a NEW
    snapshot. Separate from observe() because credits are not an Observation:
    they ride along on both successful and rate-limited responses and never
    decide a state on their own (see openai_observation.Credits).

    A None balance keeps the last known number: the backend sometimes sends
    `has-credits` without one, and forgetting the figure would blank the
    Dashboard between requests."""
    new_profiles = []
    for p in pool.profiles:
        if p.profile_id != profile_id:
            new_profiles.append(p)
            continue
        new_profiles.append(_replace(
            p,
            credits_has=has_credits,
            credits_balance=p.credits_balance if balance is None else balance,
        ))
    return PoolSnapshot(profiles=new_profiles, current_profile_id=pool.current_profile_id)


def _credits_can_serve(p: ProfileRuntime) -> bool:
    """True when this Profile may keep serving on prepaid credits: the user
    opted in AND the backend last told us there are credits. Unknown
    (credits_has None) is not permission."""
    return bool(p.may_spend_credits and p.credits_has)


def spending_on_credits(p: ProfileRuntime) -> bool:
    """True when this Profile is being served from paid credits rather than
    its plan window — i.e. the window is at or past the point where it would
    otherwise have stopped. What the Dashboard, the HUD and /api/profiles
    show so nobody spends money without seeing it."""
    if not _credits_can_serve(p):
        return False
    used = p.last_usage_percent
    return used is not None and used >= p.switch_threshold


def recover_expired_cooldowns(pool: PoolSnapshot, now: datetime) -> PoolSnapshot:
    """Moves any COOLDOWN/EXHAUSTED profile whose deadline has passed back to
    ELIGIBLE. Call this before choose() on each request boundary."""

    new_profiles = []
    for p in pool.profiles:
        if p.state == ProfileState.COOLDOWN and p.cooldown_until is not None and now >= p.cooldown_until:
            new_profiles.append(_replace(p, state=ProfileState.ELIGIBLE, cooldown_until=None, budget_unavailable=False))
        elif p.state in (ProfileState.EXHAUSTED, ProfileState.DRAINING) and p.resets_at is not None and now >= p.resets_at:
            new_profiles.append(_replace(p, state=ProfileState.ELIGIBLE, resets_at=None, last_usage_percent=None))
        else:
            new_profiles.append(p)
    return PoolSnapshot(profiles=new_profiles, current_profile_id=pool.current_profile_id)


def _find(pool: PoolSnapshot, profile_id: Optional[str]) -> Optional[ProfileRuntime]:
    if profile_id is None:
        return None
    return next((p for p in pool.profiles if p.profile_id == profile_id), None)


_WEEK_HOURS = 7 * 24
# A reset hours away is not a reason to pile everything onto one account: the
# rate is computed as if at least this long were left.
_MIN_HOURS_TO_RESET = 12.0
# No account weighs more than this many times the pool's median, so a
# subagent fan-out spreads instead of draining one account's 5h window.
_MAX_WEIGHT_VS_MEDIAN = 3.0
# Accounts without a weekly window (API keys, Codex plans whose only window is
# 5h/daily/monthly) get this; so does the floor for a nearly-spent week.
_MIN_BRANCH_WEIGHT = 0.1


def _clean_percent(value: Optional[float]) -> Optional[float]:
    """A usage percent clamped to 0..100; None for a missing or non-finite one."""
    if value is None or not math.isfinite(value):
        return None
    return min(100.0, max(0.0, value))


def _weekly_spend_rate(p: ProfileRuntime, now: datetime) -> Optional[float]:
    """Unused weekly quota per hour left before the week resets: how fast
    this account would have to be used for none of its week to go to waste.
    50% left resetting in 24h is ~2.1 %/h; in 72h, ~0.7 %/h. None when the
    account has no weekly window at all.

      * A Claude subscription's weekly window is the 7d one. A subscription
        (Claude or Codex) not observed yet counts as unused with a full week
        left, until its first reading says otherwise.
      * An account whose only window is weekly (window_label "weekly") uses
        that, with switch_threshold as the ceiling, since rotation leaves it
        there.
      * API keys, and Codex plans observed to have only a 5h/daily/monthly
        window, have no weekly window: nothing of theirs expires weekly.
      * A reading whose reset has passed is stale: the window refilled, so it
        counts as unused with a full week ahead.
    Hours are floored at _MIN_HOURS_TO_RESET."""
    if p.last_usage_percent_7d is not None or p.resets_at_7d is not None:
        percent, resets_at, ceiling = p.last_usage_percent_7d, p.resets_at_7d, 100.0
    elif p.window_label == "weekly":
        percent, resets_at, ceiling = p.last_usage_percent, p.resets_at, p.switch_threshold
    elif p.kind == "api" or (p.kind == "codex" and (p.window_label is not None
                                                     or p.last_usage_percent is not None)):
        return None
    else:
        percent, resets_at, ceiling = None, None, 100.0
    if resets_at is not None and resets_at <= now:
        percent, resets_at = None, None
    headroom = max(0.0, ceiling - (_clean_percent(percent) or 0.0))
    hours = _WEEK_HOURS
    if resets_at is not None:
        hours = max(_MIN_HOURS_TO_RESET, (resets_at - now).total_seconds() / 3600)
    return headroom / hours


def _five_hour_room(p: ProfileRuntime, now: datetime) -> float:
    """How much of the 5h window is left before the switch threshold, as a
    0.05..1 multiplier: piling branches onto an account about to hit its 5h
    limit only moves them again shortly, each move a prompt-cache miss.
    1.0 when the primary window isn't 5h, is unread, or has already reset."""
    if p.window_label not in (None, "5h"):
        return 1.0
    percent = _clean_percent(p.last_usage_percent)
    if percent is None or (p.resets_at is not None and p.resets_at <= now):
        return 1.0
    threshold = p.switch_threshold if p.switch_threshold > 0 else 100.0
    return min(1.0, max(0.05, (threshold - percent) / threshold))


def _branch_weights(candidates: list, now: datetime) -> dict[str, float]:
    """Distribute mode's weight per candidate: the weekly spend rate, capped
    at _MAX_WEIGHT_VS_MEDIAN times the candidates' median and floored at
    _MIN_BRANCH_WEIGHT, times `_five_hour_room`. An account with no weekly
    window gets the floor, clamped to the lowest weight of any account that
    has one, so an API key never outweighs a subscription. The cap uses the
    low median so a 2-account pool is capped too. Always positive and finite."""
    rates = {p.profile_id: _weekly_spend_rate(p, now) for p in candidates}
    known = [r for r in rates.values() if r is not None]
    cap = max(_MIN_BRANCH_WEIGHT, statistics.median_low(known)) * _MAX_WEIGHT_VS_MEDIAN if known else None
    weights = {}
    for p in candidates:
        rate = rates[p.profile_id]
        base = _MIN_BRANCH_WEIGHT if rate is None else max(_MIN_BRANCH_WEIGHT, rate)
        if cap is not None:
            base = min(base, cap)
        weights[p.profile_id] = base * _five_hour_room(p, now)
    rated = [weights[pid] for pid, r in rates.items() if r is not None]
    if rated:
        no_weekly_cap = min(_MIN_BRANCH_WEIGHT, min(rated))
        for pid, r in rates.items():
            if r is None:
                weights[pid] = min(weights[pid], no_weekly_cap)
    return weights


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
