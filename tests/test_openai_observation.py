from datetime import datetime, timezone

from claude_unlimited.observation import AuthInvalid, ProviderUnavailable, QuotaExhausted, ShortRateLimit, Unknown, UsageSnapshot
from claude_unlimited.openai_observation import classify

NOW = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)


def test_401_is_auth_invalid():
    assert classify(401, {}, NOW) == AuthInvalid()


def test_200_with_real_quota_headers_is_a_usage_snapshot():
    # Header names and shape as the Codex backend sends them; see this
    # module's docstring.
    headers = {
        "x-codex-primary-used-percent": "43",
        "x-codex-primary-reset-at": "1787523886",
        "x-codex-secondary-used-percent": "12",
        "x-codex-secondary-reset-at": "1788000000",
    }
    obs = classify(200, headers, NOW)
    assert isinstance(obs, UsageSnapshot)
    assert obs.percent == 43.0
    assert obs.confidence == "measured"
    assert obs.percent_7d == 12.0
    assert obs.resets_at is not None


def test_200_with_no_quota_headers_is_unknown_not_a_fabricated_snapshot():
    obs = classify(200, {}, NOW)
    assert obs == Unknown(status_code=200)


def test_429_near_full_usage_is_quota_exhausted():
    headers = {"x-codex-primary-used-percent": "100", "x-codex-primary-reset-at": "1787523886"}
    obs = classify(429, headers, NOW)
    assert isinstance(obs, QuotaExhausted)


def test_429_with_low_usage_is_a_short_rate_limit_not_exhausted():
    headers = {"x-codex-primary-used-percent": "10"}
    obs = classify(429, headers, NOW)
    assert isinstance(obs, ShortRateLimit)


def test_429_with_no_usage_header_at_all_falls_back_to_short_rate_limit():
    obs = classify(429, {}, NOW)
    assert isinstance(obs, ShortRateLimit)
    assert obs.retry_after_seconds is None


def test_429_honors_a_real_retry_after_header():
    obs = classify(429, {"retry-after": "30"}, NOW)
    assert isinstance(obs, ShortRateLimit)
    assert obs.retry_after_seconds == 30.0


def test_5xx_is_provider_unavailable():
    for status in (500, 502, 503, 529):
        assert isinstance(classify(status, {}, NOW), ProviderUnavailable)


def test_other_status_is_unknown():
    assert classify(418, {}, NOW) == Unknown(status_code=418)


# ---- dynamic window detection/labeling: unlike Claude, Codex does not always
# expose a 5h+7d dual window. Each window is labeled by its own duration, and
# an all-zero window is absent rather than a genuine "0%". Mirrors
# codex-rs/tui/src/chatwidget/rate_limits.rs and
# codex-rs/codex-api/src/rate_limits.rs. ----

def test_weekly_only_account_reports_a_single_labeled_window_not_a_fake_second_one():
    # A weekly-only account: primary populated (10080 minutes = 7 days), and
    # secondary entirely absent (used-percent 0, window-minutes 0, no reset-at).
    headers = {
        "x-codex-primary-used-percent": "0",
        "x-codex-primary-window-minutes": "10080",
        "x-codex-primary-reset-at": "1788107507",
        "x-codex-secondary-used-percent": "0",
        "x-codex-secondary-window-minutes": "0",
        "x-codex-secondary-reset-at": "",
    }
    obs = classify(200, headers, NOW)
    assert isinstance(obs, UsageSnapshot)
    assert obs.percent == 0.0
    assert obs.window_label == "weekly"
    assert obs.percent_7d is None  # no fabricated second window
    assert obs.window_label_7d is None


def test_dual_window_account_labels_both_by_real_duration():
    headers = {
        "x-codex-primary-used-percent": "20",
        "x-codex-primary-window-minutes": "300",  # 5h
        "x-codex-secondary-used-percent": "45",
        "x-codex-secondary-window-minutes": "10080",  # weekly
    }
    obs = classify(200, headers, NOW)
    assert obs.window_label == "5h"
    assert obs.percent == 20.0
    assert obs.window_label_7d == "weekly"
    assert obs.percent_7d == 45.0


def test_only_secondary_populated_still_surfaces_as_the_primary_slot():
    # With no primary window to prefer, whichever window exists becomes the
    # displayed number, matching how the Codex client renders them.
    headers = {
        "x-codex-primary-used-percent": "0",
        "x-codex-primary-window-minutes": "0",
        "x-codex-secondary-used-percent": "77",
        "x-codex-secondary-window-minutes": "1440",  # daily
    }
    obs = classify(200, headers, NOW)
    assert obs.percent == 77.0
    assert obs.window_label == "daily"
    assert obs.percent_7d is None


def test_unrecognized_window_duration_gets_no_label_not_a_guess():
    headers = {"x-codex-primary-used-percent": "50", "x-codex-primary-window-minutes": "17"}
    obs = classify(200, headers, NOW)
    assert obs.percent == 50.0
    assert obs.window_label is None  # 17 minutes matches nothing known — never guess


def test_monthly_and_annual_windows_are_recognized():
    monthly = classify(200, {"x-codex-primary-used-percent": "1", "x-codex-primary-window-minutes": "43200"}, NOW)
    assert monthly.window_label == "monthly"
    annual = classify(200, {"x-codex-primary-used-percent": "1", "x-codex-primary-window-minutes": "525600"}, NOW)
    assert annual.window_label == "annual"


def test_window_duration_tolerance_matches_the_real_clients_5_percent_slack():
    # codex-rs's get_limits_duration() allows slack rather than an exact minute
    # count: 10080 * 1.04 is within 5%.
    obs = classify(200, {"x-codex-primary-used-percent": "1", "x-codex-primary-window-minutes": "10483"}, NOW)
    assert obs.window_label == "weekly"
