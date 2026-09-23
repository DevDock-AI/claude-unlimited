"""Claude model -> OpenAI/Codex model + reasoning-effort mapping.

Pure, no I/O of its own. Model NAMES come from model_catalogue.current()
when the daemon has initialized it (LiteLLM via GitHub's API, with disk
cache and a vendored snapshot behind it); the literals below survive as the
fallback when no catalogue is loaded, and as the CURATED tier decisions.

The tiering is best-effort, matched by price/role parity between the Codex
model catalog and Anthropic's published pricing; neither vendor documents an
equivalence. The tiers are deliberately conservative, because Codex quota is
spent on reasoning output weighted by model tier — not on the size of the
request (docs/adr/0007). `gpt-6-astra` is the flagship, and NO tier maps to it
by default: Claude Code picks Fable on its own for ordinary work, so mapping
Fable to the flagship meant a user who never chose the expensive model still
had their Codex quota spent at the top tier. Fable therefore lands on
`gpt-5.6-sol` at medium effort, and astra stays one explicit edit away in the
parity table for anyone who wants it.
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

import re

from dataclasses import dataclass
from typing import Optional

from . import model_catalogue
from .model_catalogue import Catalogue, base_id

# "none" turns reasoning off entirely — verified live against gpt-5.6-terra and
# gpt-5.6-sol (0 reasoning tokens), and reasoning output is what a Codex
# subscription's quota charges for (ADR 0007). "minimal" was removed: the
# current lineup rejects it outright — "Unsupported value: 'minimal' is not
# supported with the 'gpt-5.6-sol' model. Supported values are: 'none', 'low',
# 'medium', 'high', 'xhigh', and 'max'." — so offering it only ever produced a
# failed request. "ultra" went the same way (issue #8): "Invalid value:
# 'ultra'. Supported values are: 'none', 'minimal', 'low', 'medium', 'high',
# 'xhigh', and 'max'."
VALID_REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
# Values a config saved by an older build may still hold, and what they mean
# now. Mapped on load and on save, so an old config neither fails validation
# nor sends a value the backend refuses.
_RETIRED_REASONING_EFFORTS = {"ultra": "max", "minimal": "low"}


def upgrade_reasoning_effort(effort):
    """A retired effort's current equivalent; anything else unchanged."""
    return _RETIRED_REASONING_EFFORTS.get(effort, effort)

# Claude-side reasoning effort — the `output_config.effort` knob on the Messages
# API (GA, no beta header). A DIFFERENT set from the Codex side above: Claude
# has no "none". A parity row's claude_effort, when set, is injected
# onto oauth/api-served /v1/messages requests for that model (see proxy.py);
# unset = passthrough (Claude Code's own choice), which is the zero-risk default.
CLAUDE_REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_CLAUDE_EFFORT_RANK = {e: i for i, e in enumerate(CLAUDE_REASONING_EFFORTS)}


def _clamp_effort(effort: str, ceiling: str) -> str:
    if _CLAUDE_EFFORT_RANK.get(effort, 0) <= _CLAUDE_EFFORT_RANK[ceiling]:
        return effort
    return ceiling


