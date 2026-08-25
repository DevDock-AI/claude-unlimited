"""Classifies upstream response metadata into explicit Observation types.

Pure, deterministic, no I/O. Takes only response metadata — the status code
and an allowlist of header values the Proxy module extracts — never response
bodies. That is what lets the Daemon forward request and response bodies as
opaque streams while still making Rotation decisions from structured data.

Only a usage snapshot at or above the threshold, or quota exhaustion,
triggers Rotation. A bare rate-limit never does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class UsageSnapshot:
    percent: float
    resets_at: Optional[datetime]
    confidence: str  # "measured" | "estimated"
    percent_7d: Optional[float] = None  # the unified-7d sibling of `percent`; display-only, never drives Rotation
    resets_at_7d: Optional[datetime] = None
    # What the two window slots above represent. Anthropic always reports
    # both a 5h and a 7d window, but a codex-kind Profile may report a
    # single window of some other duration (e.g. "weekly"), so the label is
    # carried rather than assumed. None means the Anthropic default of
    # 5h/7d.
    window_label: Optional[str] = None
    window_label_7d: Optional[str] = None


@dataclass(frozen=True)
class QuotaExhausted:
    resets_at: Optional[datetime]


@dataclass(frozen=True)
class ShortRateLimit:
    retry_after_seconds: Optional[float]


@dataclass(frozen=True)
class ProviderUnavailable:
    retry_after_seconds: Optional[float]


@dataclass(frozen=True)
class AuthInvalid:
    pass


@dataclass(frozen=True)
class Unknown:
    status_code: int


Observation = UsageSnapshot | QuotaExhausted | ShortRateLimit | ProviderUnavailable | AuthInvalid | Unknown


# The unified rate-limit headers Anthropic returns, and their wire format:
#   anthropic-ratelimit-unified-status: allowed | rejected
#   anthropic-ratelimit-unified-5h-status: allowed | rejected
#   anthropic-ratelimit-unified-5h-reset: 1787191800    Unix epoch seconds, not ISO 8601
#   anthropic-ratelimit-unified-5h-utilization: 0.61    0-1 float, not a remaining/limit pair
#   anthropic-ratelimit-unified-7d-status / -reset / -utilization: same shapes
#   anthropic-ratelimit-unified-reset: 1787191800
#   anthropic-ratelimit-unified-overage-status: allowed | rejected
ALLOWED_HEADERS = (
    "anthropic-ratelimit-unified-status",
    "anthropic-ratelimit-unified-5h-status",
    "anthropic-ratelimit-unified-5h-utilization",
    "anthropic-ratelimit-unified-5h-reset",
    "anthropic-ratelimit-unified-7d-status",
    "anthropic-ratelimit-unified-7d-utilization",
    "anthropic-ratelimit-unified-7d-reset",
    "anthropic-ratelimit-unified-reset",
    "anthropic-ratelimit-unified-overage-status",
    "retry-after",
)


def classify(status_code: int, headers: dict[str, str], now: datetime) -> Observation:
    """headers must already have lowercased keys and be restricted to
    ALLOWED_HEADERS by the caller (the Proxy module); this function does not
    filter them."""

    if status_code == 401:
        return AuthInvalid()

    # 403 is deliberately NOT AuthInvalid. 401 (authentication_error) means
    # the credential is rejected; 403 (permission_error) means the
    # credential is valid but isn't allowed to do this specific thing,
    # usually a model it isn't scoped for. Treating them alike would mark a
    # Profile "needs re-authentication" over a model choice. It falls
    # through to Unknown, which gateway.py relays untouched and may retry
    # once against default_model — see
    # gateway._maybe_retry_with_default_model.

    if status_code == 429:
        # The two windows are checked independently, not as `5h or 7d`: a
        # Profile can have 5h headroom while the weekly cap is what rejected
        # the request. Collapsing them would misclassify quota exhaustion as
        # a bare rate-limit, and the Router would cooldown-and-retry the same
        # Profile instead of rotating away. Each window reports its own reset
        # time.
        status_5h = headers.get("anthropic-ratelimit-unified-5h-status")
        status_7d = headers.get("anthropic-ratelimit-unified-7d-status")
        if status_5h == "rejected":
            resets_at = _parse_reset(headers.get("anthropic-ratelimit-unified-5h-reset"))
            return QuotaExhausted(resets_at=resets_at)
        if status_7d == "rejected":
            resets_at = _parse_reset(headers.get("anthropic-ratelimit-unified-7d-reset"))
            return QuotaExhausted(resets_at=resets_at)
        retry_after = _parse_float(headers.get("retry-after"))
        return ShortRateLimit(retry_after_seconds=retry_after)

    if status_code == 529 or status_code == 503:
        retry_after = _parse_float(headers.get("retry-after"))
        return ProviderUnavailable(retry_after_seconds=retry_after)

    if 200 <= status_code < 300:
        snapshot = _usage_snapshot_from_headers(headers)
        if snapshot is not None:
            return snapshot

    return Unknown(status_code=status_code)


def _usage_snapshot_from_headers(headers: dict[str, str]) -> Optional[UsageSnapshot]:
    utilization = _parse_float(headers.get("anthropic-ratelimit-unified-5h-utilization"))
    if utilization is None:
        return None
    percent = utilization * 100
    resets_at = _parse_reset(headers.get("anthropic-ratelimit-unified-5h-reset"))

    utilization_7d = _parse_float(headers.get("anthropic-ratelimit-unified-7d-utilization"))
    percent_7d = round(utilization_7d * 100, 2) if utilization_7d is not None else None
    resets_at_7d = _parse_reset(headers.get("anthropic-ratelimit-unified-7d-reset"))

    return UsageSnapshot(percent=round(percent, 2), resets_at=resets_at, confidence="measured",
                          percent_7d=percent_7d, resets_at_7d=resets_at_7d)


def _parse_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_reset(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    # Anthropic sends Unix epoch seconds, so try that first; fall back to
    # ISO 8601 in case another endpoint or version sends that instead.
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
