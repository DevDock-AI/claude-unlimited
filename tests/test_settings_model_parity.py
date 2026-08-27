"""The parity map is a spending decision: it picks which model every codex
request runs on, and Codex quota is spent on reasoning output weighted by
model tier (docs/adr/0007). It must persist, validate, and not be settable
by an imported bundle without the same checks the API applies.
"""
import pytest

from claude_unlimited.config import _validated_model_parity, validated_settings_changes


def test_a_valid_map_passes_through():
    out = _validated_model_parity({"claude-opus-5": {"model": "gpt-5.6-sol", "effort": "high"}})
    assert out == {"claude-opus-5": {"model": "gpt-5.6-sol", "effort": "high"}}


def test_a_partial_row_is_allowed():
    assert _validated_model_parity({"claude-opus-5": {"effort": "low"}}) == {
        "claude-opus-5": {"effort": "low"}}


def test_an_unknown_effort_is_rejected():
    with pytest.raises(ValueError, match="effort"):
        _validated_model_parity({"claude-opus-5": {"effort": "turbo"}})


def test_a_non_object_row_is_rejected():
    with pytest.raises(ValueError):
        _validated_model_parity({"claude-opus-5": "gpt-5.6-sol"})


def test_an_empty_model_is_rejected():
    with pytest.raises(ValueError, match="model"):
        _validated_model_parity({"claude-opus-5": {"model": "   "}})


def test_an_absurdly_long_model_is_rejected():
    with pytest.raises(ValueError, match="model"):
        _validated_model_parity({"claude-opus-5": {"model": "x" * 500}})


def test_too_many_entries_are_rejected():
    with pytest.raises(ValueError, match="too many"):
        _validated_model_parity({f"m{i}": {"effort": "low"} for i in range(65)})


def test_a_row_with_no_usable_field_is_dropped():
    assert _validated_model_parity({"claude-opus-5": {"nonsense": 1}}) == {}


def test_the_whole_payload_is_rejected_rather_than_half_applied():
    # A half-applied mapping would be worse than one that refused.
    with pytest.raises(ValueError):
        _validated_model_parity({
            "claude-opus-5": {"effort": "high"},
            "claude-sonnet-5": {"effort": "not-a-level"},
        })


def test_import_uses_the_same_validator_as_the_api():
    # A bundle is a file from somewhere else; it must not be able to set
    # something PATCH /api/settings would have refused.
    with pytest.raises(ValueError):
        validated_settings_changes({"model_parity": {"claude-opus-5": {"effort": "bogus"}}})
