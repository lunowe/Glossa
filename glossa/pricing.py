"""Per-model price table and cost calculation.

Prices are in **USD per 1M tokens**. Cache write tokens are billed at the
provider's premium (Anthropic: 1.25x input for 5min TTL); cache reads at the
discount (Anthropic: 0.1x input). The table reflects published list prices as
of model launch; override per-tenant via ``model_overrides`` on the Plan when
that ships.

Adding a new model: drop an entry in ``MODEL_PRICES``. Unknown models fall
back to ``UNKNOWN_MODEL_PRICE`` (zero cost, with a logger warning) so a typo
never silently overcharges or undercharges.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1M tokens, broken out by usage kind."""

    input_per_million: float
    output_per_million: float
    cache_write_per_million: float
    cache_read_per_million: float


# Anthropic pricing (1.25x input for 5min cache write, 0.1x for cache read).
# Source: https://www.anthropic.com/pricing — keep this in sync on model launches.
ANTHROPIC = {
    "claude-opus-4-7": ModelPrice(5.00, 25.00, 6.25, 0.50),
    "claude-opus-4-6": ModelPrice(5.00, 25.00, 6.25, 0.50),
    "claude-opus-4-5": ModelPrice(5.00, 25.00, 6.25, 0.50),
    "claude-opus-4-1": ModelPrice(15.00, 75.00, 18.75, 1.50),
    "claude-opus-4-0": ModelPrice(15.00, 75.00, 18.75, 1.50),
    "claude-sonnet-4-6": ModelPrice(3.00, 15.00, 3.75, 0.30),
    "claude-sonnet-4-5": ModelPrice(3.00, 15.00, 3.75, 0.30),
    "claude-haiku-4-5": ModelPrice(1.00, 5.00, 1.25, 0.10),
}

# OpenAI list prices for common OpenAI/Pydantic AI targets. OpenAI prompt-caching is
# free reads / no separate write tier; we approximate cache_read by the
# documented 50% discount and treat cache_write as the same as input.
OPENAI = {
    "gpt-4o-mini": ModelPrice(0.15, 0.60, 0.15, 0.075),
    "gpt-4o": ModelPrice(2.50, 10.00, 2.50, 1.25),
    "gpt-4.1": ModelPrice(2.00, 8.00, 2.00, 0.50),
    "gpt-4.1-mini": ModelPrice(0.40, 1.60, 0.40, 0.10),
}

# Google Gemini list prices (Gemini Developer API; Vertex bills the same per-token
# rates). Cache writes ≈ input; cache reads at Google's ~75% context-cache discount.
# Source: https://ai.google.dev/gemini-api/docs/pricing — keep in sync on launches.
GEMINI = {
    "gemini-2.5-pro": ModelPrice(1.25, 10.00, 1.25, 0.31),
    "gemini-2.5-flash": ModelPrice(0.30, 2.50, 0.30, 0.075),
    "gemini-2.5-flash-lite": ModelPrice(0.10, 0.40, 0.10, 0.025),
    "gemini-2.0-flash": ModelPrice(0.10, 0.40, 0.10, 0.025),
}

MODEL_PRICES: dict[str, ModelPrice] = {**ANTHROPIC, **OPENAI, **GEMINI}

UNKNOWN_MODEL_PRICE = ModelPrice(0.0, 0.0, 0.0, 0.0)


def get_price(model: str) -> ModelPrice:
    """Look up the price for a model. Unknown models cost 0 and emit a warning."""
    price = MODEL_PRICES.get(model)
    if price is None:
        logger.warning("No price entry for model %r — billing as $0. Add it to glossa.pricing.MODEL_PRICES.", model)
        return UNKNOWN_MODEL_PRICE
    return price


def compute_cost_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> float:
    """Compute the USD cost for a single LLM call.

    ``input_tokens`` is the count of *uncached* input tokens — the Anthropic
    response splits these out (input_tokens = uncached, cache_* = cached). For
    OpenAI-style usage dicts where cache splits are unavailable, the caller
    should pass cache_* as zero and the whole input as input_tokens; the
    pricing math degrades correctly.
    """
    price = get_price(model)
    return round(
        (input_tokens * price.input_per_million / 1_000_000)
        + (output_tokens * price.output_per_million / 1_000_000)
        + (cache_creation_input_tokens * price.cache_write_per_million / 1_000_000)
        + (cache_read_input_tokens * price.cache_read_per_million / 1_000_000),
        6,
    )
