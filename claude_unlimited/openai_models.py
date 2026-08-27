"""Claude model -> OpenAI/Codex model + reasoning-effort mapping.

Pure, no I/O. The tiering is best-effort, matched by price/role parity
between the Codex model catalog and Anthropic's published pricing; neither
vendor documents an equivalence. Revisit when either lineup changes.

The tiers are deliberately conservative, because Codex quota is spent on
reasoning output weighted by model tier — not on the size of the request
(docs/adr/0007). `gpt-5.6-sol` is the expensive one and is reserved for the
top Claude tier; everything below it runs on a cheaper model, so an ordinary
session does not sit on the most expensive target by default. Raising a row
here raises what a session costs, so treat it as a spending decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

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


def map_model(requested_claude_model: Optional[str], *, override_model: Optional[str] = None,
              override_reasoning_effort: Optional[str] = None,
              parity: Optional[dict] = None) -> OpenAIModelTarget:
    """Resolves what to send to OpenAI for a given incoming Claude model id.

    Precedence, narrowest first: a per-Profile override
    (Profile.codex_model / codex_reasoning_effort) beats the user's parity
    map, which beats the built-in table. Model and effort are independent at
    every level, so overriding only the model keeps the effort this model
    would otherwise have used."""
    mapped = _resolve(requested_claude_model, parity)
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
             parity: Optional[dict] = None) -> OpenAIModelTarget:
    if not requested_claude_model:
        return _apply_parity(_DEFAULT_TARGET, parity, None)
    if requested_claude_model in _MODEL_MAP:
        return _apply_parity(_MODEL_MAP[requested_claude_model], parity, requested_claude_model)
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


def fallback_models(model: str) -> list[str]:
    """Models to try, in order, after `model` was rejected.

    Starts one rung below `model` so a downgrade never re-tries something more
    capable that is likely rejected for the same reason, then wraps to the
    rungs above so a retired mid-tier model can still reach a working one. A
    model outside the ladder (a Profile override, or a lineup this build has
    never heard of) falls back to the whole ladder."""
    if model not in _MODEL_LADDER:
        return list(_MODEL_LADDER)
    index = _MODEL_LADDER.index(model)
    return list(_MODEL_LADDER[index + 1:]) + list(_MODEL_LADDER[:index])


_CLAUDE_DISPLAY_NAMES: dict[str, str] = {
    "claude-fable-5": "Claude Fable 5",
    "claude-opus-5": "Claude Opus 5",
    "claude-sonnet-5": "Claude Sonnet 5",
    "claude-haiku-4-5-20251001": "Claude Haiku 4.5",
}


def automatic_mapping(parity: Optional[dict] = None) -> list[dict]:
    """The mapping table the Dashboard shows when a codex Profile is left on
    automatic.

    Derived from _MODEL_MAP rather than restated in the page, because it was
    restated there once and silently went stale the first time the mapping
    changed — the modal kept advertising a model and effort the bridge had
    stopped using."""
    rows = []
    for claude_id, target in _MODEL_MAP.items():
        effective = _apply_parity(target, parity, claude_id)
        rows.append({
            "claude_model": claude_id,
            "claude_label": _CLAUDE_DISPLAY_NAMES.get(claude_id, claude_id),
            "openai_model": effective.model,
            "reasoning_effort": effective.reasoning_effort,
            "default_model": target.model,
            "default_effort": target.reasoning_effort,
            "overridden": effective != target,
        })
    return rows


def selectable_models() -> list[str]:
    """Every OpenAI model id the Dashboard may offer in a dropdown.

    Served rather than restated in the page: the mapping table was hardcoded
    in index.html once and went stale the first time the lineup changed, and a
    second hardcoded copy in app.js would fail the same way."""
    seen = list(_MODEL_LADDER)
    for target in _MODEL_MAP.values():
        if target.model not in seen:
            seen.append(target.model)
    for extra in _LEGACY_SELECTABLE:
        if extra not in seen:
            seen.append(extra)
    return seen


# Older ids that remain selectable for a Profile pinned to one, even though
# nothing maps to them by default.
_LEGACY_SELECTABLE: tuple[str, ...] = ("gpt-5.5", "gpt-5.2")


def advertised_models(parity: Optional[dict] = None) -> list[tuple[str, str]]:
    """(model_id, display_name) pairs for the Anthropic-shaped /v1/models
    listing a codex Profile answers with, newest-capability-first.

    The ids stay Anthropic-shaped on purpose: Claude Code sends the picked
    id straight back in /v1/messages and map_model() is keyed on exactly
    these, so advertising raw OpenAI ids would make every pick fall through
    to _DEFAULT_TARGET and collapse the tier system onto one model. The
    display name is where the backing model is surfaced.

    Derived from _MODEL_MAP so the picker can't drift out of sync with the
    mapping."""
    out: list[tuple[str, str]] = []
    for claude_id, target in _MODEL_MAP.items():
        target = _apply_parity(target, parity, claude_id)
        backing = _OPENAI_DISPLAY_NAMES.get(target.model, target.model)
        out.append((claude_id, f"{backing} · {target.reasoning_effort} reasoning"))
    return out