def apply_claude_effort(model_id: str, requested: Optional[str]) -> Optional[str]:
    """The output_config.effort value to actually send for `model_id` given a
    row's requested level — or None to SKIP injection entirely.

    output_config.effort support is per-model and UPSTREAM-COUPLED (keep in
    sync with the Messages API — see the Claude Code upstream-watch note). We
    are deliberately conservative: an unknown family/version, or a model that
    rejects the knob (Haiku, Sonnet <= 4.5), returns None so we never inject a
    value that would 400 every request. Where a model supports `max` but not
    `xhigh` (Opus/Sonnet 4.6) an `xhigh` request is clamped to `high`; where a
    model supports only low/medium/high (Opus 4.5) higher requests clamp down.
    thinking.budget_tokens is NOT used — it is removed (400) on this lineup."""
    if requested not in CLAUDE_REASONING_EFFORTS:
        return None
    base = base_id(model_id).lower()
    if "haiku" in base:
        return None
    if "fable" in base or "mythos" in base:
        return requested  # top tier: full range
    ver = model_catalogue._version_of(base)
    if "opus" in base:
        if ver >= 4.7:
            return requested
        if ver >= 4.6:
            return "high" if requested == "xhigh" else requested  # all but xhigh
        if ver >= 4.5:
            return _clamp_effort(requested, "high")               # low/medium/high
        return None
    if "sonnet" in base:
        if ver >= 5:
            return requested
        if ver >= 4.6:
            return "high" if requested == "xhigh" else requested  # all but xhigh
        return None                                               # <= 4.5 rejects it
    return None  # unknown family: don't risk a 400


@dataclass(frozen=True)
class OpenAIModelTarget:
    model: str
    reasoning_effort: str


