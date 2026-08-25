"""Approximate cost calculation from published Anthropic API pricing
(standard, non-batch rates). PRICING_SOURCE and PRICING_FETCHED below record
where the table came from and when. Prices change and this table can go
stale, so any cost figure shown must be labeled an estimate; Anthropic's own
billing is the only authoritative record of what was charged.

Matched by model-id PREFIX rather than exact equality, because a real model
id carries a dated snapshot suffix (e.g. "claude-haiku-4-5-20251001") this
table doesn't enumerate. The longest matching prefix wins, so a more specific
entry ("claude-opus-4-8") beats a shorter one that would also match
("claude-opus-4").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

PRICING_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
PRICING_FETCHED = "2026-08-20"


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


def find_price(model: Optional[str]) -> Optional[ModelPrice]:
    if not model:
        return None
    normalized = model.lower()
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
