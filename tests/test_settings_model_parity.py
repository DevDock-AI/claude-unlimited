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


# ---- the current ORDERED-LIST shape (Feature 3) ----

def test_a_valid_list_passes_through_in_order():
    out = _validated_model_parity([
        {"claude_model": "claude-fable-5-1", "model": "gpt-6-astra", "effort": "high", "claude_effort": "xhigh"},
        {"claude_model": "claude-haiku-4-5", "model": "gpt-5.6-luna", "effort": "low"},
    ])
    assert out == [
        {"claude_model": "claude-fable-5-1", "model": "gpt-6-astra", "effort": "high", "claude_effort": "xhigh"},
        {"claude_model": "claude-haiku-4-5", "model": "gpt-5.6-luna", "effort": "low"},
    ]


def test_a_list_row_may_omit_model_and_effort():
    out = _validated_model_parity([{"claude_model": "claude-opus-5"}])
    assert out == [{"claude_model": "claude-opus-5"}]


def test_a_list_row_without_a_claude_model_is_rejected():
    with pytest.raises(ValueError, match="claude_model"):
        _validated_model_parity([{"model": "gpt-6-astra"}])


def test_a_duplicate_claude_model_in_the_list_is_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        _validated_model_parity([
            {"claude_model": "claude-opus-5"},
            {"claude_model": "claude-opus-5-20260101"},  # same base id
        ])


def test_an_invalid_claude_effort_is_rejected():
    with pytest.raises(ValueError, match="claude_effort"):
        _validated_model_parity([{"claude_model": "claude-fable-5-1", "claude_effort": "turbo"}])


def test_too_many_list_rows_are_rejected():
    with pytest.raises(ValueError, match="too many"):
        _validated_model_parity([{"claude_model": f"claude-m-{i}"} for i in range(65)])


def test_an_empty_list_is_accepted_and_means_defaults():
    assert _validated_model_parity([]) == []


def test_import_accepts_the_list_shape_too():
    out = validated_settings_changes({"model_parity": [{"claude_model": "claude-opus-5", "effort": "low"}]})
    assert out["model_parity"] == [{"claude_model": "claude-opus-5", "effort": "low"}]
