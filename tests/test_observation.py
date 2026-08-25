from datetime import datetime, timezone

from claude_unlimited.observation import (
    AuthInvalid,
    ProviderUnavailable,
    QuotaExhausted,
    ShortRateLimit,
    Unknown,
    UsageSnapshot,
    classify,
)

NOW = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)


def test_success_with_usage_headers_yields_usage_snapshot():
    # The header shape Anthropic sends: a 0-1 utilization float and a Unix
    # epoch reset, not remaining/limit.
    headers = {
        "anthropic-ratelimit-unified-5h-utilization": "0.61",
        "anthropic-ratelimit-unified-5h-reset": "1787191800",
    }
    obs = classify(200, headers, NOW)
    assert isinstance(obs, UsageSnapshot)
    assert obs.percent == 61.0
    assert obs.confidence == "measured"
    assert obs.resets_at is not None
    assert obs.resets_at.year == 2026


def test_reset_header_parsed_as_unix_epoch_not_iso8601():
    headers = {"anthropic-ratelimit-unified-5h-utilization": "0.5",
               "anthropic-ratelimit-unified-5h-reset": "1787191800"}
    obs = classify(200, headers, NOW)
    assert obs.resets_at == datetime.fromtimestamp(1787191800, tz=timezone.utc)


def test_reset_header_falls_back_to_iso8601_if_ever_sent_that_way():
    headers = {"anthropic-ratelimit-unified-5h-utilization": "0.5",
               "anthropic-ratelimit-unified-5h-reset": "2026-08-20T04:00:00Z"}
    obs = classify(200, headers, NOW)
    assert obs.resets_at == datetime(2026, 8, 20, 4, 0, tzinfo=timezone.utc)


def test_success_without_usage_headers_yields_unknown():
    obs = classify(200, {}, NOW)
    assert isinstance(obs, Unknown)
    assert obs.status_code == 200


def test_429_with_rejected_status_is_quota_exhausted_not_rate_limit():
    headers = {"anthropic-ratelimit-unified-5h-status": "rejected"}
    obs = classify(429, headers, NOW)
    assert isinstance(obs, QuotaExhausted)


def test_429_without_rejected_status_is_short_rate_limit():
    headers = {"retry-after": "12"}
    obs = classify(429, headers, NOW)
    assert isinstance(obs, ShortRateLimit)
    assert obs.retry_after_seconds == 12.0


def test_429_7d_rejected_with_5h_allowed_is_still_quota_exhausted():
    # A Profile can have 5h headroom left while its weekly cap is spent.
    # `status_5h or status_7d` would pick the truthy "allowed" and never look
    # at 7d, misclassifying this as a rate-limit blip the Router would
    # cooldown-and-retry forever instead of rotating away from.
    headers = {
        "anthropic-ratelimit-unified-5h-status": "allowed",
        "anthropic-ratelimit-unified-7d-status": "rejected",
        "anthropic-ratelimit-unified-7d-reset": "1787191800",
    }
    obs = classify(429, headers, NOW)
    assert isinstance(obs, QuotaExhausted)
    # The 7d window's own reset time, not the (here-absent) 5h one.
    assert obs.resets_at == datetime.fromtimestamp(1787191800, tz=timezone.utc)


def test_429_5h_rejected_uses_5h_reset_not_7d():
    headers = {
        "anthropic-ratelimit-unified-5h-status": "rejected",
        "anthropic-ratelimit-unified-5h-reset": "1787191800",
        "anthropic-ratelimit-unified-7d-status": "allowed",
        "anthropic-ratelimit-unified-7d-reset": "1787999999",
    }
    obs = classify(429, headers, NOW)
    assert isinstance(obs, QuotaExhausted)
    assert obs.resets_at == datetime.fromtimestamp(1787191800, tz=timezone.utc)


def test_529_is_provider_unavailable_not_quota_exhausted():
    obs = classify(529, {"retry-after": "5"}, NOW)
    assert isinstance(obs, ProviderUnavailable)


def test_401_is_auth_invalid():
    assert isinstance(classify(401, {}, NOW), AuthInvalid)


def test_403_is_not_auth_invalid():
    # 403 (permission_error) means the credential is valid but not scoped for
    # this request, most commonly a model the key cannot access. Treating it
    # like a 401 would mark a healthy Profile "needs re-authentication" just
    # because `/model` picked an unavailable model.
    obs = classify(403, {}, NOW)
    assert not isinstance(obs, AuthInvalid)
    assert isinstance(obs, Unknown)
    assert obs.status_code == 403


def test_unrecognized_status_is_unknown_not_a_crash():
    obs = classify(418, {}, NOW)
    assert isinstance(obs, Unknown)
    assert obs.status_code == 418


def test_malformed_header_values_degrade_to_unknown_not_a_crash():
    headers = {"anthropic-ratelimit-unified-5h-utilization": "not-a-number"}
    obs = classify(200, headers, NOW)
    assert isinstance(obs, Unknown)
