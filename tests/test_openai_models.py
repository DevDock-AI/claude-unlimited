from claude_unlimited.openai_models import (
    _MODEL_LADDER,
    _MODEL_MAP,
    OpenAIModelTarget,
    fallback_models,
    map_model,
)


def test_known_claude_models_map_to_their_confirmed_parity_target():
    # The expensive model is reserved for the top tier on purpose: quota is
    # spent on reasoning output weighted by model tier (docs/adr/0007), so the
    # default session must not land on it.
    assert map_model("claude-fable-5") == OpenAIModelTarget("gpt-5.6-sol", "high")
    assert map_model("claude-opus-5") == OpenAIModelTarget("gpt-5.6-terra", "medium")
    assert map_model("claude-sonnet-5") == OpenAIModelTarget("gpt-5.6-terra", "medium")
    assert map_model("claude-haiku-4-5-20251001") == OpenAIModelTarget("gpt-5.6-luna", "low")


def test_only_the_top_tier_reaches_the_expensive_model():
    expensive = "gpt-5.6-sol"
    on_expensive = [c for c, t in _MODEL_MAP.items() if t.model == expensive]
    assert on_expensive == ["claude-fable-5"]


def test_unknown_model_falls_back_to_the_balanced_default():
    assert map_model("some-future-claude-model") == OpenAIModelTarget("gpt-5.6-terra", "medium")
    assert map_model(None) == OpenAIModelTarget("gpt-5.6-terra", "medium")


def test_family_prefix_fallback_for_an_unrecognized_but_familiar_id():
    # A dated id the table has no exact entry for, but which still names a
    # recognizable tier by substring.
    assert map_model("claude-opus-4-1-20260101") == OpenAIModelTarget("gpt-5.6-terra", "medium")
    assert map_model("claude-fable-legacy") == OpenAIModelTarget("gpt-5.6-sol", "high")
    assert map_model("claude-haiku-legacy") == OpenAIModelTarget("gpt-5.6-luna", "low")


def test_per_profile_model_override_wins_outright():
    target = map_model("claude-haiku-4-5-20251001", override_model="gpt-5.2")
    assert target.model == "gpt-5.2"
    # Reasoning effort keeps the mapping's own default when only the model is overridden.
    assert target.reasoning_effort == "medium"


def test_per_profile_reasoning_effort_override_alone_keeps_the_mapped_model():
    target = map_model("claude-fable-5", override_reasoning_effort="ultra")
    assert target.model == "gpt-5.6-sol"
    assert target.reasoning_effort == "ultra"


def test_both_overrides_together():
    target = map_model("claude-sonnet-5", override_model="gpt-5.6-luna", override_reasoning_effort="max")
    assert target == OpenAIModelTarget("gpt-5.6-luna", "max")


def test_fallbacks_start_below_the_rejected_model():
    # A model rejected as too capable (or withdrawn from a plan) should not
    # retry something more capable first.
    assert fallback_models("gpt-5.6-sol") == ["gpt-5.6-terra", "gpt-5.6-luna"]


def test_fallbacks_wrap_around_for_a_mid_tier_model():
    assert fallback_models("gpt-5.6-terra") == ["gpt-5.6-luna", "gpt-5.6-sol"]


def test_an_unknown_model_falls_back_to_the_whole_ladder():
    assert fallback_models("gpt-4o-legacy") == ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]


def test_every_model_in_the_map_can_reach_every_other_one():
    # The pool survives any single model being retired only if the ladder is
    # exhaustive from wherever it starts.
    for _, target in _MODEL_MAP.items():
        reachable = {target.model, *fallback_models(target.model)}
        assert reachable == set(_MODEL_LADDER)
