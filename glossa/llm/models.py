"""Pydantic AI model construction from a Space's ``llm_config``.

All Glossa inference runs through Pydantic AI. A Space selects a provider/model via
``llm_config``; ``build_model`` turns that into a ``pydantic_ai.models.Model`` that
agents run with (``agent.run(..., model=build_model(space, settings))``).

Five providers are supported, each keyed by its own ``GLOSSA_*`` setting:

  provider   model class           default auth setting(s)
  ---------  -------------------   ------------------------------------------
  anthropic  AnthropicModel        anthropic_api_key
  openai     OpenAIChatModel       openai_api_key (+ openai_base_url)
  gemini     GoogleModel (GLA)     gemini_api_key
  bedrock    BedrockConverseModel  aws_* / bedrock_api_key (+ aws_region)
  vertex     GoogleModel (Vertex)  vertex_project / vertex_location / sa file

Resolution precedence (see ``glossa.models.space.LLMConfig``):

1. ``llm_config.provider`` set -> that provider.
2. else -> ``settings.default_llm_provider``.

A Space's ``api_key_ref`` ("env:VAR" or a literal) overrides the per-provider key
when set. The ``google`` and ``bedrock`` SDKs are imported lazily so Glossa (and
the test suite) runs with only ``pydantic-ai-slim[openai,anthropic]`` installed.
"""

import os
from typing import TYPE_CHECKING

from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider

from glossa.models.space import Space

if TYPE_CHECKING:
    from pydantic_ai.usage import RunUsage

    from glossa.config import Settings

SUPPORTED_PROVIDERS = ("anthropic", "openai", "gemini", "bedrock", "vertex")

# Per-provider settings attribute holding the default API key. Bedrock and Vertex
# authenticate differently (AWS credentials / GCP project) and are handled inline.
_PROVIDER_KEY_SETTING = {
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
    "gemini": "gemini_api_key",
}

# Providers whose normalized ``input_tokens`` already EXCLUDE cached tokens
# (Anthropic reports an input/cache split). Every other provider folds cache reads
# into ``input_tokens`` (OpenAI, Gemini, Vertex, and OpenAI/Gemini-on-Bedrock), so
# ``usage_to_dict`` subtracts them to match ``glossa.pricing.compute_cost_usd``,
# which treats ``input_tokens`` as the uncached portion.
_CACHE_EXCLUDED_FROM_INPUT = {"anthropic"}


def _resolve_api_key(space: Space, settings: "Settings", provider: str) -> str | None:
    """Resolve the API key for ``provider``.

    Precedence: the Space's ``api_key_ref`` ("env:VAR" / literal) when set, else the
    per-provider ``GLOSSA_*`` key setting. Returns None when neither is configured.
    """
    ref = space.llm_config.api_key_ref
    if ref:
        return os.environ.get(ref[4:]) if ref.startswith("env:") else ref
    attr = _PROVIDER_KEY_SETTING.get(provider)
    return getattr(settings, attr) if attr else None


def resolve_provider(space: Space, settings: "Settings") -> str:
    """Provider name for this space (one of ``SUPPORTED_PROVIDERS``)."""
    return space.llm_config.provider or settings.default_llm_provider


def resolve_model_name(space: Space, settings: "Settings") -> str:
    """Bare model name used for usage/billing attribution (see ``glossa.pricing``)."""
    return space.llm_config.model or settings.default_llm_model


def build_model(space: Space, settings: "Settings") -> Model:
    """Construct a Pydantic AI model for the space (no network until first call)."""
    provider = resolve_provider(space, settings)
    model_name = resolve_model_name(space, settings)
    builder = _BUILDERS.get(provider)
    if builder is None:
        raise ValueError(f"Unknown LLM provider {provider!r}. Supported: {', '.join(SUPPORTED_PROVIDERS)}.")
    return builder(space, settings, model_name)


def _build_anthropic(space: Space, settings: "Settings", model_name: str) -> Model:
    key = _resolve_api_key(space, settings, "anthropic")
    if not key:
        raise ValueError(
            "Anthropic requires an API key. Set GLOSSA_ANTHROPIC_API_KEY or llm_config.api_key_ref on the space."
        )
    provider_kwargs: dict = {"api_key": key}
    if space.llm_config.base_url:
        provider_kwargs["base_url"] = space.llm_config.base_url
    return AnthropicModel(model_name, provider=AnthropicProvider(**provider_kwargs))


