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
              override_reasoning_effort: Optional[str] = None) -> OpenAIModelTarget:
    """Resolves what to send to OpenAI for a given incoming Claude model id.

    A per-Profile override (Profile.codex_model / codex_reasoning_effort)
    wins over the automatic mapping. The two are independent, so overriding
    only the model keeps the mapping's reasoning-effort default, and vice
    versa."""
    if override_model is not None:
        base = OpenAIModelTarget(override_model, override_reasoning_effort or _DEFAULT_TARGET.reasoning_effort)
    else:
        base = _resolve(requested_claude_model)
    if override_reasoning_effort is not None:
        base = OpenAIModelTarget(base.model, override_reasoning_effort)
    return base


def _resolve(requested_claude_model: Optional[str]) -> OpenAIModelTarget:
    if not requested_claude_model:
        return _DEFAULT_TARGET
    if requested_claude_model in _MODEL_MAP:
        return _MODEL_MAP[requested_claude_model]
    lowered = requested_claude_model.lower()
    for prefix, target in _FAMILY_FALLBACKS:
        if prefix in lowered:
            return target
    return _DEFAULT_TARGET


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


def automatic_mapping() -> list[dict[str, str]]:
    """The mapping table the Dashboard shows when a codex Profile is left on
    automatic.

    Derived from _MODEL_MAP rather than restated in the page, because it was
    restated there once and silently went stale the first time the mapping
    changed — the modal kept advertising a model and effort the bridge had
    stopped using."""
    return [
        {
            "claude_model": claude_id,
            "claude_label": _CLAUDE_DISPLAY_NAMES.get(claude_id, claude_id),
            "openai_model": target.model,
            "reasoning_effort": target.reasoning_effort,
        }
        for claude_id, target in _MODEL_MAP.items()
    ]


def advertised_models() -> list[tuple[str, str]]:
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
        backing = _OPENAI_DISPLAY_NAMES.get(target.model, target.model)
        out.append((claude_id, f"{backing} · {target.reasoning_effort} reasoning"))
    return out
