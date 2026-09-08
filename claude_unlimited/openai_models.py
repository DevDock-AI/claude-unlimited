"""Claude model -> OpenAI/Codex model + reasoning-effort mapping.

Pure, no I/O of its own. Model NAMES come from model_catalogue.current()
when the daemon has initialized it (LiteLLM via GitHub's API, with disk
cache and a vendored snapshot behind it); the literals below survive as the
fallback when no catalogue is loaded, and as the CURATED tier decisions.

The tiering is best-effort, matched by price/role parity between the Codex
model catalog and Anthropic's published pricing; neither vendor documents an
equivalence. The tiers are deliberately conservative, because Codex quota is
spent on reasoning output weighted by model tier — not on the size of the
request (docs/adr/0007). `gpt-5.6-sol` is the expensive one and is reserved
for the top Claude tier; everything below it runs on a cheaper model, so an
ordinary session does not sit on the most expensive target by default.
Raising a row here raises what a session costs, so treat it as a spending
decision — which is exactly why a catalogue refresh NEVER changes what a
model already in _MODEL_MAP maps to. A Claude model the catalogue knows and
this table does not is slotted onto an existing curated tier by capability
rank (see effective_model_map), never onto a brand-new target.

Every public function takes an optional injected `catalogue` so it stays
unit-testable without the module-level state; None means "whatever
model_catalogue.current() says", which before initialize() is None too —
so tests that never initialize run entirely on the literals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import model_catalogue
from .model_catalogue import Catalogue, base_id

VALID_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")


@dataclass(frozen=True)
class OpenAIModelTarget:
    model: str
    reasoning_effort: str


# Ordered most-capable-first — used only for the substring-match fallback below.
_MODEL_MAP: dict[str, OpenAIModelTarget] = {
    "claude-fable-5": OpenAIModelTarget("gpt-5.6-sol", "high"),
    "claude-opus-5": OpenAIModelTarget("gpt-5.6-terra", "high"),
    "claude-sonnet-5": OpenAIModelTarget("gpt-5.6-terra", "medium"),
    "claude-haiku-4-5-20251001": OpenAIModelTarget("gpt-5.6-luna", "low"),
}

# Any unrecognized Claude model id falls back to the balanced, mid-tier pick
# rather than guessing at a specific match.
_DEFAULT_TARGET = OpenAIModelTarget("gpt-5.6-terra", "medium")

# Family-prefix fallback for a model id that isn't an exact match above but
# still names a recognizable tier (e.g. a dated Sonnet id this table hasn't
# been updated for). Checked in order, first match wins, before
# _DEFAULT_TARGET.
_FAMILY_FALLBACKS: list[tuple[str, OpenAIModelTarget]] = [
    ("claude-fable", OpenAIModelTarget("gpt-5.6-sol", "high")),
    ("claude-opus", OpenAIModelTarget("gpt-5.6-terra", "high")),
    ("claude-sonnet", OpenAIModelTarget("gpt-5.6-terra", "medium")),
    ("claude-haiku", OpenAIModelTarget("gpt-5.6-luna", "low")),
]


def _curated_target(claude_id: str) -> Optional[OpenAIModelTarget]:
    """The hand-curated tier decision for a Claude model, matched exactly or
    by dated/undated base id (the catalogue may spell Haiku undated while
    the table keys the dated id — same model, same decision)."""
    if claude_id in _MODEL_MAP:
        return _MODEL_MAP[claude_id]
    base = base_id(claude_id)
    for curated_id, target in _MODEL_MAP.items():
        if base_id(curated_id) == base:
            return target
    return None


def _catalogue_or_current(catalogue: Optional[Catalogue]) -> Optional[Catalogue]:
    return catalogue if catalogue is not None else model_catalogue.current()


def effective_model_map(catalogue: Optional[Catalogue] = None) -> dict[str, OpenAIModelTarget]:
    """The Claude->Codex table actually in force, most-capable-first.

    Without a catalogue this IS _MODEL_MAP. With one, the row set and order
    come from the catalogue's Claude lineup, but the targets stay curated:
    a model _MODEL_MAP lists keeps its exact target and effort (a documented
    spending decision), and a NEW model inherits the tier of the strongest
    curated model it does not outrank — so a model above the top curated one
    gets the top tier, and one slotting between Opus and Sonnet gets
    Sonnet's (the conservative neighbour). The tiers therefore never
    collapse onto one model and never invent a new spending level."""
    cat = _catalogue_or_current(catalogue)
    if cat is None or not cat.anthropic:
        return dict(_MODEL_MAP)
    lineup = list(cat.anthropic)
    curated = [_curated_target(m.id) for m in lineup]
    if not any(curated):
        # No curated model recognized at all (a fully renamed lineup):
        # spread the curated tiers across the new lineup by rank rather
        # than collapsing everything onto one target.
        tiers = list(_MODEL_MAP.values())
        span = max(len(lineup) - 1, 1)
        return {m.id: tiers[round(i * (len(tiers) - 1) / span)]
                for i, m in enumerate(lineup)}
    out: dict[str, OpenAIModelTarget] = {}
    last_curated = next(c for c in reversed(curated) if c is not None)
    for i, model in enumerate(lineup):
        target = curated[i]
        if target is None:
            target = next((curated[j] for j in range(i + 1, len(lineup))
                           if curated[j] is not None), last_curated)
        out[model.id] = target
    return out


def map_model(requested_claude_model: Optional[str], *, override_model: Optional[str] = None,
              override_reasoning_effort: Optional[str] = None,
              parity: Optional[dict] = None,
              catalogue: Optional[Catalogue] = None) -> OpenAIModelTarget:
    """Resolves what to send to OpenAI for a given incoming Claude model id.

    Precedence, narrowest first: a per-Profile override
    (Profile.codex_model / codex_reasoning_effort) beats the user's parity
    map, which beats the built-in table. Model and effort are independent at
    every level, so overriding only the model keeps the effort this model
    would otherwise have used."""
    mapped = _resolve(requested_claude_model, parity, catalogue)
    if override_model is not None:
        # Effort falls back to the effort for THIS model, not the global
        # default. Taking _DEFAULT_TARGET's "medium" here meant a Haiku
        # request whose Profile overrode only the model ran at medium instead
        # of low — and effort is what Codex quota is actually spent on
        # (docs/adr/0007), so that was a silent overspend.
        base = OpenAIModelTarget(override_model, override_reasoning_effort or mapped.reasoning_effort)
    else:
        base = mapped
    if override_reasoning_effort is not None:
        base = OpenAIModelTarget(base.model, override_reasoning_effort)
    return base


def _resolve(requested_claude_model: Optional[str],
             parity: Optional[dict] = None,
             catalogue: Optional[Catalogue] = None) -> OpenAIModelTarget:
    effective = effective_model_map(catalogue)
    if not requested_claude_model:
        return _apply_parity(_DEFAULT_TARGET, parity, None)
    if requested_claude_model in effective:
        return _apply_parity(effective[requested_claude_model], parity, requested_claude_model)
    # A dated spelling of a model the table carries undated (or vice versa)
    # is the same model, and must land on the same row a parity override or
    # the Dashboard table would use.
    base = base_id(requested_claude_model)
    for row_id, target in effective.items():
        if base_id(row_id) == base:
            return _apply_parity(target, parity, row_id)
    lowered = requested_claude_model.lower()
    for prefix, target in _FAMILY_FALLBACKS:
        if prefix in lowered:
            # Keyed on the canonical id the family resolves to, so an override
            # for "claude-opus-5" also covers a dated Opus id.
            canonical = next((c for c, t in _MODEL_MAP.items() if t == target), None)
            return _apply_parity(target, parity, canonical)
    return _apply_parity(_DEFAULT_TARGET, parity, None)


def _apply_parity(target: OpenAIModelTarget, parity: Optional[dict],
                  claude_id: Optional[str]) -> OpenAIModelTarget:
    """Overlays a user-configured row onto the built-in mapping.

    Model and effort are independent, so overriding one keeps the shipped
    default for the other — the same rule the per-Profile overrides follow."""
    if not parity or not claude_id:
        return target
    row = parity.get(claude_id)
    if not isinstance(row, dict):
        return target
    return OpenAIModelTarget(row.get("model") or target.model,
                             row.get("effort") or target.reasoning_effort)


# Display names for the OpenAI models this mapping can target. Only used to
# label the /v1/models listing a codex Profile serves — never sent upstream.
_OPENAI_DISPLAY_NAMES: dict[str, str] = {
    "gpt-5.6-sol": "GPT-5.6 Sol",
    "gpt-5.6-terra": "GPT-5.6 Terra",
    "gpt-5.6-luna": "GPT-5.6 Luna",
}


# Ordered most- to least-capable. A model id is a moving target: OpenAI
# retires them, and the Codex subscription backend refuses some outright
# ("The 'gpt-5.6-codex' model is not supported when using Codex with a ChatGPT
# account"). Rather than hardcode one id per tier and fail hard when it goes
# away, a rejected model walks down this ladder, so the pool keeps working as
# long as any one model in it is still served.
_MODEL_LADDER: tuple[str, ...] = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")


def model_ladder(catalogue: Optional[Catalogue] = None) -> list[str]:
    """The fallback ladder in force, most-capable-first.

    Derived from the effective map's targets (which follow the catalogue's
    Claude lineup order), so a lineup change moves the ladder too; the
    literal rungs are appended so a known-good model is never lost from the
    walk. Without a catalogue this is exactly _MODEL_LADDER."""
    cat = _catalogue_or_current(catalogue)
    if cat is None or not cat.anthropic:
        return list(_MODEL_LADDER)
    ladder: list[str] = []
    for target in effective_model_map(cat).values():
        if target.model not in ladder:
            ladder.append(target.model)
    for rung in _MODEL_LADDER:
        if rung not in ladder:
            ladder.append(rung)
    return ladder


def fallback_models(model: str, catalogue: Optional[Catalogue] = None) -> list[str]:
    """Models to try, in order, after `model` was rejected.

    Starts one rung below `model` so a downgrade never re-tries something more
    capable that is likely rejected for the same reason, then wraps to the
    rungs above so a retired mid-tier model can still reach a working one. A
    model outside the ladder (a Profile override, or a lineup this build has
    never heard of) falls back to the whole ladder."""
    ladder = model_ladder(catalogue)
    if model not in ladder:
        return ladder
    index = ladder.index(model)
    return ladder[index + 1:] + ladder[:index]


_CLAUDE_DISPLAY_NAMES: dict[str, str] = {
    "claude-fable-5": "Claude Fable 5",
    "claude-opus-5": "Claude Opus 5",
    "claude-sonnet-5": "Claude Sonnet 5",
    "claude-haiku-4-5-20251001": "Claude Haiku 4.5",
}


def _claude_label(claude_id: str, cat: Optional[Catalogue]) -> str:
    if claude_id in _CLAUDE_DISPLAY_NAMES:  # curated names stay stable
        return _CLAUDE_DISPLAY_NAMES[claude_id]
    if cat is not None:
        for model in cat.anthropic:
            if model.id == claude_id:
                return model.display_name
    return model_catalogue.display_name_for(claude_id)


def _openai_label(model_id: str, cat: Optional[Catalogue]) -> str:
    if model_id in _OPENAI_DISPLAY_NAMES:  # curated names stay stable
        return _OPENAI_DISPLAY_NAMES[model_id]
    if cat is not None:
        for model in cat.openai:
            if model.id == model_id:
                return model.display_name
    return model_id


def automatic_mapping(parity: Optional[dict] = None,
                      catalogue: Optional[Catalogue] = None) -> list[dict]:
    """The mapping table the Dashboard shows when a codex Profile is left on
    automatic.

    Derived from _MODEL_MAP rather than restated in the page, because it was
    restated there once and silently went stale the first time the mapping
    changed — the modal kept advertising a model and effort the bridge had
    stopped using."""
    cat = _catalogue_or_current(catalogue)
    rows = []
    for claude_id, target in effective_model_map(cat).items():
        effective = _apply_parity(target, parity, claude_id)
        rows.append({
            "claude_model": claude_id,
            "claude_label": _claude_label(claude_id, cat),
            "openai_model": effective.model,
            "reasoning_effort": effective.reasoning_effort,
            "default_model": target.model,
            "default_effort": target.reasoning_effort,
            "overridden": effective != target,
        })
    return rows


def selectable_models(catalogue: Optional[Catalogue] = None) -> list[str]:
    """Every OpenAI model id the Dashboard may offer in a dropdown.

    Served rather than restated in the page: the mapping table was hardcoded
    in index.html once and went stale the first time the lineup changed, and a
    second hardcoded copy in app.js would fail the same way. With a catalogue
    loaded the current OpenAI chat lineup is offered too, so pinning a
    Profile to a model this build has never heard of needs no release."""
    cat = _catalogue_or_current(catalogue)
    seen = model_ladder(cat)
    for target in effective_model_map(cat).values():
        if target.model not in seen:
            seen.append(target.model)
    if cat is not None:
        for model in cat.openai:
            if model.id not in seen:
                seen.append(model.id)
    for extra in _LEGACY_SELECTABLE:
        if extra not in seen:
            seen.append(extra)
    return seen


# Older ids that remain selectable for a Profile pinned to one, even though
# nothing maps to them by default.
_LEGACY_SELECTABLE: tuple[str, ...] = ("gpt-5.5", "gpt-5.2")


def advertised_models(parity: Optional[dict] = None,
                      catalogue: Optional[Catalogue] = None) -> list[tuple[str, str]]:
    """(model_id, display_name) pairs for the Anthropic-shaped /v1/models
    listing a codex Profile answers with, newest-capability-first.

    The ids stay Anthropic-shaped on purpose: Claude Code sends the picked
    id straight back in /v1/messages and map_model() is keyed on exactly
    these, so advertising raw OpenAI ids would make every pick fall through
    to _DEFAULT_TARGET and collapse the tier system onto one model. The
    display name is where the backing model is surfaced.

    Derived from the effective map so the picker can't drift out of sync
    with the mapping — and, once a catalogue is loaded, follows the live
    Claude lineup instead of the literals."""
    cat = _catalogue_or_current(catalogue)
    out: list[tuple[str, str]] = []
    for claude_id, target in effective_model_map(cat).items():
        target = _apply_parity(target, parity, claude_id)
        claude_label = _claude_label(claude_id, cat)
        backing = _openai_label(target.model, cat)
        # `<anthropic model> | <openai model> · <effort>` so the `/model` picker
        # in a `cu code` session mirrors the Dashboard's parity table: each row
        # names both the Claude tier being picked and the OpenAI/Codex model it
        # actually routes to (effort kept — it's what Codex quota is spent on).
        out.append((claude_id, f"{claude_label} | {backing} · {target.reasoning_effort}"))
    return out
