"""Tests for the Pydantic AI model layer (glossa/llm/models.py)."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel

from glossa.config import Settings
from glossa.llm import (
    build_model,
    model_settings_for,
    resolve_model_name,
    resolve_provider,
    usage_to_dict,
)
from glossa.models.space import LLMConfig, Space, SpaceStats


def _space(cfg: LLMConfig) -> Space:
    now = datetime.now(UTC)
    return Space(
        id="gls_x",
        tenant_id="t1",
        name="S",
        slug="s",
        bucket_uri="mem://gls_x/",
        llm_config=cfg,
        stats=SpaceStats(),
        created_at=now,
        updated_at=now,
    )


def _settings(**kw) -> Settings:
    base = dict(
        _env_file=None,
        openai_base_url="http://local/v1",
        openai_api_key="k",
        anthropic_api_key="sk-ant",
    )
    base.update(kw)
    return Settings(**base)


# --- build_model resolution ------------------------------------------------


def test_default_provider_builds_openai_compatible():
    model = build_model(_space(LLMConfig()), _settings())
    assert isinstance(model, OpenAIChatModel)


def test_default_provider_can_build_anthropic():
    model = build_model(
        _space(LLMConfig()),
        _settings(default_llm_provider="anthropic", default_llm_model="claude-opus-4-7"),
    )
    assert isinstance(model, AnthropicModel)


def test_provider_anthropic_builds_anthropic():
    model = build_model(
        _space(LLMConfig(provider="anthropic", model="claude-sonnet-4-6", api_key_ref="sk-x")), _settings()
    )
    assert isinstance(model, AnthropicModel)


def test_provider_openai_with_base_url_builds_openai():
    model = build_model(_space(LLMConfig(provider="openai", base_url="http://x/v1", model="gpt-4o")), _settings())
    assert isinstance(model, OpenAIChatModel)


def test_anthropic_without_key_raises():
    with pytest.raises(ValueError, match="Anthropic requires an API key"):
        build_model(_space(LLMConfig(provider="anthropic")), _settings(anthropic_api_key=None))


def test_openai_without_key_or_base_url_raises():
    with pytest.raises(ValueError, match="OpenAI requires"):
        build_model(
            _space(LLMConfig(provider="openai")),
            _settings(openai_base_url=None, openai_api_key=None),
        )


def test_gemini_without_key_raises():
    # Validated before the (lazily imported) google SDK is touched, so this passes
    # whether or not google-genai is installed.
    with pytest.raises(ValueError, match="Gemini requires an API key"):
        build_model(_space(LLMConfig(provider="gemini")), _settings(gemini_api_key=None))


def test_bedrock_without_region_raises():
    # Validated before the (lazily imported) boto3 SDK is touched.
    with pytest.raises(ValueError, match="Bedrock requires an AWS region"):
        build_model(_space(LLMConfig(provider="bedrock")), _settings(aws_region=None))


def test_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        build_model(_space(LLMConfig(provider="does-not-exist")), _settings())


# --- resolution helpers ----------------------------------------------------


def test_resolve_provider_precedence():
    s = _settings()
    assert resolve_provider(_space(LLMConfig(provider="groq")), s) == "groq"
    assert resolve_provider(_space(LLMConfig()), s) == "openai"


def test_resolve_model_name_precedence():
    s = _settings(default_llm_model="gpt-4o-mini")
    assert resolve_model_name(_space(LLMConfig(model="custom")), s) == "custom"
    assert resolve_model_name(_space(LLMConfig()), s) == "gpt-4o-mini"


# --- model settings --------------------------------------------------------


def test_model_settings_openai_passes_temperature():
    ms = model_settings_for(_space(LLMConfig()), _settings(), temperature=0.3)
    assert ms == {"temperature": 0.3}


def test_model_settings_anthropic_omits_temperature_enables_thinking_and_cache():
    ms = model_settings_for(_space(LLMConfig(provider="anthropic")), _settings(), temperature=0.3)
    assert "temperature" not in ms
    assert ms["anthropic_cache_instructions"] is True
    assert ms["thinking"] is True
    assert ms["anthropic_effort"] == "high"


# --- usage mapping ---------------------------------------------------------


def _usage(input_tokens, output_tokens, cache_write=0, cache_read=0):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_write_tokens=cache_write,
        cache_read_tokens=cache_read,
    )


def test_usage_openai_subtracts_cache_reads_from_input():
    # OpenAI folds cache reads into input_tokens; we bill them once (at cache rate).
    d = usage_to_dict(_usage(1000, 50, cache_read=200), provider="openai")
    assert d == {
        "input_tokens": 800,
        "output_tokens": 50,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 200,
    }


def test_usage_anthropic_keeps_input_uncached():
    # Anthropic already reports input_tokens as uncached; don't subtract.
    d = usage_to_dict(_usage(1000, 50, cache_write=400, cache_read=200), provider="anthropic")
    assert d == {
        "input_tokens": 1000,
        "output_tokens": 50,
        "cache_creation_input_tokens": 400,
        "cache_read_input_tokens": 200,
    }
