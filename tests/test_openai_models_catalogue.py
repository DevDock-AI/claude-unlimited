"""openai_models deriving its lineups from an injected catalogue.

The literals in openai_models.py are the fallback AND the curated spending
decisions; the catalogue supplies the row set, order and names. These tests
inject a Catalogue built from a fixture dict — never module state, never the
network — so they hold no matter what the daemon has initialized.
"""

import datetime

import claude_unlimited.model_catalogue as mc
from claude_unlimited.openai_models import (
    _DEFAULT_TARGET,
    _LEGACY_SELECTABLE,
    _MODEL_LADDER,
    _MODEL_MAP,
    OpenAIModelTarget,
    advertised_models,
    automatic_mapping,
    effective_model_map,
    fallback_models,
    map_model,
    selectable_models,
)

TODAY = datetime.date(2026, 9, 7)


def entry(provider, out_cost, mode="chat"):
    return {"litellm_provider": provider, "mode": mode,
            "max_input_tokens": 200000, "input_cost_per_token": out_cost / 5,
            "output_cost_per_token": out_cost, "supports_reasoning": True}


def make_catalogue(extra=None):
    raw = {
        # A brand-new Claude model ABOVE the top curated one.
        "claude-zenith-6": entry("anthropic", 9e-05),
        "claude-fable-5": entry("anthropic", 5e-05),
        "claude-opus-5": entry("anthropic", 2.5e-05),
        # A new model slotting between Opus and Sonnet.
        "claude-nova-4": entry("anthropic", 1.8e-05),
        "claude-sonnet-5": entry("anthropic", 1e-05),
        # The catalogue spells Haiku undated; _MODEL_MAP keys the dated id.
        "claude-haiku-4-5": entry("anthropic", 5e-06),
        "gpt-5.6-sol": entry("openai", 2e-05),
        "gpt-5.6-terra": entry("openai", 1.2e-05),
        "gpt-5.6-luna": entry("openai", 1.2e-06),
        "gpt-5.9-new": entry("openai", 3e-05),
    }
    raw.update(extra or {})
    return mc.parse(raw, today=TODAY)


def test_effective_map_keeps_every_curated_decision_verbatim():
    emap = effective_model_map(make_catalogue())
    # A curated model keeps its EXACT target and effort — the mapping is a
    # documented spending decision a catalogue refresh must never change.
    assert emap["claude-fable-5"] == _MODEL_MAP["claude-fable-5"]
    assert emap["claude-opus-5"] == _MODEL_MAP["claude-opus-5"]
    assert emap["claude-sonnet-5"] == _MODEL_MAP["claude-sonnet-5"]
    # Undated catalogue spelling inherits the dated curated row.
    assert emap["claude-haiku-4-5"] == _MODEL_MAP["claude-haiku-4-5-20251001"]


def test_a_new_top_claude_model_gets_the_top_openai_tier():
    emap = effective_model_map(make_catalogue())
    assert emap["claude-zenith-6"] == OpenAIModelTarget("gpt-5.6-sol", "high")


def test_a_new_mid_tier_model_takes_its_conservative_neighbours_tier():
    # Between Opus (terra/high) and Sonnet (terra/medium): the cheaper
    # neighbour wins, because raising a row raises what a session costs.
    emap = effective_model_map(make_catalogue())
    assert emap["claude-nova-4"] == OpenAIModelTarget("gpt-5.6-terra", "medium")


def test_the_tier_system_does_not_collapse_onto_one_model():
    emap = effective_model_map(make_catalogue())
    assert len({t.model for t in emap.values()}) >= 3
    assert len({t for t in emap.values()}) >= 4


def test_without_a_catalogue_everything_falls_back_to_the_literals():
    # No initialize() ran in this process, so current() is None — every
    # derived surface must equal the shipped literals exactly.
    assert effective_model_map(None) == _MODEL_MAP
    assert selectable_models() == (list(_MODEL_LADDER) + list(_LEGACY_SELECTABLE))
    assert [r["claude_model"] for r in automatic_mapping()] == list(_MODEL_MAP)
    assert dict(advertised_models()).keys() == _MODEL_MAP.keys()
    assert fallback_models("gpt-5.6-sol") == ["gpt-5.6-terra", "gpt-5.6-luna"]


def test_selectable_models_include_the_catalogue_openai_lineup():
    cat = make_catalogue()
    models = selectable_models(cat)
    assert models[:3] == ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]  # ladder first
    assert "gpt-5.9-new" in models  # a model this build never heard of
    for legacy in _LEGACY_SELECTABLE:
        assert legacy in models