def _build_openai(space: Space, settings: "Settings", model_name: str) -> Model:
    key = _resolve_api_key(space, settings, "openai")
    base_url = space.llm_config.base_url or settings.openai_base_url
    if not key and not base_url:
        raise ValueError(
            "OpenAI requires an API key or an OpenAI-compatible base URL. "
            "Set GLOSSA_OPENAI_API_KEY / GLOSSA_OPENAI_BASE_URL, or llm_config on the space."
        )
    # OpenAI-compatible servers behind a base_url often ignore the key; send a
    # placeholder so the client constructs cleanly when none is configured.
    provider_kwargs: dict = {"api_key": key or "EMPTY"}
    if base_url:
        provider_kwargs["base_url"] = base_url
    return OpenAIChatModel(model_name, provider=OpenAIProvider(**provider_kwargs))


def _build_gemini(space: Space, settings: "Settings", model_name: str) -> Model:
    key = _resolve_api_key(space, settings, "gemini")
    if not key:
        raise ValueError(
            "Gemini requires an API key. Set GLOSSA_GEMINI_API_KEY or llm_config.api_key_ref on the space."
        )
    from pydantic_ai.models.google import GoogleModel
    from pydantic_ai.providers.google import GoogleProvider

    return GoogleModel(model_name, provider=GoogleProvider(api_key=key))


def _build_bedrock(space: Space, settings: "Settings", model_name: str) -> Model:
    region = space.llm_config.extra.get("region") or settings.aws_region
    if not region:
        raise ValueError(
            "Bedrock requires an AWS region. Set GLOSSA_AWS_REGION or llm_config.extra.region on the space."
        )
    provider_kwargs: dict = {"region_name": region}
    bearer = _resolve_api_key(space, settings, "bedrock") or settings.bedrock_api_key
    if bearer:
        provider_kwargs["api_key"] = bearer
    elif settings.aws_access_key_id and settings.aws_secret_access_key:
        provider_kwargs["aws_access_key_id"] = settings.aws_access_key_id
        provider_kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
        if settings.aws_session_token:
            provider_kwargs["aws_session_token"] = settings.aws_session_token
    # else: fall back to the host's default AWS credential chain (env, ~/.aws, IAM role).
    from pydantic_ai.models.bedrock import BedrockConverseModel
    from pydantic_ai.providers.bedrock import BedrockProvider

    return BedrockConverseModel(model_name, provider=BedrockProvider(**provider_kwargs))


def _build_vertex(space: Space, settings: "Settings", model_name: str) -> Model:
    project = space.llm_config.extra.get("project") or settings.vertex_project
    location = space.llm_config.extra.get("location") or settings.vertex_location
    from pydantic_ai.models.google import GoogleModel
    from pydantic_ai.providers.google_cloud import GoogleCloudProvider

    provider_kwargs: dict = {}
    if project:
        provider_kwargs["project"] = project
    if location:
        provider_kwargs["location"] = location
    if settings.vertex_service_account_file:
        from google.oauth2 import service_account

        provider_kwargs["credentials"] = service_account.Credentials.from_service_account_file(
            settings.vertex_service_account_file,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
    # With no project/location/credentials, GoogleCloudProvider falls back to
    # Application Default Credentials (GOOGLE_CLOUD_PROJECT / ADC).
    return GoogleModel(model_name, provider=GoogleCloudProvider(**provider_kwargs))


_BUILDERS = {
    "anthropic": _build_anthropic,
    "openai": _build_openai,
    "gemini": _build_gemini,
    "bedrock": _build_bedrock,
    "vertex": _build_vertex,
}


def model_settings_for(space: Space, settings: "Settings", *, temperature: float) -> dict:
    """Per-call ``model_settings`` for an agent run.

    Anthropic: omit sampling params (thinking models reject them), enable adaptive
    thinking + effort, and cache the reused system prompts. Every other provider
    (OpenAI, Gemini, Bedrock, Vertex): pass ``temperature``.
    """
    if resolve_provider(space, settings) == "anthropic":
        ms: dict = {
            "max_tokens": settings.anthropic_max_tokens,
            "anthropic_cache_instructions": True,
        }
        if settings.anthropic_enable_thinking:
            ms["thinking"] = True
            ms["anthropic_effort"] = settings.anthropic_effort
        return ms
    return {"temperature": temperature}


def usage_to_dict(run_usage: "RunUsage", *, provider: str) -> dict:
    """Map a Pydantic AI ``RunUsage`` to the dict ``record_usage`` normalizes.

    ``glossa.pricing`` bills ``input_tokens`` as the *uncached* portion plus cache
    reads/writes separately. For providers that fold cache reads into
    ``input_tokens`` (everything except Anthropic) we subtract them so cached tokens
    are billed once, at the cache rate.
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
