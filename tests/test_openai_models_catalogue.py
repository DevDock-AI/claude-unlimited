"""openai_models deriving its lineups from an injected catalogue.

The literals in openai_models.py are the fallback AND the curated spending
decisions; the catalogue supplies the row set, order and names. These tests
inject a Catalogue built from a fixture dict — never module state, never the
network — so they hold no matter what the daemon has initialized.
"""

import datetime

import claude_unlimited.model_catalogue as mc
from claude_unlimited.openai_models import (
    unmapped_claude_models,
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
    selectable_claude_models,
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
    # "Top tier" means the strongest CURATED target, not the most expensive
    # model in existence: nothing maps to the flagship by default, so a brand
    # new top Claude model must not invent that spending level either.
    emap = effective_model_map(make_catalogue())
    assert emap["claude-zenith-6"] == OpenAIModelTarget("gpt-5.6-sol", "medium")
    assert "gpt-6-astra" not in {t.model for t in emap.values()}


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
    heads, seen = [], set()
    for claude_id in _MODEL_MAP:          # one default row per family, newest first
        family = "-".join(claude_id.split("-")[:2])
        if family not in seen:
            seen.add(family)
            heads.append(claude_id)
    assert [r["claude_model"] for r in automatic_mapping()] == heads
    assert list(dict(advertised_models()).keys()) == heads   # the picker gets no duplicate Opus
    assert fallback_models("gpt-5.6-sol") == ["gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-astra"]


def test_selectable_models_are_ordered_most_expensive_first():
    cat = make_catalogue()
    models = selectable_models(cat)
    # Catalogue rank first: generation desc (gpt-5.9 before the 5.6s), then
    # cost desc within a generation (sol > terra > luna).
    assert models[:4] == ["gpt-5.9-new", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
    for legacy in _LEGACY_SELECTABLE:
        assert legacy in models


def test_selectable_claude_models_follow_the_catalogue_rank():
    cat = make_catalogue()
    claude = selectable_claude_models(cat)
    # Anthropic rank is cost desc: zenith (9e-05) leads, haiku (5e-06) trails.
    assert [c["id"] for c in claude][0] == "claude-zenith-6"
    assert [c["id"] for c in claude][-1] == "claude-haiku-4-5"
    assert all(c["label"] for c in claude)


def test_advertised_models_are_exactly_the_saved_rows():
    cat = make_catalogue()
    # No parity saved -> the four default family heads, in family order. A new
    # top model (zenith) and a mid model (nova) are NOT advertised until added.
    ads = advertised_models(catalogue=cat)
    ids = [claude_id for claude_id, _ in ads]
    assert ids == ["claude-fable-5", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]
    assert "claude-zenith-6" not in ids and "claude-nova-4" not in ids
    labels = dict(ads)
    assert labels["claude-fable-5"] == "Claude Fable 5 | GPT-5.6 Sol · medium"


def test_advertised_models_reflect_a_saved_list_with_claude_effort():
    cat = make_catalogue()
    saved = [
        {"claude_model": "claude-fable-5", "model": "gpt-5.6-sol",
         "effort": "high", "claude_effort": "xhigh"},
        {"claude_model": "claude-haiku-4-5", "model": "gpt-5.6-luna",
         "effort": "low", "claude_effort": "high"},
    ]
    labels = dict(advertised_models(saved, catalogue=cat))
    ids = [cid for cid, _ in advertised_models(saved, catalogue=cat)]
    assert ids == ["claude-fable-5", "claude-haiku-4-5"]  # exactly the saved rows, in order
    # fable accepts output_config.effort -> surfaced; codex effort stays last segment.
    assert labels["claude-fable-5"] == "Claude Fable 5 (effort xhigh) | GPT-5.6 Sol · high"
    # haiku rejects it (gate returns None) -> no effort annotation even if set.
    assert labels["claude-haiku-4-5"] == "Claude Haiku 4.5 | GPT-5.6 Luna · low"


def test_automatic_mapping_rows_come_from_the_saved_list():
    rows = {r["claude_model"]: r for r in automatic_mapping(catalogue=make_catalogue())}
    # Defaults only, until the user adds rows.
    assert set(rows) == {"claude-fable-5", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"}
    assert rows["claude-fable-5"]["claude_label"] == "Claude Fable 5"  # curated name kept
    assert rows["claude-fable-5"]["claude_effort"] is None
    assert all(r["claude_label"] for r in rows.values())


def test_map_model_resolves_catalogue_only_models():
    cat = make_catalogue()
    # Inherits the TOP CURATED tier, which is Fable's — no longer the flagship.
    assert map_model("claude-zenith-6", catalogue=cat) == OpenAIModelTarget("gpt-5.6-sol", "medium")
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
        # Default saved list -> the four family heads; first is the fable head.
        assert [mid for mid, _ in listing][0] == "claude-fable-5"
        assert "gpt-5.9-new" in selectable_models()
        assert any(r["claude_model"] == "claude-fable-5" for r in automatic_mapping())
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


# ---- Claude-side effort support gate (Feature 1) ----

import pytest
from claude_unlimited.openai_models import apply_claude_effort, claude_effort_for, normalize_parity, default_parity_rows


@pytest.mark.parametrize("model,requested,expected", [
    ("claude-fable-5-1", "xhigh", "xhigh"),   # top tier: full range
    ("claude-fable-5-1", "max", "max"),
    ("claude-opus-5", "max", "max"),
    ("claude-opus-4-8", "xhigh", "xhigh"),    # opus >=4.7: full range
    ("claude-opus-4-6", "xhigh", "high"),     # 4.6: all but xhigh -> clamp
    ("claude-opus-4-6", "max", "max"),
    ("claude-opus-4-5", "xhigh", "high"),     # 4.5: low/medium/high only
    ("claude-opus-4-5", "max", "high"),
    ("claude-sonnet-5", "xhigh", "xhigh"),
    ("claude-sonnet-4-6", "xhigh", "high"),
    ("claude-sonnet-4-5", "high", None),      # <=4.5 rejects the knob
    ("claude-haiku-4-5", "low", None),        # haiku never accepts it
    ("claude-haiku-4-5-20251001", "high", None),
    ("claude-mystery-9", "high", None),       # unknown family: never risk a 400
    ("claude-fable-5-1", "bogus", None),      # invalid level
    ("claude-fable-5-1", None, None),
])
def test_apply_claude_effort_gate(model, requested, expected):
    assert apply_claude_effort(model, requested) == expected


def test_claude_effort_for_matches_a_row_and_gates_by_model():
    cat = make_catalogue()
    saved = [
        {"claude_model": "claude-fable-5", "model": "gpt-5.6-sol", "effort": "high", "claude_effort": "max"},
        {"claude_model": "claude-haiku-4-5", "model": "gpt-5.6-luna", "effort": "low", "claude_effort": "high"},
    ]
    assert claude_effort_for("claude-fable-5", saved, cat) == "max"
    # a dated spelling still matches the row by base id
    assert claude_effort_for("claude-fable-5-20260101", saved, cat) == "max"
    # haiku's row sets it, but the model rejects it -> None (no injection)
    assert claude_effort_for("claude-haiku-4-5", saved, cat) is None
    # a model not in the saved list -> None
    assert claude_effort_for("claude-opus-5", saved, cat) is None
    # no row sets claude_effort -> None
    assert claude_effort_for("claude-fable-5", None, cat) is None


def test_normalize_parity_migrates_a_legacy_dict_to_the_default_rows_plus_extras():
    cat = make_catalogue()
    legacy = {
        "claude-opus-5": {"model": "gpt-5.6-luna", "effort": "low"},  # overlay a default
        "claude-zenith-6": {"model": "gpt-5.6-sol", "effort": "high"},  # a key outside defaults
    }
    rows = normalize_parity(legacy, cat)
    by_id = {r["claude_model"]: r for r in rows}
    # the four defaults are present, opus overlaid
    assert set(["claude-fable-5", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]) <= set(by_id)
    assert by_id["claude-opus-5"]["model"] == "gpt-5.6-luna"
    # the non-default key survives as its own appended row
    assert by_id["claude-zenith-6"]["model"] == "gpt-5.6-sol"


def test_normalize_parity_empty_shapes_yield_the_defaults():
    cat = make_catalogue()
    defaults = [r["claude_model"] for r in default_parity_rows(cat)]
    for empty in (None, {}, []):
        assert [r["claude_model"] for r in normalize_parity(empty, cat)] == defaults


def test_normalize_parity_fills_missing_fields_in_a_list_row():
    cat = make_catalogue()
    rows = normalize_parity([{"claude_model": "claude-opus-5"}], cat)  # no model/effort
    assert rows == [{"claude_model": "claude-opus-5", "model": "gpt-5.6-terra",
                     "effort": "high", "claude_effort": None}]


def test_default_rows_pick_the_newest_version_of_each_family_not_the_priciest():
    # LiteLLM sometimes prices an older point release above the flagship; the
    # default parity row for a family must still be the newest version.
    cat = make_catalogue(extra={
        "claude-sonnet-4-6": entry("anthropic", 3e-05),  # priced ABOVE sonnet-5 (1e-05)
    })
    heads = {r["claude_model"] for r in default_parity_rows(cat)}
    assert "claude-sonnet-5" in heads       # newest wins
    assert "claude-sonnet-4-6" not in heads  # not the prilier older one


def test_unmapped_reports_a_family_with_no_saved_row():
    cat = make_catalogue()
    # Defaults cover fable/opus/sonnet/haiku, so only the families the
    # catalogue added are outstanding.
    ids = [r["claude_model"] for r in unmapped_claude_models(catalogue=cat)]
    assert ids == ["claude-zenith-6", "claude-nova-4"]
    zenith = unmapped_claude_models(catalogue=cat)[0]
    assert zenith["would_map_to"] == "gpt-5.6-sol"
    assert zenith["would_use_effort"] == "medium"
    assert zenith["claude_label"]


def test_unmapped_ignores_models_older_than_the_saved_decision():
    # The real catalogue keeps every point release forever. Against a saved
    # opus-5 / sonnet-5, the 4.x releases are NOT news — reporting them made
    # the live banner list 10 models, 9 of them stale.
    cat = make_catalogue(extra={
        "claude-opus-4-8": entry("anthropic", 2.4e-05),
        "claude-opus-4-5": entry("anthropic", 2.2e-05),
        "claude-sonnet-4-6": entry("anthropic", 9e-06),
    })
    ids = [r["claude_model"] for r in unmapped_claude_models(catalogue=cat)]
    assert "claude-opus-4-8" not in ids
    assert "claude-opus-4-5" not in ids
    assert "claude-sonnet-4-6" not in ids


def test_unmapped_reports_a_model_newer_than_its_saved_row():
    # The case the banner exists for: the family is already mapped, and
    # something newer than that decision shipped. It needs an EXPLICIT saved
    # list — with no saved list the defaults track the catalogue's newest
    # member per family, so by construction nothing is ever outstanding.
    cat = make_catalogue(extra={"claude-sonnet-6": entry("anthropic", 1.4e-05)})
    saved = [{"claude_model": "claude-sonnet-5", "model": "gpt-5.6-terra",
              "effort": "medium", "claude_effort": None}]
    ids = [r["claude_model"] for r in unmapped_claude_models(saved, catalogue=cat)]
    assert "claude-sonnet-6" in ids
    assert "claude-sonnet-5" not in ids


def test_unmapped_reports_only_the_newest_member_of_an_unknown_family():
    cat = make_catalogue(extra={
        "claude-mythos-5-1": entry("anthropic", 6e-05),
        "claude-mythos-5": entry("anthropic", 5.5e-05),
    })
    ids = [r["claude_model"] for r in unmapped_claude_models(catalogue=cat)]
    assert "claude-mythos-5-1" in ids
    assert "claude-mythos-5" not in ids   # one decision to make, not a back catalogue


def test_unmapped_never_reports_an_unversioned_preview():
    cat = make_catalogue(extra={"claude-mythos-preview": entry("anthropic", 6e-05)})
    ids = [r["claude_model"] for r in unmapped_claude_models(catalogue=cat)]
    assert "claude-mythos-preview" not in ids


def test_unmapped_counts_a_dated_spelling_as_covered():
    cat = make_catalogue()
    saved = [{"claude_model": "claude-zenith-6-20260901", "model": "gpt-5.6-sol",
              "effort": "medium", "claude_effort": None}]
    assert "claude-zenith-6" not in [r["claude_model"] for r in unmapped_claude_models(saved, catalogue=cat)]


def test_unmapped_is_empty_without_a_catalogue():
    # No catalogue means no way to know a model exists — never raise the
    # banner on a guess.
    assert unmapped_claude_models() == []
