import pytest

import claude_unlimited.pricing as pricing


def test_find_price_matches_dated_snapshot_model_id():
    price = pricing.find_price("claude-haiku-4-5-20251001")
    assert price is not None
    assert price.prefix == "claude-haiku-4-5"


def test_find_price_matches_bare_model_id():
    price = pricing.find_price("claude-sonnet-5")
    assert price is not None
    assert price.prefix == "claude-sonnet-5"


def test_find_price_prefers_longest_matching_prefix():
    # "claude-opus-4-8..." must not match the shorter "claude-opus-4" entry.
    price = pricing.find_price("claude-opus-4-8-20260101")
    assert price.prefix == "claude-opus-4-8"
    assert price.input_per_mtok == 5


def test_find_price_unknown_model_returns_none():
    assert pricing.find_price("some-future-model-nobody-has-seen") is None


def test_find_price_none_input_returns_none():
    assert pricing.find_price(None) is None
    assert pricing.find_price("") is None


def test_estimate_cost_matches_worked_example_from_docs():
    # Anthropic's own worked example: Opus 5, 50,000 input + 15,000 output -> $0.625
    cost = pricing.estimate_cost_usd("claude-opus-5", {"input_tokens": 50_000, "output_tokens": 15_000})
    assert cost == pytest.approx(0.25 + 0.375, abs=1e-6)


def test_estimate_cost_matches_worked_example_with_cache_reads():
    # Same doc's second example: 10,000 uncached + 40,000 cache-read input, 15,000 output -> $0.445 (token-only, no session runtime)
    cost = pricing.estimate_cost_usd("claude-opus-5", {
        "input_tokens": 10_000, "output_tokens": 15_000, "cache_read_input_tokens": 40_000,
    })
    assert cost == pytest.approx(0.05 + 0.02 + 0.375, abs=1e-6)


def test_estimate_cost_includes_cache_write_tokens():
    cost = pricing.estimate_cost_usd("claude-sonnet-5", {
        "input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1_000_000,
    })
    assert cost == pytest.approx(2.50, abs=1e-6)


def test_estimate_cost_unknown_model_returns_none():
    assert pricing.estimate_cost_usd("totally-unknown-model", {"input_tokens": 100, "output_tokens": 100}) is None


def test_estimate_cost_no_usage_returns_none():
    assert pricing.estimate_cost_usd("claude-sonnet-5", None) is None
    assert pricing.estimate_cost_usd("claude-sonnet-5", {}) is None