# Ordered most-capable-first — used only for the substring-match fallback below.
_MODEL_MAP: dict[str, OpenAIModelTarget] = {
    "claude-fable-5": OpenAIModelTarget("gpt-5.6-sol", "medium"),
    "claude-opus-5-5": OpenAIModelTarget("gpt-5.6-terra", "high"),
    # Kept beside 5.5, not replaced by it: a saved parity row naming Opus 5
    # with no effort of its own takes its effort from here.
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
    ("claude-fable", OpenAIModelTarget("gpt-5.6-sol", "medium")),
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
              parity=None,
              catalogue: Optional[Catalogue] = None) -> OpenAIModelTarget:
    """Resolves what to send to OpenAI for a given incoming Claude model id.

    Precedence, narrowest first: a per-Profile override
    (Profile.codex_model / codex_reasoning_effort) beats the user's parity
    list, which beats the built-in table. Model and effort are independent at
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


# The four families the default parity list is seeded from, most-capable-first.
_DEFAULT_FAMILIES = ("claude-fable", "claude-opus", "claude-sonnet", "claude-haiku")


def _curated_fallback(requested_claude_model: Optional[str],
                      cat: Optional[Catalogue]) -> OpenAIModelTarget:
    """The built-in Claude->Codex target for a model, ignoring the user's
    parity list. Exact id -> dated/undated base -> family prefix -> default."""
    effective = effective_model_map(cat)
    if not requested_claude_model:
        return _DEFAULT_TARGET
    if requested_claude_model in effective:
        return effective[requested_claude_model]
    base = base_id(requested_claude_model)
    for row_id, target in effective.items():
        if base_id(row_id) == base:
            return target
    lowered = requested_claude_model.lower()
    for prefix, target in _FAMILY_FALLBACKS:
        if prefix in lowered:
            return target
    return _DEFAULT_TARGET


def default_parity_rows(catalogue: Optional[Catalogue] = None) -> list[dict]:
    """The parity list a fresh install (or a Reset) starts from: one row per
    family in _DEFAULT_FAMILIES, each the highest-ranked model of that family
    with its curated Codex target. With the vendored catalogue this is exactly
    claude-fable-5-1 / claude-opus-5-5 / claude-sonnet-5 / claude-haiku-4-5 —
    the same ids cli.py's _MODEL_TIER_IDS use, so the /model picker relabels
    the native tiers instead of duplicating them. No catalogue -> one
    _MODEL_MAP literal per family (the newest listed)."""
    cat = _catalogue_or_current(catalogue)
    rows: list[dict] = []
    if cat is not None and cat.anthropic:
        for family in _DEFAULT_FAMILIES:
            members = [m for m in cat.anthropic if base_id(m.id).lower().startswith(family)]
            if not members:
                continue
            # The family's "main" model is the NEWEST version, not the
            # highest-priced — LiteLLM sometimes prices an older point release
            # above the flagship, which would otherwise seed Sonnet 4.6 instead
            # of Sonnet 5. Ties keep catalogue (cost-desc) order via max()'s
            # first-max rule.
            head = max(members, key=lambda m: model_catalogue._version_of(m.id)).id
            target = _curated_fallback(head, cat)
            rows.append({"claude_model": head, "model": target.model,
                         "effort": target.reasoning_effort, "claude_effort": None})
    if not rows:
        # One row per family here too: _MODEL_MAP can hold several ids of one
        # family (Opus 5.5 beside Opus 5), newest first, and a fresh install
        # must not start with two Opus rows.
        seen: set[str] = set()
        for claude_id, target in _MODEL_MAP.items():
            family = next((f for f in _DEFAULT_FAMILIES if claude_id.startswith(f)), claude_id)
            if family in seen:
                continue
            seen.add(family)
            rows.append({"claude_model": claude_id, "model": target.model,
                         "effort": target.reasoning_effort, "claude_effort": None})
    return rows


def normalize_parity(raw, catalogue: Optional[Catalogue] = None) -> list[dict]:
    """The parity list actually in force, as an ordered list of complete rows
    ({claude_model, model, effort, claude_effort}).

    Accepts every shape the config has ever stored, so old files and export
    bundles keep working without a migration pass on load:
      * a list -> each row with model/effort filled from the curated default
        for that Claude id when absent;
      * a non-empty legacy sparse dict {claude_id: {model,effort}} -> the
        default rows with those overlays applied (matched by base id), plus a
        row appended for any dict key outside the defaults;
      * {} / [] / None -> the default rows (never "advertise nothing", which
        would give Claude Code an empty /v1/models).
    """
    cat = _catalogue_or_current(catalogue)
    defaults = default_parity_rows(cat)
    if isinstance(raw, list):
        rows: list[dict] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            claude_id = entry.get("claude_model")
            if not claude_id:
                continue
            fallback = _curated_fallback(claude_id, cat)
            rows.append({
                "claude_model": claude_id,
                "model": entry.get("model") or fallback.model,
                "effort": entry.get("effort") or fallback.reasoning_effort,
                "claude_effort": entry.get("claude_effort") or None,
            })
        return rows or defaults
    if isinstance(raw, dict) and raw:
        rows = [dict(row) for row in defaults]
        by_base = {base_id(row["claude_model"]): row for row in rows}
        for claude_id, overlay in raw.items():
            if not isinstance(overlay, dict):
                continue
            row = by_base.get(base_id(claude_id))
            if row is not None:
                if overlay.get("model"):
                    row["model"] = overlay["model"]
                if overlay.get("effort"):
                    row["effort"] = overlay["effort"]
                if overlay.get("claude_effort"):
                    row["claude_effort"] = overlay["claude_effort"]
            else:
                fallback = _curated_fallback(claude_id, cat)
                rows.append({
                    "claude_model": claude_id,
                    "model": overlay.get("model") or fallback.model,
                    "effort": overlay.get("effort") or fallback.reasoning_effort,
                    "claude_effort": overlay.get("claude_effort") or None,
                })
        return rows
    return defaults


def _match_row(rows: list[dict], requested_claude_model: str) -> Optional[dict]:
    """The parity row that governs a requested model: exact id, then
    dated/undated base id, then family prefix."""
    for row in rows:
        if row["claude_model"] == requested_claude_model:
            return row
    base = base_id(requested_claude_model)
    for row in rows:
        if base_id(row["claude_model"]) == base:
            return row
    lowered = requested_claude_model.lower()
    for family in _DEFAULT_FAMILIES:
        if family in lowered:
            for row in rows:
                if base_id(row["claude_model"]).lower().startswith(family):
                    return row
    return None


def _resolve(requested_claude_model: Optional[str], parity=None,
             catalogue: Optional[Catalogue] = None) -> OpenAIModelTarget:
    cat = _catalogue_or_current(catalogue)
    if requested_claude_model:
        row = _match_row(normalize_parity(parity, cat), requested_claude_model)
        if row is not None:
            return OpenAIModelTarget(row["model"], row["effort"])
    # Not in the saved list (or no model requested): fall back to the built-in
    # curated target so a typed-but-unlisted id still routes sanely.
    return _curated_fallback(requested_claude_model, cat)


def claude_effort_for(requested_claude_model: Optional[str], parity=None,
                      catalogue: Optional[Catalogue] = None) -> Optional[str]:
    """The output_config.effort to inject for an oauth/api-served request, or
    None to leave the request untouched. Matches the requested model to a
    parity row, then runs the row's claude_effort through the per-model support
    gate (apply_claude_effort)."""
    if not requested_claude_model:
        return None
    cat = _catalogue_or_current(catalogue)
    row = _match_row(normalize_parity(parity, cat), requested_claude_model)
    if row is None:
        return None
    return apply_claude_effort(requested_claude_model, row.get("claude_effort"))


# Display names for the OpenAI models this mapping can target. Only used to
# label the /v1/models listing a codex Profile serves — never sent upstream.
_OPENAI_DISPLAY_NAMES: dict[str, str] = {
    "gpt-6-astra": "GPT-6 Astra",
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
_MODEL_LADDER: tuple[str, ...] = ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")


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
    "claude-opus-5-5": "Claude Opus 5.5",
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


def automatic_mapping(parity=None,
                      catalogue: Optional[Catalogue] = None) -> list[dict]:
    """The parity table the Dashboard renders — one entry per SAVED row (the
    explicit list the user edits), in order.

    Each row also carries the built-in default for its Claude model, so the
    UI can show what a Reset would restore and mark a row as overridden."""
    cat = _catalogue_or_current(catalogue)
    rows = []
    for row in normalize_parity(parity, cat):
        claude_id = row["claude_model"]
        default = _curated_fallback(claude_id, cat)
        rows.append({
            "claude_model": claude_id,
            "claude_label": _claude_label(claude_id, cat),
            "openai_model": row["model"],
            "reasoning_effort": row["effort"],
            "claude_effort": row.get("claude_effort"),
            "default_model": default.model,
            "default_effort": default.reasoning_effort,
            "overridden": (row["model"] != default.model
                           or row["effort"] != default.reasoning_effort
                           or bool(row.get("claude_effort"))),
        })
    return rows


# Older ids that remain selectable for a Profile pinned to one, even though
# nothing maps to them by default.
_LEGACY_SELECTABLE: tuple[str, ...] = ("gpt-5.5", "gpt-5.2")


def selectable_models(catalogue: Optional[Catalogue] = None) -> list[str]:
    """Every OpenAI/Codex model id the Dashboard may offer in a dropdown,
    ordered most-expensive/most-capable FIRST.

    The order is the catalogue's own rank (model_catalogue._sort_key: OpenAI
    is generation-then-cost, with the documented o1-pro correction), so the
    flagship leads and legacy rungs trail. With a catalogue the live OpenAI
    lineup comes first; the curated ladder rungs and _LEGACY_SELECTABLE are
    appended so a known-good id is never lost. Without a catalogue this is the
    ladder (already most-capable-first) then legacy."""
    cat = _catalogue_or_current(catalogue)
    seen: list[str] = []
    if cat is not None:
        for model in cat.openai:  # already rank-ordered, cost/generation desc
            if model.id not in seen:
                seen.append(model.id)
    for rung in model_ladder(cat):
        if rung not in seen:
            seen.append(rung)
    for target in effective_model_map(cat).values():
        if target.model not in seen:
            seen.append(target.model)
    for extra in _LEGACY_SELECTABLE:
        if extra not in seen:
            seen.append(extra)
    return seen


def selectable_claude_models(catalogue: Optional[Catalogue] = None) -> list[dict]:
    """`[{"id","label"}]` of Claude models a parity row may pick, ordered
    most-expensive/most-capable first (the catalogue's Anthropic rank:
    cost-then-version desc). Without a catalogue, the 4 curated tiers."""
    cat = _catalogue_or_current(catalogue)
    if cat is not None and cat.anthropic:
        return [{"id": m.id, "label": _claude_label(m.id, cat)} for m in cat.anthropic]
    return [{"id": claude_id, "label": _claude_label(claude_id, None)} for claude_id in _MODEL_MAP]


def _family_of(model_id: str) -> str:
    """The versionless family key of a Claude id: claude-opus-4-8 ->
    claude-opus. Used to ask "is this model newer than the one I already
    decided about?", which a bare id cannot answer."""
    stripped = base_id(model_id)
    match = re.match(r"^([a-z]+(?:-[a-z]+)*?)-\d", stripped)
    return match.group(1) if match else stripped


