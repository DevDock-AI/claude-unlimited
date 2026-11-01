"""How long to wait after a failed OAuth token refresh.

One rule for both refresh paths — gateway._try_refresh (Claude) and
openai_bridge.refresh_now (Codex) — so a failure backs off the same way
whichever provider it came from.

Every failure escalates, not only a 429. A refresh that kept failing with a
400 or a network error used to re-arm only the ordinary 60s throttle, so an
account whose grant had died asked the token endpoint to refresh it about
once a minute, indefinitely, and logged nothing while doing so (issue #10).
"""

from __future__ import annotations

from typing import Optional

# A rate limit or a rejected grant: retrying soon cannot help, so these climb
# to hours. At the ceiling that is four attempts a day.
SLOW_CEILING_SECONDS = 6 * 60 * 60.0
# A network error or a 5xx is usually brief, and the token may be about to
# expire, so these stay comparatively responsive.
TRANSIENT_CEILING_SECONDS = 30 * 60.0
# The first wait after the token endpoint rejects the grant outright.
REJECTED_BASE_SECONDS = 600.0


def is_rejection(status_code: Optional[int]) -> bool:
    """A 4xx other than 429: the endpoint answered, and said no to this
    refresh token (invalid_grant and the like). Not something a retry fixes."""
    return status_code is not None and 400 <= status_code < 500 and status_code != 429


def failure_wait(status_code: Optional[int], streak: int, base: float,
                 rate_limit_base: float) -> float:
    """Seconds until the next attempt after the `streak`-th consecutive
    failure (1 for the first). `base` is the caller's ordinary cooldown, so a
    first transient failure waits exactly what a success would have."""
    if status_code == 429:
        first, ceiling = rate_limit_base, SLOW_CEILING_SECONDS
    elif is_rejection(status_code):
        first, ceiling = max(base, REJECTED_BASE_SECONDS), SLOW_CEILING_SECONDS
    else:
        first, ceiling = base, max(base, TRANSIENT_CEILING_SECONDS)
    return min(first * 2 ** (max(1, streak) - 1), ceiling)


def worth_logging(streak: int) -> bool:
    """Log the 1st, 2nd, 4th, 8th... failure in a row: every failure stays
    visible in the Activity log without one stuck account filling it."""
    return streak >= 1 and streak & (streak - 1) == 0


def describe(status_code: Optional[int], streak: int, wait: float) -> str:
    status = f"HTTP {status_code}" if status_code is not None else "network error"
    minutes = max(1, round(wait / 60))
    when = f"{minutes // 60}h" if minutes >= 60 and minutes % 60 == 0 else f"{minutes} min"
    return f"{status}, {streak} in a row — next attempt in {when}"
