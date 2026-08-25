"""Ties Router + Observation + Proxy + secret_store into one handle() call.

This is the Proxy module's orchestration layer (see the implementation
review's module list) — the HTTP-server plumbing lives in daemon.py, the
pure decision/transform logic lives in router.py/observation.py/proxy.py,
and the real socket I/O lives in upstream.py. This file is what a real
request handler calls; it's kept separate from daemon.py's BaseHTTPRequestHandler
subclass so it can be tested with a fake `transport` callable instead of a
real HTTPS connection.

Safety rule, structural rather than by convention: this module NEVER forwards a single byte of the upstream
response body before deciding whether to retry on quota-exhaustion. Status
and headers arrive first (see upstream.send's use of http.client, which
reads the header block before any body read); the retry decision is made
right there, before body_chunks is ever iterated. Once the caller starts
draining body_chunks, that response is committed — Gateway will not retry
underneath it.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Iterator, Optional

from . import activity, connectors, notifications, oauth_credential, oauth_login, openai_bridge, openai_observation, openai_translate, project_attribution, project_usage, runtime_state, secret_store, usage_history, usage_tracking
from . import profiles as profile_repo
from .config import Pool, Profile, load_pool
from .observation import AuthInvalid, ProviderUnavailable, QuotaExhausted, Unknown, UsageSnapshot, classify
from .proxy import build_upstream_request, filter_response_headers, request_model, rewrite_model
from .router import PoolSnapshot, ProfileRuntime, ProfileState, RoutingDecision, choose, observe, recover_expired_cooldowns
from .upstream import UpstreamResponse
from .upstream import send as real_send

MAX_ROTATION_ATTEMPTS = 4  # bounded — never loop the whole pool forever on a bad run


def _filter_openai_headers(headers: dict[str, str]) -> dict[str, str]:
    """The codex-kind analogue of proxy.filter_response_headers — restricts
    an OpenAI backend response's headers to openai_observation.py's own
    allowlist before classify() ever sees them, same contract as the
    Anthropic side."""
    lower = {k.lower(): v for k, v in headers.items()}
    return {k: lower[k] for k in openai_observation.ALLOWED_HEADERS if k in lower}

# A Profile is warned about an approaching threshold once per crossing, not
# once per request — this in-memory set is cleared for a Profile the moment
# it leaves ELIGIBLE (rotated/exhausted) or comes back from a reset, so the
# next approach gets its own warning instead of staying silent forever.
_QUOTA_RESET_SOURCE_STATES = (ProfileState.COOLDOWN, ProfileState.EXHAUSTED, ProfileState.DRAINING)
APPROACHING_THRESHOLD_BAND = 5.0  # percentage points below switch_threshold that counts as "approaching"

# Status codes plausibly meaning "this specific model isn't usable with this
# key" for an API-kind Profile — 400 (invalid_request_error, e.g. "model:
# X not found"), 403 (permission_error, key not scoped for this model —
# see observation.py's classify()), 404 (not_found_error). Never touches
# OAuth Profiles or any other status: this is specifically the
# default_model fallback (see Gateway._maybe_retry_with_default_model).
_MODEL_FALLBACK_STATUS_CODES = (400, 403, 404)


def _restorable_usage_fields(persisted: Optional[dict], now: datetime) -> dict:
    """Turns one profile's entry from runtime_state.load() into
    ProfileRuntime kwargs — only the usage-number fields, never state.
    A window whose resets_at has already passed isn't restored at all
    (showing a stale percentage past its own reset would be actively
    wrong); everything else defensively no-ops on missing/malformed data
    rather than raising — this must never break daemon startup."""
    if not persisted:
        return {}

    def _parse(iso: object) -> Optional[datetime]:
        if not isinstance(iso, str):
            return None
        try:
            return datetime.fromisoformat(iso)
        except ValueError:
            return None

    fields: dict = {}
    resets_at = _parse(persisted.get("resets_at"))
    if resets_at is not None and resets_at > now and isinstance(persisted.get("last_usage_percent"), (int, float)):
        fields["last_usage_percent"] = persisted["last_usage_percent"]
        fields["resets_at"] = resets_at
    resets_at_7d = _parse(persisted.get("resets_at_7d"))
    if resets_at_7d is not None and resets_at_7d > now and isinstance(persisted.get("last_usage_percent_7d"), (int, float)):
        fields["last_usage_percent_7d"] = persisted["last_usage_percent_7d"]
        fields["resets_at_7d"] = resets_at_7d
    return fields



def _client_wants_streaming(body: bytes) -> bool:
    """Whether the inbound Anthropic request asked for an SSE response.

    Anthropic's default is non-streaming, but every real Claude Code turn
    sets stream:true explicitly; an unparsable or absent body is treated as
    streaming so a malformed request can never silently buffer a whole
    response in memory."""
    if not body:
        return True
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return True
    if not isinstance(parsed, dict) or "stream" not in parsed:
        return True
    return bool(parsed.get("stream"))


@dataclass(frozen=True)
class GatewayResult:
    status: int
    headers: dict
    body_chunks: Optional[Iterator[bytes]]
    profile_id: Optional[str]
    error: Optional[str] = None


class Gateway:
    """Holds the live Rotation state for the running daemon process. One
    instance per daemon.

    _USED_NOW_GRACE_SECONDS: how long a Profile keeps showing as "Used now"
    on the Dashboard after its last request finished — see in_flight_ids()'s
    docstring for why this exists at all (a quick call can complete faster
    than the Dashboard polls). Deliberately session-length, not
    request-length: the intent is "this Profile is what an active session
    is currently using," not "a request literally completed within the
    last few seconds" — a real gap between individual API calls in an
    ongoing conversation (thinking time, a long tool call, someone reading
    a response) is normal and must not flicker the indicator off. It
    clears sooner than this if a real rotation switch moves the shared
    pointer away from this Profile first — see handle()'s
    `self._last_active.pop(previous_profile_id, None)`.

    Rotation STATE itself (ELIGIBLE/DRAINING/EXHAUSTED/COOLDOWN/AUTH_INVALID)
    is intentionally NOT persisted across a restart — see router.py's module
    docstring: no claimed per-session affinity, request-boundary global Pool
    state only, and every enabled Profile genuinely deserves a fresh try
    after a restart, same as it always has. What IS persisted (via
    runtime_state.py) is just the last-observed usage numbers and which
    Profile was current — display-only data the Dashboard would otherwise
    show as a blank "not yet observed" wall after every restart/update/
    service bounce, even though the real quota window hasn't actually
    reset. A number whose reset time has already passed by load time is
    dropped, not restored — see _sync_snapshot()."""

    _USED_NOW_GRACE_SECONDS = 900.0  # 15 minutes — see the class docstring
    _REFRESH_CHECK_COOLDOWN_SECONDS = 60.0
    # A 429 from the OAuth token endpoint means back off hard. Retrying a
    # rate-limited endpoint every 60s only re-triggers the same limiter and
    # never lets it clear.
    _RATE_LIMIT_BACKOFF_SECONDS = 900.0

    def __init__(self, transport: Callable = real_send):
        self._lock = threading.Lock()
        self._runtime: dict[str, ProfileRuntime] = {}
        self._current_profile_id: Optional[str] = None
        self._transport = transport
        self._warned_approaching: set[str] = set()
        # Profile ids with a request genuinely in flight right now — a
        # request is only ever added here once it's actually being served
        # (transport call started) and removed once that Profile's response
        # is fully drained or the client disconnects (see
        # _wrap_with_in_flight_clear). Backs the Dashboard's "Used now"
        # indicator; unlike current_profile_id (one shared rotation
        # pointer), more than one Profile can legitimately be in here at
        # once — e.g. two concurrent `claude-unlimited code --profile`
        # terminals pinned to different Profiles.
        self._in_flight: set[str] = set()
        # Last time (monotonic) each Profile's in-flight request finished.
        # Without this, "Used now" is only ever true for the literal
        # duration of one request/response cycle — for a quick non-streaming
        # call that can be well under the Dashboard's 1s poll interval, so
        # the indicator would flicker on and off between ticks and often
        # never be observed at all. Holding it visible for
        # _USED_NOW_GRACE_SECONDS (a session-length window, not a request-
        # length one — see the class docstring) after the last completion
        # makes it represent "this is the Profile the current session is
        # using" rather than "a request happened to be running a moment
        # ago."
        self._last_active: dict[str, float] = {}
        # Per-Profile throttle for _maybe_check_oauth_credential — maps
        # profile id to the monotonic time before which another refresh
        # attempt must not be made. That method does a real secret_store
        # fetch and, when due, a real network call to Anthropic's token
        # endpoint, so it must not run on every single _sync_snapshot call
        # (every Dashboard poll tick, ~1/s); a 429 response pushes this
        # deadline out much further than a normal attempt does (see
        # _RATE_LIMIT_BACKOFF_SECONDS). See that method's own docstring for
        # what it's actually for.
        self._refresh_check_not_before: dict[str, float] = {}
        persisted = runtime_state.load()
        self._persisted_profiles: dict = persisted["profiles"]
        self._current_profile_id = persisted["current_profile_id"]

    def _sync_snapshot(self, pool: Pool) -> PoolSnapshot:
        """Folds the persisted Pool (source of truth for config: priority,
        threshold, enabled, automatic) into the live runtime state (source of
        truth for observed state: ELIGIBLE/DRAINING/etc), adding new Profiles
        and dropping deleted ones, never losing observed state for a Profile
        that still exists just because its config changed."""

        live_ids = {p.id for p in pool.profiles}
        self._runtime = {pid: rt for pid, rt in self._runtime.items() if pid in live_ids}

        # An API-kind Profile has no session-% concept the way OAuth's
        # switch_threshold does (there's no Anthropic rate-limit header to
        # read), so token_threshold is its analogue: a hard, absolute
        # lifetime-token-count cap instead of a percentage of a window
        # Anthropic reports. Only computed when at least one Profile
        # actually uses it — usage_history.list_events() reads and parses
        # the whole (bounded, but potentially sizeable) usage log, not
        # worth paying on every single request for setups that never touch
        # this feature.
        tokens_by_profile = None
        if any(p.kind == "api" and p.token_threshold for p in pool.profiles):
            tokens_by_profile = usage_history.usage_by_profile(usage_history.list_events())

        def _over_token_budget(p: Profile) -> bool:
            if p.kind != "api" or not p.token_threshold or tokens_by_profile is None:
                return False
            return tokens_by_profile.get(p.id, {}).get("tokens", 0) >= p.token_threshold

        for p in pool.profiles:
            if p.id not in self._runtime:
                state = ProfileState.ELIGIBLE if p.enabled else ProfileState.DISABLED
                if state == ProfileState.ELIGIBLE and _over_token_budget(p):
                    # EXHAUSTED, not DRAINING — DRAINING's status_word
                    # ("near threshold") is right for OAuth's soft,
                    # percentage-based crossing but actively wrong for a
                    # hard, user-set token cap that's already been passed;
                    # EXHAUSTED's own state comment already calls it out as
                    # exactly this: "explicit hard quota".
                    state = ProfileState.EXHAUSTED
                    self._notify_token_budget_exhausted(p, tokens_by_profile, pool)
                self._runtime[p.id] = ProfileRuntime(
                    profile_id=p.id, priority=p.priority, switch_threshold=p.switch_threshold,
                    automatic=p.automatic,
                    state=state,
                    credential_seen=p.credential_updated_at,
                    **_restorable_usage_fields(self._persisted_profiles.get(p.id), now=datetime.now(timezone.utc)),
                )
                if state == ProfileState.ELIGIBLE:
                    # Covers a Profile that's ELIGIBLE but already near its
                    # real expiry the very first time this Gateway instance
                    # ever sees it — e.g. right after a daemon restart, before
                    # any request or a second sync tick would otherwise reach
                    # the existing-Profile branch below. A brand new Profile
                    # can never start AUTH_INVALID (it hasn't been observed
                    # yet), so this only ever exercises the preventive
                    # refresh-while-still-healthy path, never the recovery one.
                    self._maybe_check_oauth_credential(p, self._runtime[p.id])
            else:
                rt = self._runtime[p.id]
                new_state = rt.state
                credential_refreshed = (
                    p.credential_updated_at is not None and p.credential_updated_at != rt.credential_seen
                )
                if not p.enabled:
                    new_state = ProfileState.DISABLED
                elif rt.state == ProfileState.DISABLED and p.enabled:
                    new_state = ProfileState.ELIGIBLE
                elif rt.state == ProfileState.AUTH_INVALID and credential_refreshed:
                    # A stuck AUTH_INVALID Profile is filtered out of choose()'s
                    # candidates and has no time-based recovery (unlike
                    # COOLDOWN/EXHAUSTED) — without this, re-authenticating an
                    # already-registered Profile (CLI `login`, "Import current
                    # login", or re-pasting a token) would never actually clear
                    # the "needs re-auth" status until a manual disable/enable
                    # toggle or a full daemon restart, even though the real
                    # credential behind it is now valid.
                    new_state = ProfileState.ELIGIBLE
                elif (rt.state == ProfileState.DRAINING and p.kind in ("oauth", "codex")
                        and rt.last_usage_percent is not None and rt.last_usage_percent < p.switch_threshold):
                    # Raising switch_threshold can put a DRAINING Profile
                    # back under its own threshold without any new request
                    # ever happening — re-check the LAST OBSERVED number
                    # against the CURRENT (just-edited) threshold right
                    # here, instead of leaving it stuck until the next
                    # real request notices.
                    new_state = ProfileState.ELIGIBLE
                elif (rt.state == ProfileState.EXHAUSTED and p.kind == "api"
                        and p.token_threshold and not _over_token_budget(p)):
                    # Same idea for a token-budget-triggered EXHAUSTED —
                    # raising token_threshold above the current lifetime
                    # usage recovers it immediately.
                    new_state = ProfileState.ELIGIBLE
                elif self._maybe_check_oauth_credential(p, rt) == ProfileState.ELIGIBLE:
                    # An AUTH_INVALID OAuth Profile just self-recovered via
                    # its refresh_token — see that method's docstring. For
                    # every other state this call is either a no-op (not
                    # oauth, not due for a check yet) or a proactive refresh
                    # with no state change (still ELIGIBLE, just less close
                    # to actually expiring now).
                    new_state = ProfileState.ELIGIBLE
                if new_state not in (ProfileState.DISABLED, ProfileState.AUTH_INVALID) and _over_token_budget(p):
                    # No resets_at (there's no time window here) — stays
                    # EXHAUSTED, same as force_active()'s reasoning for
                    # other states, until a real user action clears it:
                    # raising the budget, disabling then re-enabling, or
                    # Take Over. Only notify on the actual crossing, not
                    # every sync while it stays over budget.
                    if new_state != ProfileState.EXHAUSTED:
                        self._notify_token_budget_exhausted(p, tokens_by_profile, pool)
                    new_state = ProfileState.EXHAUSTED
                self._runtime[p.id] = ProfileRuntime(
                    profile_id=p.id, priority=p.priority, switch_threshold=p.switch_threshold,
                    automatic=p.automatic, state=new_state,
                    last_usage_percent=rt.last_usage_percent, cooldown_until=rt.cooldown_until,
                    resets_at=rt.resets_at,
                    last_usage_percent_7d=rt.last_usage_percent_7d, resets_at_7d=rt.resets_at_7d,
                    window_label=rt.window_label, window_label_7d=rt.window_label_7d,
                    credential_seen=p.credential_updated_at,
                    # Must be carried over: this reconstruction runs on every
                    # _sync_snapshot call (roughly every Dashboard poll tick, not
                    # only on a real observation). Rebuilding without it resets
                    # the escalating-backoff streak within about a second of it
                    # being incremented, defeating router.py's no-Retry-After
                    # design (see _cooldown_deadline's own docstring) in
                    # practice any time the Dashboard was open. The exact class
                    # of bug the "Background API retry safety" standing rule
                    # exists to catch, found here by inspection rather than by
                    # a second live incident.
                    consecutive_unretryable_failures=rt.consecutive_unretryable_failures,
                )
        if self._current_profile_id not in live_ids:
            self._current_profile_id = None
        return PoolSnapshot(profiles=list(self._runtime.values()), current_profile_id=self._current_profile_id)

    @staticmethod
    def _notify_token_budget_exhausted(p: Profile, tokens_by_profile: Optional[dict], pool: Pool) -> None:
        """Fires exactly once per crossing (caller only calls this the
        moment new_state is about to become EXHAUSTED, not on every sync
        while it stays there) — same "needs_attention" category
        AUTH_INVALID already uses, since both mean "this Profile needs a
        real user action before it'll be picked again"."""
        used = tokens_by_profile.get(p.id, {}).get("tokens", 0) if tokens_by_profile else 0
        activity.record("error", f"{p.name} hit its token budget",
                         meta=f"{used}/{p.token_threshold} tokens — excluded from rotation")
        notifications.notify_if_enabled(
            "needs_attention", "Claude Unlimited",
            f"{p.name} hit its token budget ({used}/{p.token_threshold}) — excluded from rotation.", pool.settings)

    def handle(self, method: str, path: str, headers: dict, body: bytes,
               forced_profile_id: Optional[str] = None) -> GatewayResult:
        """forced_profile_id (see session_tokens.py) pins this ONE request
        to exactly that Profile — set by a `claude-unlimited code --profile`
        terminal session, and scoped to it alone: unlike force_active()
        ("Take over", a Dashboard-wide sticky override), this never touches
        self._current_profile_id or fires a "Rotated" notification, since
        other concurrent sessions' own rotation must stay exactly as it
        was. A forced request that can't be served returns a clear error
        instead of silently trying a different Profile — silently
        substituting a different account is exactly what pinning is for
        avoiding."""
        now = datetime.now(timezone.utc)
        attempted: set[str] = set()
        previous_profile_id = self._current_profile_id

        for _ in range(MAX_ROTATION_ATTEMPTS):
            with self._lock:
                pool = load_pool()
                snapshot = self._sync_snapshot(pool)
                pre_recovery_states = {rt.profile_id: rt.state for rt in snapshot.profiles}
                snapshot = recover_expired_cooldowns(snapshot, now)
                self._runtime = {rt.profile_id: rt for rt in snapshot.profiles}
                if forced_profile_id is not None:
                    decision = self._forced_decision(pool, forced_profile_id)
                else:
                    decision = choose(snapshot, now)

            for rt in snapshot.profiles:
                if pre_recovery_states.get(rt.profile_id) in _QUOTA_RESET_SOURCE_STATES and rt.state == ProfileState.ELIGIBLE:
                    self._warned_approaching.discard(rt.profile_id)
                    name = self._profile_name(pool, rt.profile_id)
                    activity.record("rotation", f"{name} quota reset — eligible again")
                    notifications.notify_if_enabled("quota_reset", "Claude Unlimited",
                                                      f"{name} is available again.", pool.settings)

            if decision.profile_id is None or decision.profile_id in attempted:
                if forced_profile_id is not None:
                    activity.record("error", "Pinned Profile unavailable — request rejected",
                                     meta=f"{forced_profile_id}: {decision.reason}")
                    return GatewayResult(status=503, headers={}, body_chunks=None, profile_id=None,
                                          error=decision.reason)
                if previous_profile_id is not None:
                    activity.record("error", "No eligible Profile available — request rejected",
                                     meta=f"last active was {previous_profile_id}")
                    notifications.notify_if_enabled("needs_attention", "Claude Unlimited",
                                                      "No eligible Profile is available — a request was rejected.",
                                                      pool.settings)
                return GatewayResult(status=503, headers={}, body_chunks=None, profile_id=None,
                                      error="no_eligible_profile")

            profile = pool.get(decision.profile_id)
            attempted.add(profile.id)

            # GET /v1/models is what Claude Code builds its `/model` picker
            # from. A connector whose real backend isn't Anthropic-shaped
            # answers it locally (see connectors.models_listing); everything
            # else falls through and relays upstream exactly as before, so
            # a Claude Profile keeps serving Anthropic's own live list.
            if method == "GET" and path.startswith("/v1/models"):
                listing = connectors.models_listing(profile.kind)
                if listing is not None:
                    return self._models_listing_response(profile, path, listing)

            try:
                credential = secret_store.get_token(profile.id)
            except Exception:
                self._observe(profile.id, QuotaExhausted(resets_at=None), now)  # treat as unusable, try next
                continue

            if profile.kind == "codex":
                result = self._handle_codex(profile, credential, method, path, headers, body, now,
                                             forced_profile_id, previous_profile_id, pool)
                if result is not None:
                    return result
                continue  # this attempt failed in a rotate-away way — try the next eligible Profile

            if profile.kind == "oauth":
                credential = self._maybe_refresh_credential(profile, credential)

            try:
                upstream_req = build_upstream_request(profile, credential, method, path, headers, body)
            except ValueError:
                # A structurally invalid inbound request (e.g. over the body
                # size cap) — no Profile can serve this; retrying the next
                # one would just repeat the same ValueError until the loop
                # gives up with a misleading "no eligible profile" error.
                # Fail the request itself, immediately.
                return GatewayResult(status=400, headers={}, body_chunks=None, profile_id=None,
                                      error="bad_request")

            # "in flight" from here until either this attempt is abandoned
            # (cleared explicitly below, at every exit that doesn't hand a
            # body back to the caller) or the response body this Profile
            # served is fully drained/closed (cleared by the generator
            # wrapper further down) — backs the Dashboard's "Used now"
            # indicator, which can legitimately be true for MORE than one
            # Profile at once (concurrent `claude-unlimited code --profile`
            # sessions each pinned to a different one).
            with self._lock:
                self._in_flight.add(profile.id)

            try:
                resp: UpstreamResponse = self._transport(upstream_req)
            except OSError:
                # Real network failure reaching this Profile's upstream
                # (timeout, DNS, connection refused, TLS) — not a quota
                # problem. Same handling as a 503/529 ProviderUnavailable
                # response: brief cooldown, try the next eligible Profile.
                # Previously unhandled here, this could crash the request
                # thread with no HTTP response at all (daemon.py's proxy
                # handler has no guard around Gateway.handle() either).
                with self._lock:
                    self._mark_profile_idle(profile.id)
                self._observe(profile.id, ProviderUnavailable(retry_after_seconds=None), now)
                if forced_profile_id is not None:
                    # No other Profile to fall back to when pinned — fail
                    # the request clearly instead of a pointless immediate
                    # retry of the same unreachable upstream.
                    activity.record("error", f"{profile.name} — could not reach upstream",
                                     meta="pinned profile, not rotating")
                    return GatewayResult(status=503, headers={}, body_chunks=None, profile_id=None,
                                          error="upstream_unreachable")
                activity.record("error", f"{profile.name} — could not reach upstream",
                                 meta="network error, rotating to next eligible profile")
                continue

            observation = classify(resp.status, filter_response_headers(resp.headers), now)

            if (profile.kind == "api" and profile.default_model
                    and isinstance(observation, Unknown) and observation.status_code in _MODEL_FALLBACK_STATUS_CODES):
                retried = self._maybe_retry_with_default_model(
                    profile, credential, method, path, headers, upstream_req.body, now)
                if retried is not None:
                    resp.connection.close()  # the first attempt's response is being discarded, unread
                    resp, observation = retried

            old_rt = self._runtime.get(profile.id)
            old_state = old_rt.state if old_rt is not None else None
            with self._lock:
                self._observe(profile.id, observation, now)
            new_rt = self._runtime.get(profile.id)
            self._persist()

            if isinstance(observation, AuthInvalid) and old_state != ProfileState.AUTH_INVALID:
                activity.record("error", f"{profile.name} needs re-authentication", meta="credential rejected")
                notifications.notify_if_enabled("needs_attention", "Claude Unlimited",
                                                  f"{profile.name} needs re-authentication.", pool.settings)

            if isinstance(observation, UsageSnapshot) and new_rt is not None and new_rt.state == ProfileState.ELIGIBLE:
                near_threshold = observation.percent >= new_rt.switch_threshold - APPROACHING_THRESHOLD_BAND
                if near_threshold and profile.id not in self._warned_approaching:
                    self._warned_approaching.add(profile.id)
                    notifications.notify_if_enabled(
                        "approaching_threshold", "Claude Unlimited",
                        f"{profile.name} is approaching its switch threshold "
                        f"({observation.percent:.0f}% / {new_rt.switch_threshold:.0f}%).", pool.settings)
                elif not near_threshold:
                    self._warned_approaching.discard(profile.id)

            if isinstance(observation, QuotaExhausted):
                if forced_profile_id is not None:
                    # No other Profile to fall back to when pinned — relay
                    # Anthropic's real quota-exhausted response as-is
                    # instead of rotating away from the one Profile the
                    # user explicitly chose for this terminal.
                    activity.record("rotation", f"{profile.name} hit its quota",
                                     meta="pinned profile — returning the real response, not rotating")
                else:
                    # Headers/status only have arrived so far — no body
                    # byte has reached the caller yet. Safe to retry on the
                    # next profile.
                    with self._lock:
                        self._mark_profile_idle(profile.id)
                    resp.connection.close()
                    activity.record("rotation", f"{profile.name} hit its quota", meta="rotating to next eligible profile")
                    continue

            if forced_profile_id is None:
                # A pinned session's requests must never move the shared
                # rotation pointer or fire a "Rotated" notification — other
                # concurrent terminals may be relying on normal rotation at
                # the exact same time (see handle()'s docstring).
                with self._lock:
                    self._current_profile_id = profile.id
                self._persist()
                if previous_profile_id is not None and previous_profile_id != profile.id:
                    prev_name = self._profile_name(pool, previous_profile_id)
                    activity.record("rotation", f"Rotated {prev_name} → {profile.name}")
                    notifications.notify_if_enabled("rotated", "Claude Unlimited",
                                                      f"Rotated {prev_name} → {profile.name}.", pool.settings)
                    # A real rotation switch clears the PREVIOUS Profile's
                    # "Used now" immediately rather than leaving it to
                    # linger for the rest of _USED_NOW_GRACE_SECONDS — the
                    # whole point of that grace window is to survive short
                    # gaps BETWEEN requests on the same still-in-use
                    # Profile, not to keep showing a Profile as active
                    # after rotation has genuinely moved on from it. Safe
                    # even if some other pinned session is concurrently
                    # using previous_profile_id too: that session's own
                    # in-flight marking (self._in_flight, not
                    # self._last_active) is untouched by this, so
                    # in_flight_ids() still reports it correctly.
                    with self._lock:
                        self._last_active.pop(previous_profile_id, None)

            project_id = None
            try:
                session_id = project_attribution.session_id_from_headers(headers)
                if session_id:
                    project_id = project_attribution.resolve_project(session_id)
                    if project_id:
                        project_usage.record_request(project_id)
            except Exception:
                project_id = None  # best-effort attribution — must never affect a real request

            body_chunks = self._wrap_with_usage_capture(resp.body_chunks, resp.headers, profile.id, project_id)
            body_chunks = self._wrap_with_in_flight_clear(body_chunks, profile.id)
            return GatewayResult(status=resp.status, headers=resp.headers, body_chunks=body_chunks,
                                  profile_id=profile.id)

        activity.record("error", "Rotation attempts exhausted without a usable Profile")
        notifications.notify_if_enabled("needs_attention", "Claude Unlimited",
                                          "Rotation attempts exhausted — no usable Profile was found.", pool.settings)
        return GatewayResult(status=503, headers={}, body_chunks=None, profile_id=None,
                              error="rotation_attempts_exhausted")

    def _maybe_check_oauth_credential(self, p: Profile, rt: ProfileRuntime) -> Optional[ProfileState]:
        """Runs at most once every _REFRESH_CHECK_COOLDOWN_SECONDS per
        Profile, called from every _sync_snapshot — i.e. every Dashboard
        poll tick AND the daemon's own periodic background thread (see
        daemon.py), NOT just when a live proxy request happens to route
        to this exact Profile.

        Without this, _maybe_refresh_credential's proactive refresh only
        ever fires from inside handle(), which only runs for a Profile
        choose() actually selected — a Profile that's ELIGIBLE but simply
        idle (not picked this rotation, or nobody sent any request at all
        for a while) can sail past its access token's real expiry with
        zero refresh attempts, then get a genuine 401 the next time it
        IS picked... and a Profile that's already AUTH_INVALID is by
        definition never selected by choose() again, so it could never
        even reach that per-request refresh path to begin with — a stuck
        Profile had no way back except a full manual re-auth, even when
        its refresh_token alone would have worked fine. Sitting on the
        non-current side of rotation for longer than the access token's TTL
        is enough to strand a Profile this way.

        Returns ProfileState.ELIGIBLE if this call just recovered an
        AUTH_INVALID Profile (caller should transition it). Returns None
        for every other outcome — not oauth, not due for a check yet,
        healthy and nowhere near expiry, or a refresh that was attempted
        but failed (a genuinely dead/revoked refresh_token correctly
        stays AUTH_INVALID; this never fakes a recovery)."""
        if p.kind != "oauth" or rt.state == ProfileState.DISABLED:
            return None
        try:
            stored = secret_store.get_token(p.id)
        except Exception:
            return None
        cred = oauth_credential.decode(stored)
        if not cred.refresh_token:
            return None
        was_auth_invalid = rt.state == ProfileState.AUTH_INVALID
        if not was_auth_invalid and not oauth_credential.is_expiring_soon(cred):
            return None  # healthy and not close to expiry — nothing to do yet
        # An AUTH_INVALID Profile's access token already proved itself dead
        # via a real 401 — worth trying the refresh_token unconditionally,
        # regardless of what its stored expires_at claims (that's exactly
        # is_expiring_soon's gate, which only makes sense for the
        # preventive/not-yet-broken case). _try_refresh still owns the
        # shared throttle either way — see its own docstring for why that
        # must never be bypassed.
        try:
            refreshed = self._try_refresh(p.id, cred.refresh_token)
        except oauth_login.OAuthLoginError as exc:
            if was_auth_invalid:
                activity.record("error", f"{p.name} — automatic recovery attempt failed", meta=str(exc)[:200])
            return None
        if refreshed is None:
            return None  # still within the shared backoff window — not due yet
        try:
            profile_repo.update_credential(
                p.id, refreshed.access_token,
                refresh_token=refreshed.refresh_token or cred.refresh_token,
                expires_at=refreshed.expires_at,
            )
        except Exception:
            return None
        if was_auth_invalid:
            activity.record("rotation", f"{p.name} — token refreshed automatically, back online")
            return ProfileState.ELIGIBLE
        return None

    def _handle_codex(self, profile: Profile, credential: str, method: str, path: str, headers: dict,
                       body: bytes, now: datetime, forced_profile_id: Optional[str],
                       previous_profile_id: Optional[str], pool: Pool) -> Optional["GatewayResult"]:
        """The codex-kind analogue of handle()'s main oauth/api body — kept
        as a separate method rather than inlined in the same branch,
        because openai_bridge.run() owns its own HTTP call and response
        translation entirely (it is not a thin build-request/send pair the
        rest of handle()'s shared plumbing can operate on unmodified the
        way an UpstreamResponse can). Mirrors the surrounding loop's own
        contract: returns a GatewayResult to end the request (success or a
        pinned-Profile failure), or None to mean "this attempt failed in a
        way that should rotate to the next eligible Profile" — the caller
        does the actual `continue`.

        Only /v1/messages is bridged for real; every other path a real
        Claude Code session hits (count_tokens, models list, ...) has no
        faithful OpenAI equivalent to translate to, so those get a
        lightweight local answer instead of being mistranslated."""
        if path != "/v1/messages":
            return self._codex_non_messages_response(profile, path, body)

        with self._lock:
            self._in_flight.add(profile.id)

        try:
            result = openai_bridge.run(profile, credential, body)
        except openai_bridge.OpenAIBridgeError as exc:
            with self._lock:
                self._mark_profile_idle(profile.id)
            self._observe(profile.id, ProviderUnavailable(retry_after_seconds=None), now)
            if forced_profile_id is not None:
                activity.record("error", f"{profile.name} — could not reach OpenAI",
                                 meta=f"pinned profile, not rotating ({exc})")
                return GatewayResult(status=503, headers={}, body_chunks=None, profile_id=None,
                                      error="upstream_unreachable")
            activity.record("error", f"{profile.name} — could not reach OpenAI",
                             meta=f"network error, rotating to next eligible profile ({exc})")
            return None

        observation = openai_observation.classify(
            result.status, _filter_openai_headers(result.headers), now)

        old_rt = self._runtime.get(profile.id)
        old_state = old_rt.state if old_rt is not None else None
        with self._lock:
            self._observe(profile.id, observation, now)
        new_rt = self._runtime.get(profile.id)
        self._persist()

        if isinstance(observation, AuthInvalid) and old_state != ProfileState.AUTH_INVALID:
            activity.record("error", f"{profile.name} needs re-authentication", meta="credential rejected")
            notifications.notify_if_enabled("needs_attention", "Claude Unlimited",
                                              f"{profile.name} needs re-authentication.", pool.settings)

        if isinstance(observation, UsageSnapshot) and new_rt is not None and new_rt.state == ProfileState.ELIGIBLE:
            near_threshold = observation.percent >= new_rt.switch_threshold - APPROACHING_THRESHOLD_BAND
            if near_threshold and profile.id not in self._warned_approaching:
                self._warned_approaching.add(profile.id)
                notifications.notify_if_enabled(
                    "approaching_threshold", "Claude Unlimited",
                    f"{profile.name} is approaching its switch threshold "
                    f"({observation.percent:.0f}% / {new_rt.switch_threshold:.0f}%).", pool.settings)
            elif not near_threshold:
                self._warned_approaching.discard(profile.id)

        if isinstance(observation, QuotaExhausted):
            if forced_profile_id is not None:
                activity.record("rotation", f"{profile.name} hit its quota",
                                 meta="pinned profile — returning the real response, not rotating")
            else:
                with self._lock:
                    self._mark_profile_idle(profile.id)
                activity.record("rotation", f"{profile.name} hit its quota", meta="rotating to next eligible profile")
                return None

        if forced_profile_id is None:
            with self._lock:
                self._current_profile_id = profile.id
            self._persist()
            if previous_profile_id is not None and previous_profile_id != profile.id:
                prev_name = self._profile_name(pool, previous_profile_id)
                activity.record("rotation", f"Rotated {prev_name} → {profile.name}")
                notifications.notify_if_enabled("rotated", "Claude Unlimited",
                                                  f"Rotated {prev_name} → {profile.name}.", pool.settings)
                with self._lock:
                    self._last_active.pop(previous_profile_id, None)

        project_id = None
        try:
            session_id = project_attribution.session_id_from_headers(headers)
            if session_id:
                project_id = project_attribution.resolve_project(session_id)
                if project_id:
                    project_usage.record_request(project_id)
        except Exception:
            project_id = None

        # Deliberately NOT result.headers here — those are OpenAI's own raw
        # response headers (Cloudflare ray/cookies, x-codex-* quota
        # telemetry, etc.), already consumed above for rotation/observation
        # purposes but never meant to reach the client: Claude Code expects
        # an Anthropic-shaped response, and leaking a different provider's
        # infrastructure headers through would be a real, visible tell,
        # not just noise. Every codex-kind response is translated SSE
        # (openai_translate.ResponseTranslator's whole job), so this is
        # always the same clean content-type — nothing upstream-specific
        # to preserve.
        # A non-2xx result carries a single plain-JSON error object
        # (openai_bridge.run()'s _error_chunks()), never SSE — only a real
        # 200 is actually the translated event stream.
        content_type = "text/event-stream; charset=utf-8" if result.status < 300 else "application/json"
        client_headers = {"content-type": content_type}
        body_chunks = self._wrap_with_usage_capture(result.body_chunks, client_headers, profile.id, project_id)
        body_chunks = self._wrap_with_in_flight_clear(body_chunks, profile.id)

        # The upstream Responses call is always streamed, but the client
        # decides how it wants the answer back. A client that asked for
        # stream:false cannot parse an SSE body and reads the whole model as
        # unavailable — which is how Claude Code's auto-mode safety
        # classifier (a non-streaming call) ends up blocking tools that need
        # a safety decision.
        if result.status < 300 and not _client_wants_streaming(body):
            message = openai_translate.assemble_message_from_sse(body_chunks)
            payload = json.dumps(message).encode("utf-8")
            client_headers = {"content-type": "application/json"}
            return GatewayResult(status=result.status, headers=client_headers,
                                  body_chunks=iter([payload]), profile_id=profile.id)

        return GatewayResult(status=result.status, headers=client_headers, body_chunks=body_chunks,
                              profile_id=profile.id)

    @staticmethod
    def _models_listing_response(profile: Profile, path: str, listing: list) -> "GatewayResult":
        """Anthropic's own /v1/models wire shape, answered locally for a
        connector that has no Anthropic-compatible backend to relay to.
        Handles both the list and the /v1/models/{id} retrieve form, since
        the client SDK calls each."""
        import json as _json

        def entry(model_id: str, display_name: str) -> dict:
            return {"type": "model", "id": model_id, "display_name": display_name,
                    "created_at": "2025-01-01T00:00:00Z"}

        requested_id = path[len("/v1/models/"):].strip("/") if path.startswith("/v1/models/") else ""
        if requested_id:
            match = next(((i, n) for i, n in listing if i == requested_id), None)
            if match is None:
                payload = _json.dumps({"type": "error", "error": {
                    "type": "not_found_error", "message": f"model {requested_id!r} not found"}}).encode("utf-8")
                return GatewayResult(status=404, headers={"content-type": "application/json"},
                                      body_chunks=iter([payload]), profile_id=profile.id)
            payload = _json.dumps(entry(*match)).encode("utf-8")
        else:
            data = [entry(i, n) for i, n in listing]
            payload = _json.dumps({
                "data": data, "has_more": False,
                "first_id": data[0]["id"] if data else None,
                "last_id": data[-1]["id"] if data else None,
            }).encode("utf-8")
        return GatewayResult(status=200, headers={"content-type": "application/json"},
                              body_chunks=iter([payload]), profile_id=profile.id)

    @staticmethod
    def _codex_non_messages_response(profile: Profile, path: str, body: bytes) -> "GatewayResult":
        """A codex-kind Profile has no real Anthropic-compatible backend to
        relay these to — answer locally rather than mistranslate. Claude
        Code calls count_tokens before a real turn fairly often; a rough
        chars/4 heuristic (openly approximate, never billed against — this
        daemon does not charge for it) is far better than erroring on
        every single message this Profile serves."""
        if path == "/v1/messages/count_tokens":
            import json as _json
            try:
                parsed = _json.loads(body) if body else {}
            except _json.JSONDecodeError:
                parsed = {}
            text_len = len(_json.dumps(parsed.get("messages", []))) + len(str(parsed.get("system", "")))
            estimate = max(1, text_len // 4)
            payload = _json.dumps({"input_tokens": estimate}).encode("utf-8")
            return GatewayResult(status=200, headers={"content-type": "application/json"},
                                  body_chunks=iter([payload]), profile_id=profile.id)
        payload = b'{"type":"error","error":{"type":"not_found_error","message":"Not supported for a codex Profile."}}'
        return GatewayResult(status=404, headers={"content-type": "application/json"},
                              body_chunks=iter([payload]), profile_id=profile.id)

    def _try_refresh(self, profile_id: str, refresh_token: str) -> Optional["oauth_login.LoginTokens"]:
        """The ONE place that actually calls oauth_login.refresh_access_token
        — shared by _maybe_refresh_credential (the per-request path, called
        from inside handle() for whichever Profile choose() just picked) and
        _maybe_check_oauth_credential (the sync-driven path, covering idle
        and AUTH_INVALID Profiles) specifically so they share ONE per-Profile
        backoff clock (self._refresh_check_not_before), not two independent
        ones.

        This is load-bearing. With independent clocks, one path can take a
        429 and the other retries the same Profile moments later, unaware.
        Whichever path hits a 429 first must block BOTH until the backoff
        clears. Returns None (not an exception) when still within the
        backoff window — that is a normal, silent "not due yet" outcome,
        never logged as a failure by either caller. Raises
        oauth_login.OAuthLoginError, unchanged, for a real (non-throttled)
        failure so each caller can decide how to log/react to that."""
        now = time.monotonic()
        not_before = self._refresh_check_not_before.get(profile_id)
        if not_before is not None and now < not_before:
            return None
        self._refresh_check_not_before[profile_id] = now + self._REFRESH_CHECK_COOLDOWN_SECONDS
        try:
            return oauth_login.refresh_access_token(refresh_token)
        except oauth_login.OAuthLoginError as exc:
            if exc.status_code == 429:
                # Anthropic's own rate limiter, not a dead credential —
                # retrying this every _REFRESH_CHECK_COOLDOWN_SECONDS (60s)
                # never let the window actually clear, since each attempt
                # is itself another strike against the same limiter. Back
                # off much further before ANY path tries this Profile again.
                self._refresh_check_not_before[profile_id] = now + self._RATE_LIMIT_BACKOFF_SECONDS
            raise

    def _maybe_refresh_credential(self, profile: Profile, stored: str) -> str:
        """Proactively refreshes an OAuth Profile's access token before it's
        used, if it's within oauth_credential.EXPIRING_SOON_BUFFER_MS of its
        known expiry (or already past it) and a refresh_token is on hand.
        Without this, every OAuth Profile eventually goes stale and needs a
        full manual re-auth no matter how "healthy" it looked a moment ago —
        the access token itself has a real, usually short, expiry, and
        nothing else refreshes it — so a Profile can look healthy and still
        fail on its very next request.

        Always returns the actual bare access token to use — NEVER the raw
        `stored` string as-is, which for a Profile using the new blob shape
        (anything with a refresh_token) is a JSON object, not a token; using
        it directly as the Bearer credential would send Anthropic garbage.

        A silent no-op for a Profile with no known expiry (covers every
        Profile that predates this feature, and any manually pasted token,
        which never has a refresh_token at all), if still within
        _try_refresh's shared backoff window, or if the refresh itself
        fails — that falls through to sending the possibly-stale but still
        real access token exactly as before this existed, so a real 401
        still correctly lands the Profile on AUTH_INVALID rather than this
        method blocking the request pipeline on a refresh failure."""
        cred = oauth_credential.decode(stored)
        if not cred.refresh_token or not oauth_credential.is_expiring_soon(cred):
            return cred.access_token
        try:
            refreshed = self._try_refresh(profile.id, cred.refresh_token)
        except oauth_login.OAuthLoginError as exc:
            # Previously silent — a Profile could sit here failing to
            # refresh every single request with zero trace anywhere,
            # indistinguishable from "nothing tried." Now visible in
            # Activity so it is visible rather than inferred from
            # request timing again.
            activity.record("error", f"{profile.name} — proactive token refresh failed",
                             meta=str(exc)[:200])
            return cred.access_token
        if refreshed is None:
            return cred.access_token  # still within the shared backoff window — not due yet
        try:
            profile_repo.update_credential(
                profile.id, refreshed.access_token,
                refresh_token=refreshed.refresh_token or cred.refresh_token,
                expires_at=refreshed.expires_at,
            )
        except Exception as exc:
            # Anthropic ROTATES the refresh token on every refresh: the one we
            # just spent is now dead server-side. If persisting the replacement
            # fails we are left holding an invalidated token, every future
            # refresh fails with invalid_grant, and the Profile ends up needing
            # a full manual re-auth. This used to be a bare `pass`, so the one
            # failure that causes exactly that left no trace anywhere and had
            # to be inferred. This request still succeeds on the
            # token we just got — but say so loudly, because it is the last
            # one that will work.
            activity.record("error", f"{profile.name} — could not save refreshed credential",
                             meta=f"re-auth will be required: {str(exc)[:160]}")
            notifications.notify_if_enabled(
                "needs_attention", "Claude Unlimited",
                f"{profile.name}: could not save its refreshed login — it will need re-authentication.",
                load_pool().settings)
        return refreshed.access_token

    def force_active(self, profile_id: str) -> bool:
        """The Dashboard's "Take over" action: immediately makes this
        Profile the active one for the next request, bypassing normal
        priority/threshold/rotation selection. Resets its live state to
        ELIGIBLE regardless of what it was (DRAINING past its threshold,
        EXHAUSTED, COOLDOWN, even AUTH_INVALID) — the whole point of a
        deliberate manual override is to try it right now anyway; the very
        next real request is the honest test of whether it's actually
        usable, and a stale state that doesn't reflect reality just gets
        re-observed correctly from that real response.

        Returns False without doing anything for a disabled Profile
        (respects that as explicit user intent — a "Take over" action
        implicitly re-enabling it would be surprising) or one that no
        longer exists. True on success."""
        with self._lock:
            pool = load_pool()
            profile = pool.get(profile_id)
            if profile is None or not profile.enabled:
                return False
            snapshot = self._sync_snapshot(pool)
            self._runtime = {rt.profile_id: rt for rt in snapshot.profiles}
            rt = self._runtime.get(profile_id)
            if rt is None:
                return False
            self._runtime[profile_id] = replace(rt, state=ProfileState.ELIGIBLE,
                                                  cooldown_until=None, resets_at=None)
            self._current_profile_id = profile_id
        self._persist()
        activity.record("rotation", f"{profile.name} manually taken over",
                         meta="overrides rotation/threshold")
        return True

    def _maybe_retry_with_default_model(self, profile: Profile, credential: str, method: str, path: str,
                                          headers: dict, body: bytes, now: datetime):
        """Retries ONCE against profile.default_model instead of whatever
        model the client actually asked for — called only when the first
        attempt already failed with a status in _MODEL_FALLBACK_STATUS_CODES
        (see handle()). A real scenario this fixes: running `/model` in a
        Claude Code session routed through an API-kind Profile whose key
        doesn't have that model — before this, the failure got surfaced (or,
        pre the classify() 401/403 fix, actively mis-surfaced as "needs
        re-authentication") instead of just falling back to the model that
        Profile was actually configured for.

        Returns None (nothing to retry) when the body has no "model" field,
        or when it already IS default_model — retrying with an identical
        body would just reproduce the exact same failure. On a network
        error during the retry itself, also returns None (caller keeps the
        original failed response/observation — a second network failure is
        not this method's problem to solve)."""
        requested_model = request_model(body)
        if requested_model is None or requested_model == profile.default_model:
            return None
        retry_body = rewrite_model(body, profile.default_model)
        try:
            retry_req = build_upstream_request(profile, credential, method, path, headers, retry_body)
        except ValueError:
            return None
        try:
            retry_resp = self._transport(retry_req)
        except OSError:
            return None
        retry_observation = classify(retry_resp.status, filter_response_headers(retry_resp.headers), now)
        activity.record("config", f"{profile.name} — retried with its default model",
                         meta=f"{requested_model} unavailable, used {profile.default_model} instead")
        return retry_resp, retry_observation

    def _forced_decision(self, pool: Pool, forced_profile_id: str) -> RoutingDecision:
        """Routing for a `forced_profile_id` request (see handle()) — always
        picks exactly that Profile, bypassing priority/threshold ranking the
        same way force_active() does, EXCEPT for AUTH_INVALID: force_active()
        is a one-shot manual click where "try it anyway, the next response
        is the honest test" is the right call, but a forced session hits
        this on every single request for as long as it's pinned (session
        tokens live up to session_tokens.SESSION_TOKEN_TTL) — retrying a
        credential already known to be dead on every request would just
        waste a round trip and surface a raw upstream 401 instead of one
        clear local message. Caller must already hold self._lock and have
        refreshed self._runtime from a current snapshot."""
        profile = pool.get(forced_profile_id)
        if profile is None:
            return RoutingDecision(profile_id=None, reason="forced_profile_missing")
        if not profile.enabled:
            return RoutingDecision(profile_id=None, reason="forced_profile_disabled")
        rt = self._runtime.get(forced_profile_id)
        if rt is not None and rt.state == ProfileState.AUTH_INVALID:
            return RoutingDecision(profile_id=None, reason="forced_profile_needs_reauth")
        return RoutingDecision(profile_id=forced_profile_id, reason="forced")

    def _persist(self) -> None:
        """Best-effort snapshot of the Dashboard-visible runtime fields to
        disk (runtime_state.py) — never allowed to affect a real request,
        so any failure here is swallowed, not raised."""
        with self._lock:
            current_profile_id = self._current_profile_id
            profiles = {
                pid: {
                    "last_usage_percent": rt.last_usage_percent,
                    "resets_at": rt.resets_at.isoformat() if rt.resets_at else None,
                    "last_usage_percent_7d": rt.last_usage_percent_7d,
                    "resets_at_7d": rt.resets_at_7d.isoformat() if rt.resets_at_7d else None,
                }
                for pid, rt in self._runtime.items()
            }
        try:
            runtime_state.save(current_profile_id, profiles)
        except Exception:
            pass

    @staticmethod
    def _wrap_with_usage_capture(chunks, resp_headers: dict, profile_id: str, project_id: Optional[str]):
        """Tees the response body through UsageCapture (see its module
        docstring for the safety invariant: every byte forwarded exactly
        unchanged) and records one usage_history event as soon as the
        capture has what it needs.

        Recording lives in a `finally`, not directly after the `yield from` —
        verified as a real, live bug: the real `claude` CLI routinely closes
        its socket right after it has parsed what it needs, before the
        daemon's write loop finishes draining this generator. That raises
        BrokenPipeError/ConnectionResetError in daemon.py's write loop, which
        catches it and returns — abandoning this generator without ever
        reaching code placed directly after `yield from`, even though
        parsing (a side effect of pulling each chunk, which already
        happened) had already captured a complete model+usage. Confirmed via
        a real captured request: project attribution recorded 12 real
        requests while usage_history recorded 0, because project attribution
        happens before any bytes are streamed while this happens after.
        `finally` runs on that same abandonment too — CPython closes a
        garbage-collected generator by throwing GeneratorExit into it at its
        suspension point, which unwinds through `finally` normally — so this
        now records real capability actually used, not just the lucky case
        where a client happens to keep reading until the literal last byte."""
        if chunks is None:
            return chunks
        content_type = {k.lower(): v for k, v in resp_headers.items()}.get("content-type")
        capture = usage_tracking.UsageCapture()

        def generator():
            try:
                yield from capture.wrap(chunks, content_type)
            finally:
                if capture.model and capture.usage:
                    try:
                        usage_history.record(profile_id, project_id, capture.model, capture.usage)
                    except Exception:
                        pass  # usage history is best-effort — must never affect a real request

        return generator()

    def _mark_profile_idle(self, profile_id: str) -> None:
        """Moves a Profile out of `_in_flight` and starts its "Used now"
        grace period — call with `self._lock` held. Centralized so every
        exit path (forced-return, rotate-and-continue, or a fully-drained
        response) records the same last-active timestamp; a call site that
        only did `self._in_flight.discard(...)` would make that Profile's
        "Used now" pill vanish instantly instead of fading out like the
        others."""
        self._in_flight.discard(profile_id)
        self._last_active[profile_id] = time.monotonic()

    def _wrap_with_in_flight_clear(self, chunks, profile_id: str):
        """Clears profile_id from self._in_flight once its response is
        fully drained — or, via the same `finally`-under-GeneratorExit
        mechanism _wrap_with_usage_capture relies on (see its own
        docstring), as soon as the client disconnects mid-stream instead of
        staying marked "in use" forever."""
        if chunks is None:
            with self._lock:
                self._mark_profile_idle(profile_id)
            return chunks

        def generator():
            try:
                yield from chunks
            finally:
                with self._lock:
                    self._mark_profile_idle(profile_id)

        return generator()

    def in_flight_ids(self) -> set[str]:
        """Profile ids to show as "Used now" — either a request is
        literally being served right now (see self._in_flight's own
        docstring), or one finished within the last _USED_NOW_GRACE_SECONDS.
        The grace period exists because a quick non-streaming call can
        complete well inside the Dashboard's poll interval, making the
        indicator otherwise flicker on and off between ticks (or be missed
        entirely) instead of being reliably visible for a moment after real
        usage."""
        now = time.monotonic()
        with self._lock:
            recent = {pid for pid, ts in self._last_active.items()
                      if now - ts < self._USED_NOW_GRACE_SECONDS}
            return self._in_flight | recent

    def runtime_snapshot(self) -> dict[str, ProfileRuntime]:
        """Read-only view of live per-Profile Rotation state, synced against
        the current Pool first — safe to call anytime (e.g. from the
        Dashboard's GET /api/profiles), not just from inside handle()."""

        with self._lock:
            pool = load_pool()
            snapshot = self._sync_snapshot(pool)
            self._runtime = {rt.profile_id: rt for rt in snapshot.profiles}
            return dict(self._runtime)

    @staticmethod
    def _profile_name(pool: Pool, profile_id: str) -> str:
        p = pool.get(profile_id)
        return p.name if p is not None else profile_id

    def _observe(self, profile_id: str, observation, now: datetime) -> None:
        snapshot = PoolSnapshot(profiles=list(self._runtime.values()), current_profile_id=self._current_profile_id)
        updated = observe(snapshot, profile_id, observation, now)
        self._runtime = {rt.profile_id: rt for rt in updated.profiles}