def test_advertised_models_follow_the_catalogue_lineup():
    cat = make_catalogue()
    ads = advertised_models(catalogue=cat)
    ids = [claude_id for claude_id, _ in ads]
    assert ids[0] == "claude-zenith-6"  # most capable first, catalogue order
    assert "claude-haiku-4-5" in ids
    labels = dict(ads)
    # `<anthropic model> | <openai model> · <effort>` — the /model picker names
    # both the Claude tier and the OpenAI/Codex model it routes to.
    assert labels["claude-zenith-6"] == "Claude Zenith 6 | GPT-5.6 Sol · high"


def test_automatic_mapping_rows_come_from_the_catalogue_with_clean_labels():
    rows = {r["claude_model"]: r for r in automatic_mapping(catalogue=make_catalogue())}
    assert rows["claude-zenith-6"]["claude_label"] == "Claude Zenith 6"
    assert rows["claude-fable-5"]["claude_label"] == "Claude Fable 5"  # curated name kept
    assert rows["claude-nova-4"]["openai_model"] == "gpt-5.6-terra"
    assert all(r["claude_label"] for r in rows.values())


def test_map_model_resolves_catalogue_only_models():
    cat = make_catalogue()
    assert map_model("claude-zenith-6", catalogue=cat) == OpenAIModelTarget("gpt-5.6-sol", "high")
    # A dated spelling of a catalogue row lands on the same row.
    assert map_model("claude-nova-4-20260901", catalogue=cat) == OpenAIModelTarget("gpt-5.6-terra", "medium")


def test_parity_overlay_still_wins_over_the_catalogue_mapping():
    cat = make_catalogue()
    parity = {"claude-zenith-6": {"model": "gpt-5.6-luna", "effort": "low"}}
    assert map_model("claude-zenith-6", parity=parity, catalogue=cat) == \
        OpenAIModelTarget("gpt-5.6-luna", "low")
    rows = {r["claude_model"]: r for r in automatic_mapping(parity, catalogue=cat)}
    assert rows["claude-zenith-6"]["overridden"] is True
    assert rows["claude-zenith-6"]["openai_model"] == "gpt-5.6-luna"
    # Per-Profile override remains narrower and still beats the parity map.
    assert map_model("claude-zenith-6", override_model="gpt-5.2",
                     parity=parity, catalogue=cat).model == "gpt-5.2"


def test_an_unknown_model_still_resolves_safely_with_a_catalogue():
    cat = make_catalogue()
    assert map_model("mystery-model-9000", catalogue=cat) == _DEFAULT_TARGET
    assert map_model(None, catalogue=cat) == _DEFAULT_TARGET
    # Family fallback still works for a familiar family the catalogue lacks.
    assert map_model("claude-haiku-legacy", catalogue=cat) == OpenAIModelTarget("gpt-5.6-luna", "low")


def test_fallback_ladder_derives_from_the_catalogue_and_stays_exhaustive():
    cat = make_catalogue()
    ladder_walk = {"gpt-5.6-sol", *fallback_models("gpt-5.6-sol", cat)}
    for target in effective_model_map(cat).values():
        assert target.model in ladder_walk


def test_the_daemon_surfaces_pick_up_an_initialized_catalogue(monkeypatch):
    # /api/codex/model-map calls automatic_mapping()/selectable_models() and
    # the codex /v1/models listing goes connectors.models_listing ->
    # advertised_models(), all WITHOUT a catalogue argument — so the wiring
    # they rely on is the default falling through to model_catalogue.current().
    cat = make_catalogue()
    monkeypatch.setattr(mc, "_current", cat)
    try:
        from claude_unlimited import connectors
        listing = connectors.models_listing("codex")
        assert [mid for mid, _ in listing][0] == "claude-zenith-6"
        assert "gpt-5.9-new" in selectable_models()
        assert any(r["claude_model"] == "claude-nova-4" for r in automatic_mapping())
    finally:
        monkeypatch.undo()


def test_a_fully_renamed_lineup_spreads_across_the_curated_tiers():
    # No curated model recognizable at all: rank-proportional assignment,
    # never a collapse onto one target. Built directly because parse()'s
    # anchor validation would (rightly) refuse such a lineup from a fetch.
    def info(mid, rank):
        return mc.ModelInfo(id=mid, display_name=mid, supports_reasoning=True,
                            input_cost=1e-06, output_cost=1e-05, rank=rank)
    cat = mc.Catalogue(
        anthropic=tuple(info(f"claude-alien-{i}", i) for i in range(4)),
        openai=(info("gpt-5.6-sol", 0),))
    emap = effective_model_map(cat)
    assert emap["claude-alien-0"] == list(_MODEL_MAP.values())[0]
    assert emap["claude-alien-3"] == list(_MODEL_MAP.values())[-1]
    assert len({t for t in emap.values()}) >= 3
