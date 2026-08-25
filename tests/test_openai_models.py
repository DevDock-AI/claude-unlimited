from claude_unlimited.openai_models import OpenAIModelTarget, map_model


def test_known_claude_models_map_to_their_confirmed_parity_target():
    assert map_model("claude-fable-5") == OpenAIModelTarget("gpt-5.6-sol", "max")
    assert map_model("claude-opus-5") == OpenAIModelTarget("gpt-5.6-sol", "high")
    assert map_model("claude-sonnet-5") == OpenAIModelTarget("gpt-5.6-terra", "medium")
    assert map_model("claude-haiku-4-5-20251001") == OpenAIModelTarget("gpt-5.6-luna", "low")


def test_unknown_model_falls_back_to_the_balanced_default():
    assert map_model("some-future-claude-model") == OpenAIModelTarget("gpt-5.6-terra", "medium")
    assert map_model(None) == OpenAIModelTarget("gpt-5.6-terra", "medium")


def test_family_prefix_fallback_for_an_unrecognized_but_familiar_id():
    # A dated id the table has no exact entry for, but which still names a
    # recognizable tier by substring.
    assert map_model("claude-opus-4-1-20260101") == OpenAIModelTarget("gpt-5.6-sol", "high")
    assert map_model("claude-haiku-legacy") == OpenAIModelTarget("gpt-5.6-luna", "low")


def test_per_profile_model_override_wins_outright():
    target = map_model("claude-haiku-4-5-20251001", override_model="gpt-5.2")
    assert target.model == "gpt-5.2"
    # Reasoning effort keeps the mapping's own default when only the model is overridden.
    assert target.reasoning_effort == "medium"


def test_per_profile_reasoning_effort_override_alone_keeps_the_mapped_model():
    target = map_model("claude-opus-5", override_reasoning_effort="ultra")
    assert target.model == "gpt-5.6-sol"
    assert target.reasoning_effort == "ultra"


def test_both_overrides_together():
    target = map_model("claude-sonnet-5", override_model="gpt-5.6-luna", override_reasoning_effort="max")
    assert target == OpenAIModelTarget("gpt-5.6-luna", "max")