def unmapped_claude_models(parity=None,
                           catalogue: Optional[Catalogue] = None) -> list[dict]:
    """Claude models worth telling the user they have not decided about yet.

    NOT simply "every catalogue model with no saved row": the catalogue keeps
    every point release it has ever seen, so that set is dominated by models
    OLDER than what the user already runs (opus-4-5 next to a saved opus-5).
    A banner listing those would nag permanently and, worse, offer to add
    obsolete models to the `/model` picker. Measured against the real
    catalogue it was 10 models, 9 of them stale.

    So a model is reported only when it is genuinely ahead of a decision:

    * its family already has a saved row and this model is NEWER than it
      (claude-fable-6 while fable-5-1 is saved), or
    * its family has no saved row at all, in which case only the family's
      newest member is reported — one row to decide, not a back catalogue.

    Unversioned ids (previews) are never reported: they are not a shipped
    model anyone needs to budget for, and they have no version to compare.

    Each entry carries the target a row would get, so the UI can say what
    adding it would cost without computing tiers itself."""
    cat = _catalogue_or_current(catalogue)
    if cat is None or not cat.anthropic:
        return []
    saved = normalize_parity(parity, cat)
    covered = {base_id(row["claude_model"]) for row in saved}
    saved_peak: dict[str, float] = {}
    for row in saved:
        family = _family_of(row["claude_model"])
        version = model_catalogue._version_of(row["claude_model"])
        saved_peak[family] = max(saved_peak.get(family, 0.0), version)

    candidates: dict[str, tuple[float, object]] = {}
    for model in cat.anthropic:
        if base_id(model.id) in covered:
            continue
        version = model_catalogue._version_of(model.id)
        if version <= 0.0:                      # preview / unversioned
            continue
        family = _family_of(model.id)
        if family in saved_peak:
            if version <= saved_peak[family]:   # older than the saved decision
                continue
        else:
            # Unknown family: keep only its newest member.
            best = candidates.get(family)
            if best is not None and best[0] >= version:
                continue
        candidates[family] = (version, model)

    out: list[dict] = []
    for model in cat.anthropic:                 # catalogue order, not dict order
        entry = candidates.get(_family_of(model.id))
        if entry is None or entry[1] is not model:
            continue
        target = _curated_fallback(model.id, cat)
        out.append({
            "claude_model": model.id,
            "claude_label": _claude_label(model.id, cat),
            "would_map_to": target.model,
            "would_use_effort": target.reasoning_effort,
        })
    return out


