"""Approximate cost calculation, sourced from the model catalogue with the
literal table below as fallback (docs/tickets/007, stage 2).

Rates come first from model_catalogue.current(), which is itself a chain
(live LiteLLM fetch -> disk cache -> vendored snapshot); a model the
catalogue doesn't carry — retired families past their deprecation date, or
any run where the catalogue never loaded — falls back to the MODEL_PRICES
literals, which stay in the file exactly for that (a long-lived local
history may still reference claude-3-opus). PRICING_SOURCE and
PRICING_FETCHED record where the literal table came from and when. Prices
change and estimates are estimates; Anthropic's own billing is the only
authoritative record of what was charged.

Matched by model-id PREFIX rather than exact equality, because a real model
id carries a dated snapshot suffix (e.g. "claude-haiku-4-5-20251001")
neither source enumerates: catalogue ids are undated base ids (LiteLLM's
provider namespaces already stripped by model_catalogue), so
"claude-opus-4-5-20251101" resolves "claude-opus-4-5". The longest matching
prefix wins, so a more specific entry ("claude-opus-4-8") beats a shorter
one that would also match ("claude-opus-4").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import model_catalogue

PRICING_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
PRICING_FETCHED = "2026-09-23"
OPENAI_PRICING_SOURCE = "https://platform.openai.com/docs/pricing"
OPENAI_PRICING_FETCHED = "2026-09-17"

# Anthropic prices prompt-cache traffic as fixed multiples of the base input
# rate (uniform across every model family in the table below). Used only
# when LiteLLM states a model's input/output rates but omits a cache rate.
CACHE_WRITE_5M_INPUT_MULTIPLIER = 1.25
CACHE_WRITE_1H_INPUT_MULTIPLIER = 2.0
CACHE_READ_INPUT_MULTIPLIER = 0.10
ANTHROPIC_CACHE_WRITE_MULTIPLIERS = (CACHE_WRITE_5M_INPUT_MULTIPLIER,
                                     CACHE_WRITE_1H_INPUT_MULTIPLIER)

# OpenAI has no uniform cache-write rule to derive one from, and no 1-hour
# tier at all. Where it charges a write premium the catalogue states the rate
# outright (the gpt-5.6 family and gpt-6-astra: 1.25x input); where it states
# nothing, a cached prefix simply bills as ordinary input on the first call,
# which is 1.0x — not Anthropic's 1.25/2.0, which would invent a surcharge
# OpenAI does not levy on those models. So this pair is only ever the
# unstated-rate fallback. (Moot for Codex traffic today, which reports
# cache_creation_input_tokens=0 in openai_translate, but the rate has to be
# right for any path that does report cache writes.)
#
# The cache-READ multiplier needs no such split: OpenAI's cached-input rate is
# 10% of input across the gpt-5 family, the same CACHE_READ_INPUT_MULTIPLIER
# Anthropic uses.
OPENAI_CACHE_WRITE_MULTIPLIERS = (1.0, 1.0)


@dataclass(frozen=True)
class ModelPrice:
    prefix: str
    input_per_mtok: float
    cache_write_5m_per_mtok: float
    cache_write_1h_per_mtok: float
    cache_read_per_mtok: float
    output_per_mtok: float


# Standard (non-batch) Claude API pricing, per model family. Retired models
# are included since a long-lived local history may still reference them.
MODEL_PRICES: tuple[ModelPrice, ...] = (
    # 5.1 point releases have their own cache-read rate (0.025x input) and
    # must out-match the 5 prefix, or every cache hit is billed at 4x.
    ModelPrice("claude-fable-5-1", 10, 12.50, 20, 0.25, 50),
    ModelPrice("claude-mythos-5-1", 10, 12.50, 20, 0.25, 50),
    ModelPrice("claude-fable-5", 10, 12.50, 20, 1, 50),
    ModelPrice("claude-mythos-5", 10, 12.50, 20, 1, 50),
    # Opus 5.5 is cheaper than Opus 5 and its cache hits are 0.05x input. It
    # must out-match the "claude-opus-5" prefix, which it otherwise extends.
    ModelPrice("claude-opus-5-5", 4, 5, 8, 0.20, 20),
    ModelPrice("claude-opus-5", 5, 6.25, 10, 0.50, 25),
    ModelPrice("claude-opus-4-8", 5, 6.25, 10, 0.50, 25),
    ModelPrice("claude-opus-4-7", 5, 6.25, 10, 0.50, 25),
    ModelPrice("claude-opus-4-6", 5, 6.25, 10, 0.50, 25),
    ModelPrice("claude-opus-4-5", 5, 6.25, 10, 0.50, 25),
    ModelPrice("claude-opus-4-1", 15, 18.75, 30, 1.50, 75),
    ModelPrice("claude-opus-4", 15, 18.75, 30, 1.50, 75),
    ModelPrice("claude-sonnet-5", 2, 2.50, 4, 0.20, 10),
    ModelPrice("claude-sonnet-4-6", 3, 3.75, 6, 0.30, 15),
    ModelPrice("claude-sonnet-4-5", 3, 3.75, 6, 0.30, 15),
    ModelPrice("claude-sonnet-4", 3, 3.75, 6, 0.30, 15),
    ModelPrice("claude-haiku-4-5", 1, 1.25, 2, 0.10, 5),
    ModelPrice("claude-haiku-3-5", 0.80, 1, 1.60, 0.08, 4),
    ModelPrice("claude-3-5-haiku", 0.80, 1, 1.60, 0.08, 4),  # older dot-release id style
    ModelPrice("claude-3-5-sonnet", 3, 3.75, 6, 0.30, 15),
    ModelPrice("claude-3-opus", 15, 18.75, 30, 1.50, 75),

    # OpenAI, for Codex profiles. Same role as the Claude literals above:
    # only reached when the catalogue never loaded at all, since LiteLLM
    # carries every one of these — and kept in step with it, so a fallback run
    # does not quietly cost differently from a normal one. The gpt-5.6 family
    # and gpt-6-astra state a 1.25x cache-write rate; the rest state none, so a
    # write bills as ordinary input. Cache-read is OpenAI's cached-input rate,
    # 10% of input; the `-pro` models do not support prompt caching at all, so
    # theirs is that same derived 10% rather than a published figure.
    ModelPrice("gpt-6-astra", 10, 12.50, 10, 1, 50),
    ModelPrice("gpt-5.6-cyber", 12.50, 15.625, 12.50, 1.25, 75),
    ModelPrice("gpt-5.6-sol", 4, 5, 4, 0.40, 20),
    ModelPrice("gpt-5.6-terra", 2, 2.50, 2, 0.20, 12),
    ModelPrice("gpt-5.6-luna", 0.20, 0.25, 0.20, 0.02, 1.20),
    ModelPrice("gpt-5.6", 4, 5, 4, 0.40, 20),
    ModelPrice("gpt-5.5-pro", 30, 30, 30, 3, 180),
    ModelPrice("gpt-5.5-cyber", 12.50, 12.50, 12.50, 1.25, 75),
    ModelPrice("gpt-5.5", 5, 5, 5, 0.50, 30),
    ModelPrice("gpt-5.4-pro", 30, 30, 30, 3, 180),
    ModelPrice("gpt-5.4-mini", 0.75, 0.75, 0.75, 0.075, 4.50),
    ModelPrice("gpt-5.4-nano", 0.20, 0.20, 0.20, 0.02, 1.25),
    ModelPrice("gpt-5.4", 2.50, 2.50, 2.50, 0.25, 15),
    ModelPrice("gpt-5.3-codex", 1.75, 1.75, 1.75, 0.175, 14),
    ModelPrice("gpt-5.2-pro", 21, 21, 21, 2.10, 168),
    ModelPrice("gpt-5.2", 1.75, 1.75, 1.75, 0.175, 14),
    ModelPrice("gpt-5.1", 1.25, 1.25, 1.25, 0.125, 10),
    ModelPrice("gpt-5-pro", 15, 15, 15, 1.50, 120),
    ModelPrice("gpt-5-mini", 0.25, 0.25, 0.25, 0.025, 2),
    ModelPrice("gpt-5-nano", 0.05, 0.05, 0.05, 0.005, 0.40),
    ModelPrice("gpt-5", 1.25, 1.25, 1.25, 0.125, 10),
)


def _price_from_model_info(info, cache_write_multipliers=None) -> Optional[ModelPrice]:
    """A ModelPrice from a catalogue ModelInfo, or None when LiteLLM didn't
    state both base rates — never a guessed price. Cache rates use
    LiteLLM's own fields when present and a multiple of the input rate
    otherwise; `cache_write_multipliers` is the (5m, 1h) pair to use, which
    differs by vendor — see ANTHROPIC_CACHE_WRITE_MULTIPLIERS and
    OPENAI_CACHE_WRITE_MULTIPLIERS."""
    if not isinstance(info.input_cost, (int, float)) or not isinstance(info.output_cost, (int, float)):
        return None
    write_5m, write_1h = cache_write_multipliers or ANTHROPIC_CACHE_WRITE_MULTIPLIERS
    input_per_mtok = info.input_cost * 1_000_000
    output_per_mtok = info.output_cost * 1_000_000

    def per_mtok(stated: Optional[float], multiplier: float) -> float:
        if isinstance(stated, (int, float)):
            return stated * 1_000_000
        return input_per_mtok * multiplier

    return ModelPrice(
        prefix=model_catalogue.base_id(info.id).lower(),
        input_per_mtok=input_per_mtok,
        cache_write_5m_per_mtok=per_mtok(info.cache_write_cost, write_5m),
        cache_write_1h_per_mtok=per_mtok(info.cache_write_1h_cost, write_1h),
        cache_read_per_mtok=per_mtok(info.cache_read_cost, CACHE_READ_INPUT_MULTIPLIER),
        output_per_mtok=output_per_mtok,
    )


_USE_CURRENT_CATALOGUE = object()  # sentinel: default to model_catalogue.current()


def find_price(model: Optional[str], catalogue=_USE_CURRENT_CATALOGUE) -> Optional[ModelPrice]:
    """Longest-prefix match, catalogue first, MODEL_PRICES literals when
    the catalogue is unavailable or doesn't carry the model at all.
    `catalogue` exists for tests: pass a Catalogue to inject one, or None
    to force the literal fallback."""
    if not model:
        return None
    if catalogue is _USE_CURRENT_CATALOGUE:
        catalogue = model_catalogue.current()
    normalized = model.lower()

    if catalogue is not None:
        best_info = None
        best_len = -1
        # Both lineups, not just Anthropic's. Scanning only `catalogue.anthropic`
        # is why every Codex request was recorded uncosted: `gpt-5.6-sol` is in
        # the catalogue with rates, and this loop never looked at it. The two
        # lineups cannot collide on a prefix (`claude-` vs `gpt-`/`o`), so the
        # longest match still decides.
        for lineup, multipliers in ((catalogue.anthropic, ANTHROPIC_CACHE_WRITE_MULTIPLIERS),
                                    (catalogue.openai, OPENAI_CACHE_WRITE_MULTIPLIERS)):
            for info in lineup:
                prefix = model_catalogue.base_id(info.id).lower()
                if normalized.startswith(prefix) and len(prefix) > best_len:
                    price = _price_from_model_info(info, multipliers)
                    if price is not None:
                        best_info, best_len = price, len(prefix)
        if best_info is not None:
            return best_info

    best: Optional[ModelPrice] = None
    for price in MODEL_PRICES:
        if normalized.startswith(price.prefix) and (best is None or len(price.prefix) > len(best.prefix)):
            best = price
    return best


def estimate_cost_usd(model: Optional[str], usage: Optional[dict]) -> Optional[float]:
    """Returns None when the model isn't recognized, rather than silently
    guessing $0 or some default rate. `usage` is the raw Anthropic usage
    dict: input_tokens, output_tokens, cache_creation_input_tokens,
    cache_read_input_tokens.

    Cache-write tokens are costed at the 5-minute rate because the usage
    payload doesn't report which TTL (5m vs 1h) was used."""
    if not usage:
        return None
    price = find_price(model)
    if price is None:
        return None

    input_tokens = usage.get("input_tokens") or 0
    output_tokens = usage.get("output_tokens") or 0
    cache_write_tokens = usage.get("cache_creation_input_tokens") or 0
    cache_read_tokens = usage.get("cache_read_input_tokens") or 0

    cost = (
        input_tokens / 1_000_000 * price.input_per_mtok
        + output_tokens / 1_000_000 * price.output_per_mtok
        + cache_write_tokens / 1_000_000 * price.cache_write_5m_per_mtok
        + cache_read_tokens / 1_000_000 * price.cache_read_per_mtok
    )
    return round(cost, 6)
