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
    # No tier maps to the flagship by default: quota is spent on reasoning
    # output weighted by model tier (docs/adr/0007), and Claude Code chooses
    # Fable on its own, so a user who never picked the expensive model must
    # not end up paying for it.
    assert map_model("claude-fable-5") == OpenAIModelTarget("gpt-5.6-sol", "medium")
    assert map_model("claude-opus-5") == OpenAIModelTarget("gpt-5.6-terra", "high")
    assert map_model("claude-sonnet-5") == OpenAIModelTarget("gpt-5.6-terra", "medium")
    assert map_model("claude-haiku-4-5-20251001") == OpenAIModelTarget("gpt-5.6-luna", "low")


def test_no_tier_reaches_the_expensive_model_by_default():
    # It stays selectable in the parity table; it is simply never the shipped
    # target for any tier.
    expensive = "gpt-6-astra"
    assert [c for c, t in _MODEL_MAP.items() if t.model == expensive] == []
    assert expensive in _MODEL_LADDER


def test_unknown_model_falls_back_to_the_balanced_default():
    assert map_model("some-future-claude-model") == OpenAIModelTarget("gpt-5.6-terra", "medium")
    assert map_model(None) == OpenAIModelTarget("gpt-5.6-terra", "medium")


def test_family_prefix_fallback_for_an_unrecognized_but_familiar_id():
    # A dated id the table has no exact entry for, but which still names a
    # recognizable tier by substring.
    assert map_model("claude-opus-4-1-20260101") == OpenAIModelTarget("gpt-5.6-terra", "high")
    assert map_model("claude-fable-legacy") == OpenAIModelTarget("gpt-5.6-sol", "medium")
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
    target = map_model("claude-fable-5", override_reasoning_effort="xhigh")
    assert target.model == "gpt-5.6-sol"
    assert target.reasoning_effort == "xhigh"


def test_both_overrides_together():
    target = map_model("claude-sonnet-5", override_model="gpt-5.6-luna", override_reasoning_effort="max")
    assert target == OpenAIModelTarget("gpt-5.6-luna", "max")


def test_fallbacks_start_below_the_rejected_model():
    # A model rejected as too capable (or withdrawn from a plan) should not
    # retry something more capable first — it walks down, then wraps to the
    # rungs above (so a retired mid-tier can still reach a working one).
    assert fallback_models("gpt-5.6-sol") == ["gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-astra"]


def test_fallbacks_wrap_around_for_a_mid_tier_model():
    assert fallback_models("gpt-5.6-terra") == ["gpt-5.6-luna", "gpt-6-astra", "gpt-5.6-sol"]


def test_an_unknown_model_falls_back_to_the_whole_ladder():
    assert fallback_models("gpt-4o-legacy") == ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]


def test_every_model_in_the_map_can_reach_every_other_one():
    # The pool survives any single model being retired only if the ladder is
    # exhaustive from wherever it starts.
    for _, target in _MODEL_MAP.items():
        reachable = {target.model, *fallback_models(target.model)}
        assert reachable == set(_MODEL_LADDER)


def _one_per_family(ids):
    # The default list starts with one row per family — the newest the map
    # lists — even when the map holds two of a family (Opus 5.5 and Opus 5).
    seen, out = set(), []
    for claude_id in ids:
        family = "-".join(claude_id.split("-")[:2])
        if family not in seen:
            seen.add(family)
            out.append(claude_id)
    return out


def test_automatic_mapping_matches_the_real_map():
    rows = automatic_mapping()
    assert [r["claude_model"] for r in rows] == _one_per_family(_MODEL_MAP)
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
    parity = {"claude-opus-5-5": {"effort": "none"}}   # the family's default row
    assert map_model("claude-opus-4-1-20260101", parity=parity).reasoning_effort == "none"


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


def test_the_effort_list_matches_what_the_backend_accepts():
    """Verified live (2026-09-17) against gpt-5.6-terra and gpt-5.6-sol:
    "Supported values are: 'none', 'low', 'medium', 'high', 'xhigh', and
    'max'." `none` returns 0 reasoning tokens — the thing a Codex subscription
    is charged for (ADR 0007). `minimal` is rejected outright, so offering it
    only ever produced a failed request."""
    from claude_unlimited.openai_models import VALID_REASONING_EFFORTS
    assert "none" in VALID_REASONING_EFFORTS
    assert "minimal" not in VALID_REASONING_EFFORTS
    assert set(VALID_REASONING_EFFORTS) >= {"none", "low", "medium", "high", "xhigh", "max"}


def test_none_reaches_the_request_body_as_the_effort():
    import json
    from claude_unlimited.openai_models import map_model
    from claude_unlimited.openai_translate import anthropic_request_to_openai
    target = map_model("claude-opus-5", override_reasoning_effort="none")
    assert target.reasoning_effort == "none"
    body = anthropic_request_to_openai({"messages": [], "model": "claude-opus-5"}, target)
    assert body["reasoning"] == {"effort": "none"}


def test_a_profile_can_be_saved_with_reasoning_off():
    from claude_unlimited.profiles import _validate_field_types
    _validate_field_types(codex_reasoning_effort="none")
