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
PRICING_FETCHED = "2026-08-20"

# Anthropic prices prompt-cache traffic as fixed multiples of the base input
# rate (uniform across every model family in the table below). Used only
# when LiteLLM states a model's input/output rates but omits a cache rate.
CACHE_WRITE_5M_INPUT_MULTIPLIER = 1.25
CACHE_WRITE_1H_INPUT_MULTIPLIER = 2.0
CACHE_READ_INPUT_MULTIPLIER = 0.10


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
    ModelPrice("claude-fable-5", 10, 12.50, 20, 1, 50),
    ModelPrice("claude-mythos-5", 10, 12.50, 20, 1, 50),
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
)


def _price_from_model_info(info) -> Optional[ModelPrice]:
    """A ModelPrice from a catalogue ModelInfo, or None when LiteLLM didn't
    state both base rates — never a guessed price. Cache rates use
    LiteLLM's own fields when present and Anthropic's uniform multipliers
    of the input rate otherwise."""
    if not isinstance(info.input_cost, (int, float)) or not isinstance(info.output_cost, (int, float)):
        return None
    input_per_mtok = info.input_cost * 1_000_000
    output_per_mtok = info.output_cost * 1_000_000

    def per_mtok(stated: Optional[float], multiplier: float) -> float:
        if isinstance(stated, (int, float)):
            return stated * 1_000_000
        return input_per_mtok * multiplier

    return ModelPrice(
        prefix=model_catalogue.base_id(info.id).lower(),
        input_per_mtok=input_per_mtok,
        cache_write_5m_per_mtok=per_mtok(info.cache_write_cost, CACHE_WRITE_5M_INPUT_MULTIPLIER),
        cache_write_1h_per_mtok=per_mtok(info.cache_write_1h_cost, CACHE_WRITE_1H_INPUT_MULTIPLIER),
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
        for info in catalogue.anthropic:
            prefix = model_catalogue.base_id(info.id).lower()
            if normalized.startswith(prefix) and len(prefix) > best_len:
                price = _price_from_model_info(info)
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