def advertised_models(parity=None,
                      catalogue: Optional[Catalogue] = None) -> list[tuple[str, str]]:
    """(model_id, display_name) pairs for the Anthropic-shaped /v1/models
    listing a codex Profile answers with — exactly the SAVED parity rows, in
    order. This is what the user's `/model` picker offers for Codex-served
    sessions: the explicit list is the advertised set.

    The ids stay Anthropic-shaped on purpose: Claude Code sends the picked id
    straight back in /v1/messages and map_model() is keyed on exactly these,
    so advertising raw OpenAI ids would make every pick fall through to
    _DEFAULT_TARGET. The display name surfaces the backing model (and the
    Claude-side effort when the row sets one and the model accepts it)."""
    cat = _catalogue_or_current(catalogue)
    out: list[tuple[str, str]] = []
    for row in normalize_parity(parity, cat):
        claude_id = row["claude_model"]
        claude_label = _claude_label(claude_id, cat)
        backing = _openai_label(row["model"], cat)
        applied = apply_claude_effort(claude_id, row.get("claude_effort"))
        lead = f"{claude_label} (effort {applied})" if applied else claude_label
        # `<claude> | <openai> · <codex effort>` — codex effort stays the LAST
        # ` · ` segment so cli.py's _fetch_parity_labels keeps parsing it out.
        out.append((claude_id, f"{lead} | {backing} · {row['effort']}"))
    return out


