from claude_unlimited.openai_models import (
    _MODEL_LADDER,
    _MODEL_MAP,
    OpenAIModelTarget,
    automatic_mapping,
    advertised_models,
    fallback_models,
    map_model,
)


def test_known_claude_models_map_to_their_confirmed_parity_target():
    # The expensive model is reserved for the top tier on purpose: quota is
    # spent on reasoning output weighted by model tier (docs/adr/0007), so the
    # default session must not land on it.
    assert map_model("claude-fable-5") == OpenAIModelTarget("gpt-5.6-sol", "high")
    assert map_model("claude-opus-5") == OpenAIModelTarget("gpt-5.6-terra", "high")
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
    assert map_model("claude-opus-4-1-20260101") == OpenAIModelTarget("gpt-5.6-terra", "high")
    assert map_model("claude-fable-legacy") == OpenAIModelTarget("gpt-5.6-sol", "high")
    assert map_model("claude-haiku-legacy") == OpenAIModelTarget("gpt-5.6-luna", "low")


def test_per_profile_model_override_wins_outright():
    target = map_model("claude-haiku-4-5-20251001", override_model="gpt-5.2")
    assert target.model == "gpt-5.2"
    # Effort falls back to the effort for THIS model ("low" for Haiku), not a
    # global default. It used to take the global "medium", which quietly ran
    # Haiku requests at a higher effort than their tier — and effort is what
    # Codex quota is spent on (docs/adr/0007).
    assert target.reasoning_effort == "low"


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


def test_automatic_mapping_matches_the_real_map():
    rows = automatic_mapping()
    assert [r["claude_model"] for r in rows] == list(_MODEL_MAP)
    for row in rows:
        target = _MODEL_MAP[row["claude_model"]]
        assert row["openai_model"] == target.model
        assert row["reasoning_effort"] == target.reasoning_effort
        assert row["claude_label"]  # never blank, or the table renders empty cells


def test_the_dashboard_does_not_restate_the_mapping():
    # This table was hardcoded in index.html, in two places, and both silently
    # went stale the first time the mapping changed. It is served from
    # /api/codex/model-map now; a literal model id back in the page means the
    # duplicate has returned.
    from pathlib import Path
    static = Path(__file__).resolve().parent.parent / "claude_unlimited" / "static"
    # app.js as well as index.html: the option lists there hardcoded the same
    # ids, which is the same drift bug one file over.
    for filename in ("index.html", "app.js"):
        page = (static / filename).read_text(encoding="utf-8")
        for target in {t.model for t in _MODEL_MAP.values()}:
            assert target not in page, f"{target} is hardcoded in {filename} again"


def test_parity_override_applies_to_the_mapping():
    parity = {"claude-opus-5": {"model": "gpt-5.6-sol", "effort": "max"}}
    assert map_model("claude-opus-5", parity=parity) == OpenAIModelTarget("gpt-5.6-sol", "max")


def test_a_parity_row_may_override_only_one_field():
    parity = {"claude-opus-5": {"effort": "low"}}
    t = map_model("claude-opus-5", parity=parity)
    assert t.model == _MODEL_MAP["claude-opus-5"].model  # shipped default kept
    assert t.reasoning_effort == "low"


def test_a_profile_override_still_beats_the_parity_map():
    # Narrower wins: the Profile setting is more specific than a global table.
    parity = {"claude-opus-5": {"model": "gpt-5.6-sol"}}
    t = map_model("claude-opus-5", override_model="gpt-5.6-luna", parity=parity)
    assert t.model == "gpt-5.6-luna"


def test_parity_reaches_a_dated_model_id_through_the_family_fallback():
    parity = {"claude-opus-5": {"effort": "minimal"}}
    assert map_model("claude-opus-4-1-20260101", parity=parity).reasoning_effort == "minimal"


def test_untouched_rows_follow_the_shipped_defaults():
    parity = {"claude-opus-5": {"effort": "low"}}
    assert map_model("claude-sonnet-5", parity=parity) == _MODEL_MAP["claude-sonnet-5"]


def test_advertised_models_reflect_parity():
    # The /model picker must not advertise one thing while the bridge runs
    # another — that is the drift class this work exists to remove.
    parity = {"claude-opus-5": {"model": "gpt-5.6-sol", "effort": "max"}}
    labels = dict(advertised_models(parity))
    assert "Sol" in labels["claude-opus-5"] and "max" in labels["claude-opus-5"]


def test_automatic_mapping_marks_overridden_rows():
    rows = {r["claude_model"]: r for r in automatic_mapping({"claude-opus-5": {"effort": "max"}})}
    assert rows["claude-opus-5"]["overridden"] is True
    assert rows["claude-opus-5"]["reasoning_effort"] == "max"
    assert rows["claude-opus-5"]["default_effort"] == _MODEL_MAP["claude-opus-5"].reasoning_effort
    assert rows["claude-sonnet-5"]["overridden"] is False
