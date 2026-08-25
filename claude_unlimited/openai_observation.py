"""Classifies an OpenAI/Codex-backend response's status and headers into the
same Observation union observation.py defines for the Anthropic side.

router.py's rotation logic already works over that union, so a codex-kind
Profile needs a producer of it rather than a new consumer. Like
observation.py, this stays metadata-only: status and headers, never response
bodies.

IMPORTANT: the primary/secondary split is NOT a fixed "primary = 5h,
secondary = 7d" mapping the way Anthropic's unified headers are. Each window
is independently labeled by its own `window-minutes` value (5h, daily,
weekly, monthly or annual), and a window reporting used_percent 0 with no
window-minutes and no reset time is ABSENT rather than a real 0%-used
window. A codex Profile can genuinely have only one quota window, so never
assume two exist.
"""

from __future__ import annotations

from datetime import datetime, timezone
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

ALLOWED_HEADERS = (
    "x-codex-primary-used-percent",
    "x-codex-primary-reset-after-seconds",
    "x-codex-primary-reset-at",
    "x-codex-primary-window-minutes",
    "x-codex-secondary-used-percent",
    "x-codex-secondary-reset-after-seconds",
    "x-codex-secondary-reset-at",
    "x-codex-secondary-window-minutes",
    "x-codex-plan-type",
    "x-codex-credits-has-credits",
    "x-codex-credits-balance",
    "retry-after",
)

# Known window durations in minutes, with 5% slack: a "weekly" window is not
# always reported as exactly 10080 minutes.
_WINDOW_LABELS = (
    (5 * 60, "5h"),
    (24 * 60, "daily"),
    (7 * 24 * 60, "weekly"),
    (30 * 24 * 60, "monthly"),
    (365 * 24 * 60, "annual"),
)
_WINDOW_TOLERANCE = 0.05


def _parse_float(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _reset_from_epoch(raw: Optional[str]) -> Optional[datetime]:
    epoch = _parse_float(raw)
    return datetime.fromtimestamp(epoch, tz=timezone.utc) if epoch is not None else None


def _label_for_window_minutes(minutes: Optional[float]) -> Optional[str]:
    """None for an unrecognized duration rather than a guessed label; the
    Dashboard then shows a bare percentage with no duration word."""
    if minutes is None:
        return None
    for candidate_minutes, label in _WINDOW_LABELS:
        if abs(minutes - candidate_minutes) <= candidate_minutes * _WINDOW_TOLERANCE:
            return label
    return None


def label_from_reset_distance(resets_at: Optional[datetime], now: datetime) -> Optional[str]:
    """Fallback for when the backend reports a window's reset time but omits
    x-codex-*-window-minutes.

    Time-until-reset is always <= the window's own length, so the smallest
    known window that still fits the remaining time is the only one it can
    be: a reset 7 days out cannot be a 5h or daily window. Deliberately
    approximate, and only used when the precise header is missing."""
    if resets_at is None:
        return None
    remaining_minutes = (resets_at - now).total_seconds() / 60.0
    if remaining_minutes <= 0:
        return None
    for candidate_minutes, label in _WINDOW_LABELS:
        if remaining_minutes <= candidate_minutes * (1 + _WINDOW_TOLERANCE):
            return label
    return None


class _Window:
    __slots__ = ("used_percent", "resets_at", "label")

    def __init__(self, used_percent: float, resets_at: Optional[datetime], label: Optional[str]):
        self.used_percent = used_percent
        self.resets_at = resets_at
        self.label = label


def _parse_window(headers: dict[str, str], prefix: str, now: datetime) -> Optional[_Window]:
    """One of "primary"/"secondary" -> a _Window, or None if the response
    didn't report one. An all-zero window counts as absent, not as a real
    0%-used window (see the module docstring)."""
    used_percent = _parse_float(headers.get(f"x-codex-{prefix}-used-percent"))
    window_minutes = _parse_float(headers.get(f"x-codex-{prefix}-window-minutes"))
    reset_at_raw = headers.get(f"x-codex-{prefix}-reset-at")
    has_data = bool(used_percent) or bool(window_minutes) or bool(reset_at_raw)
    if not has_data:
        return None
    resets_at = _reset_from_epoch(reset_at_raw)
    label = _label_for_window_minutes(window_minutes) or label_from_reset_distance(resets_at, now)
    return _Window(used_percent=used_percent or 0.0, resets_at=resets_at, label=label)


def classify(status_code: int, headers: dict[str, str], now: datetime) -> Observation:
    """headers must already have lowercased keys and be restricted to
    ALLOWED_HEADERS by the caller, matching observation.classify()'s
    contract."""
    if status_code == 401:
        return AuthInvalid()

    if status_code == 429:
        primary = _parse_window(headers, "primary", now)
        # This backend has no structured "fully exhausted" signal distinct
        # from a bare rate-limit, so a used-percent at or near 100 on
        # whichever window exists is treated as QuotaExhausted, mirroring
        # Anthropic's `-status: rejected` handling. Anything else becomes a
        # ShortRateLimit, which router.py handles with escalating backoff
        # even without a retry-after.
        if primary is not None and primary.used_percent >= 99.5:
            return QuotaExhausted(resets_at=primary.resets_at)
        retry_after = _parse_float(headers.get("retry-after"))
        return ShortRateLimit(retry_after_seconds=retry_after)

    if status_code in (500, 502, 503, 529):
        retry_after = _parse_float(headers.get("retry-after"))
        return ProviderUnavailable(retry_after_seconds=retry_after)

    if 200 <= status_code < 300:
        primary = _parse_window(headers, "primary", now)
        secondary = _parse_window(headers, "secondary", now)
        # Report whichever windows came back. Never assume "primary" means
        # 5h or "secondary" means 7d; each is labeled by its own duration,
        # or left unlabeled if unrecognized. With neither window populated
        # there is nothing to say about usage, so return Unknown rather
        # than a fabricated 0%.
        if primary is None and secondary is None:
            return Unknown(status_code=status_code)
        if primary is None:
            # Only the second slot is populated, so surface it as the
            # primary displayed number; there is no first window to prefer.
            primary, secondary = secondary, None
        return UsageSnapshot(
            percent=primary.used_percent,
            resets_at=primary.resets_at,
            confidence="measured",
            percent_7d=secondary.used_percent if secondary else None,
            resets_at_7d=secondary.resets_at if secondary else None,
            window_label=primary.label,
            window_label_7d=secondary.label if secondary else None,
        )

    return Unknown(status_code=status_code)