def model_bucket_name(model_id: Optional[str]) -> Optional[str]:
    """The per-model usage bucket a requested Claude model is counted against,
    as the provider names it: `claude-fable-5-1` -> "Fable".

    Derived from the family prefix, never from a table: we control neither the
    bucket names nor which plans have them, so a configured matrix would go
    stale. A model whose family is not one of the
    four heads is UNRESTRICTED — None, which every caller must read as "no
    per-model limit applies", not as "blocked"."""
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    lowered = base_id(model_id).lower()
    for family in _DEFAULT_FAMILIES:
        if lowered.startswith(family):
            # "claude-fable" -> "Fable". The provider titles the display name
            # exactly this way; the comparison is case-insensitive anyway.
            return family.split("-", 1)[1].capitalize()
    return None


def bucket_matches(model_id: Optional[str], bucket_name: Optional[str]) -> bool:
    """Whether a per-model usage bucket governs this requested model. Case
    folded, because the two names come from different sources: ours from the
    model id, theirs from the provider's display name."""
    bucket = model_bucket_name(model_id)
    if bucket is None or not isinstance(bucket_name, str):
        return False
    return bucket.lower() == bucket_name.strip().lower()


def codex_profile_windows(profile, parity=None, catalogue: Optional[Catalogue] = None) -> dict:
    """What the capacity guard (docs/adr/0009) would budget for this codex
    Profile: the GPT ids its in-force parity rows resolve to (plus the
    default target, since an id outside the rows still maps somewhere), and
    the SMALLEST window/budget among them — the number a session on this
    account is actually bounded by. `assumed` is True when any of those ids
    is not in gpt_windows' table. Never raises; a broken parity list simply
    reports the default target."""
    from . import gpt_windows

    auth_mode = getattr(profile, "auth_mode", None)
    base_url = getattr(profile, "base_url", None)
    override = getattr(profile, "codex_model", None)
    effort = getattr(profile, "codex_reasoning_effort", None)
    claude_ids: list = [None]
    try:
        rows = normalize_parity(parity, catalogue)
        for row in rows:
            claude_model = row.get("claude_model")
            if isinstance(claude_model, str) and claude_model:
                claude_ids.append(claude_model)
    except Exception:
        rows = None
    targets: list[str] = []
    for claude_id in claude_ids:
        try:
            model = map_model(claude_id, override_model=override, override_reasoning_effort=effort,
                              parity=rows, catalogue=catalogue).model
        except Exception:
            model = override
        if model and model not in targets:
            targets.append(model)
    infos = [gpt_windows.window_for(m, auth_mode, base_url) for m in targets] \
        or [gpt_windows.window_for(None, auth_mode, base_url)]
    tightest = min(infos, key=lambda i: i.budget)
    return {
        "models": [i.model for i in infos],
        "backend": tightest.backend,
        "window": tightest.window,
        "budget": tightest.budget,
        "assumed": any(i.assumed for i in infos),
    }
