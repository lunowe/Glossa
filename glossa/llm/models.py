"""Pydantic AI model construction from a Space's ``llm_config``.

All Glossa inference runs through Pydantic AI. A Space selects a provider/model via
``llm_config``; ``build_model`` turns that into a ``pydantic_ai.models.Model`` that
agents run with (``agent.run(..., model=build_model(space, settings))``).

Resolution precedence (see ``glossa.models.space.LLMConfig``):

1. ``provider`` set     -> provider-agnostic registry (openai-compatible, anthropic).
2. ``mode == hosted``   -> anthropic (legacy two-mode config).
3. else (byo/default)   -> openai-compatible endpoint (legacy two-mode config).

New providers slot into ``build_model`` (and, if they fold cache reads into
``input_tokens``, stay out of ``_CACHE_EXCLUDED_FROM_INPUT``).
"""

import os
from typing import TYPE_CHECKING

from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider

from glossa.models.space import LLMMode, Space

if TYPE_CHECKING:
    from pydantic_ai.usage import RunUsage

    from glossa.config import Settings

# Providers whose normalized ``input_tokens`` already EXCLUDE cached tokens
# (Anthropic reports input/cache split). For the OpenAI family cache reads are a
# subset of ``input_tokens``, so ``usage_to_dict`` subtracts them to match
# ``glossa.pricing.compute_cost_usd``, which treats ``input_tokens`` as uncached.
_CACHE_EXCLUDED_FROM_INPUT = {"anthropic"}


def _resolve_api_key(api_key_ref: str | None, settings: "Settings") -> str | None:
    """Resolve an ``api_key_ref`` (``env:VAR`` / literal / None -> settings default)."""
    if api_key_ref is None:
        return settings.default_llm_api_key
    if api_key_ref.startswith("env:"):
        return os.environ.get(api_key_ref[4:])
    return api_key_ref


def resolve_provider(space: Space, settings: "Settings") -> str:
    """Pydantic AI provider name for this space (e.g. ``openai``, ``anthropic``)."""
    cfg = space.llm_config
    if cfg.provider:
        return cfg.provider
    if cfg.mode == LLMMode.HOSTED:
        return "anthropic"
    return settings.default_llm_provider


def resolve_model_name(space: Space, settings: "Settings") -> str:
    """Bare model name used for usage/billing attribution (see ``glossa.pricing``)."""
    cfg = space.llm_config
    if cfg.model:
        return cfg.model
    if cfg.mode == LLMMode.HOSTED:
        return settings.hosted_default_model
    return settings.default_llm_model


def build_model(space: Space, settings: "Settings") -> Model:
    """Construct a Pydantic AI model for the space (no network until first call)."""
    cfg = space.llm_config
    provider = resolve_provider(space, settings)
    model_name = resolve_model_name(space, settings)
    api_key = _resolve_api_key(cfg.api_key_ref, settings)

    if provider == "anthropic":
        key = api_key or settings.hosted_anthropic_api_key
        if not key:
            raise ValueError(
                "Anthropic requires an API key. Set GLOSSA_HOSTED_ANTHROPIC_API_KEY "
                "or llm_config.api_key_ref on the space."
            )
        provider_kwargs: dict = {"api_key": key}
        base_url = cfg.base_url or cfg.endpoint
        if base_url:
            provider_kwargs["base_url"] = base_url
        return AnthropicModel(model_name, provider=AnthropicProvider(**provider_kwargs))

    # OpenAI and any OpenAI-compatible endpoint (the legacy "byo"/default path).
    base_url = cfg.base_url or cfg.endpoint or settings.default_llm_endpoint
    if not base_url and not api_key:
        raise ValueError(
            "OpenAI provider requires an API key or an OpenAI-compatible base_url. "
            "Set llm_config (provider/base_url/api_key_ref) on the space, or "
            "GLOSSA_DEFAULT_LLM_ENDPOINT / GLOSSA_DEFAULT_LLM_API_KEY."
        )
    # OpenAI-compatible servers behind a base_url often ignore the key; send a
    # placeholder so the client constructs cleanly when none is configured.
    provider_kwargs = {"api_key": api_key or "EMPTY"}
    if base_url:
        provider_kwargs["base_url"] = base_url
    return OpenAIChatModel(model_name, provider=OpenAIProvider(**provider_kwargs))


def model_settings_for(space: Space, settings: "Settings", *, temperature: float) -> dict:
    """Per-call ``model_settings`` for an agent run.

    OpenAI family: pass ``temperature``. Anthropic: omit sampling params (thinking
    models reject them), enable adaptive thinking + effort, and cache the (reused)
    system prompts — mirroring the previous hosted driver's cost behavior.
    """
    if resolve_provider(space, settings) == "anthropic":
        ms: dict = {
            "max_tokens": settings.hosted_default_max_tokens,
            "anthropic_cache_instructions": True,
        }
        if settings.hosted_enable_thinking:
            ms["thinking"] = True
            ms["anthropic_effort"] = settings.hosted_default_effort
        return ms
    return {"temperature": temperature}


def usage_to_dict(run_usage: "RunUsage", *, provider: str) -> dict:
    """Map a Pydantic AI ``RunUsage`` to the dict ``record_usage`` normalizes.

    ``glossa.pricing`` bills ``input_tokens`` as the *uncached* portion plus cache
    reads/writes separately. For providers that fold cache reads into
    ``input_tokens`` (OpenAI family) we subtract them so cached tokens are billed
    once, at the cache rate.
    """
    input_tokens = run_usage.input_tokens or 0
    cache_write = getattr(run_usage, "cache_write_tokens", 0) or 0
    cache_read = getattr(run_usage, "cache_read_tokens", 0) or 0
    if provider not in _CACHE_EXCLUDED_FROM_INPUT:
        input_tokens = max(input_tokens - cache_read, 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": run_usage.output_tokens or 0,
        "cache_creation_input_tokens": cache_write,
        "cache_read_input_tokens": cache_read,
    }
