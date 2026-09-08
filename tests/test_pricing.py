import datetime
import json

import pytest

import claude_unlimited.model_catalogue as mc
import claude_unlimited.pricing as pricing

TODAY = datetime.date(2026, 9, 7)


@pytest.fixture(scope="module")
def vendored_catalogue():
    """The shipped offline snapshot, parsed exactly as initialize() would —
    read from disk, never the network."""
    raw = json.loads(mc._VENDORED_FILE.read_text(encoding="utf-8"))
    return mc.parse(raw, today=TODAY)


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


# ---- catalogue-sourced pricing (ticket 007 stage 2) ----


@pytest.mark.parametrize("literal", pricing.MODEL_PRICES, ids=lambda p: p.prefix)
def test_every_previously_priced_model_still_resolves(literal, vendored_catalogue):
    """Migration safety: every id the literal table priced keeps resolving
    with the catalogue active — through the catalogue when it carries the
    family, through the literal fallback when it doesn't (retired families
    past their deprecation date are dropped by the catalogue parser but may
    still appear in a long-lived local usage history)."""
    for model_id in (literal.prefix, literal.prefix + "-20260101"):
        price = pricing.find_price(model_id, catalogue=vendored_catalogue)
        assert price is not None, model_id
        assert model_id.startswith(price.prefix)


def test_dated_id_resolves_via_the_catalogue_with_catalogue_rates(vendored_catalogue):
    price = pricing.find_price("claude-opus-4-5-20251101", catalogue=vendored_catalogue)
    assert price.prefix == "claude-opus-4-5"
    # The vendored snapshot's LiteLLM rates (per-token * 1e6), which for
    # this family agree with the literal table.
    assert price.input_per_mtok == pytest.approx(5)
    assert price.output_per_mtok == pytest.approx(25)
    assert price.cache_write_5m_per_mtok == pytest.approx(6.25)
    assert price.cache_write_1h_per_mtok == pytest.approx(10)
    assert price.cache_read_per_mtok == pytest.approx(0.50)


def test_catalogue_prefers_the_longest_prefix_too():
    cat = mc.parse({
        "claude-opus-4": {"litellm_provider": "anthropic", "mode": "chat",
                          "input_cost_per_token": 1.5e-05, "output_cost_per_token": 7.5e-05},
        "claude-opus-4-5": {"litellm_provider": "anthropic", "mode": "chat",
                            "input_cost_per_token": 5e-06, "output_cost_per_token": 2.5e-05},
        "gpt-5.6-terra": {"litellm_provider": "openai", "mode": "chat",
                          "input_cost_per_token": 2e-06, "output_cost_per_token": 1.2e-05},
    }, today=TODAY)
    price = pricing.find_price("claude-opus-4-5-20251101", catalogue=cat)
    assert price.prefix == "claude-opus-4-5"
    assert price.input_per_mtok == pytest.approx(5)
    # Missing cache rates derive from the input rate by Anthropic's uniform
    # multipliers — never a guessed flat number.
    assert price.cache_write_5m_per_mtok == pytest.approx(5 * 1.25)
    assert price.cache_write_1h_per_mtok == pytest.approx(5 * 2)
    assert price.cache_read_per_mtok == pytest.approx(5 * 0.10)


def test_no_catalogue_falls_back_to_the_literal_table():
    price = pricing.find_price("claude-opus-4-8-20260101", catalogue=None)
    assert price is not None
    assert price.prefix == "claude-opus-4-8"
    assert price.input_per_mtok == 5 and price.output_per_mtok == 25


def test_unknown_model_returns_none_with_and_without_a_catalogue(vendored_catalogue):
    assert pricing.find_price("some-future-model-nobody-has-seen", catalogue=vendored_catalogue) is None
    assert pricing.find_price("some-future-model-nobody-has-seen", catalogue=None) is None


def test_estimate_cost_uses_catalogue_rates_when_the_catalogue_is_live(monkeypatch):
    cat = mc.parse({
        "claude-sonnet-5": {"litellm_provider": "anthropic", "mode": "chat",
                            "input_cost_per_token": 4e-06, "output_cost_per_token": 2e-05},
        "gpt-5.6-terra": {"litellm_provider": "openai", "mode": "chat",
                          "input_cost_per_token": 2e-06, "output_cost_per_token": 1.2e-05},
    }, today=TODAY)
    monkeypatch.setattr(mc, "_current", cat)
    cost = pricing.estimate_cost_usd("claude-sonnet-5", {"input_tokens": 1_000_000})
    assert cost == pytest.approx(4.0)  # the catalogue's rate, not the literal 2
