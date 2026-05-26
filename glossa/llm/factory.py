import os
from typing import TYPE_CHECKING

from glossa.llm.base import LLMDriver
from glossa.llm.byo import BYOLLMDriver
from glossa.llm.hosted import HostedLLMDriver
from glossa.models.space import LLMMode, Space

if TYPE_CHECKING:
    from glossa.config import Settings


def _resolve_api_key(api_key_ref: str | None, settings: "Settings") -> str | None:
    """Resolve an api_key_ref to an actual key.

    Forms:
      - ``env:VAR_NAME`` — read from environment
      - any other non-empty string — used as the literal key
      - None — fall back to settings.default_llm_api_key
    """
    if api_key_ref is None:
        return settings.default_llm_api_key
    if api_key_ref.startswith("env:"):
        return os.environ.get(api_key_ref[4:])
    return api_key_ref


def build_driver(space: Space, settings: "Settings") -> LLMDriver:
    cfg = space.llm_config
    if cfg.mode == LLMMode.HOSTED:
        api_key = _resolve_api_key(cfg.api_key_ref, settings) or settings.hosted_anthropic_api_key
        if not api_key:
            raise ValueError(
                "Hosted mode requires an Anthropic API key. Set "
                "GLOSSA_HOSTED_ANTHROPIC_API_KEY or llm_config.api_key_ref on the space."
            )
        return HostedLLMDriver(
            api_key=api_key,
            default_model=cfg.model or settings.hosted_default_model,
            default_effort=settings.hosted_default_effort,
            default_max_tokens=settings.hosted_default_max_tokens,
            enable_thinking=settings.hosted_enable_thinking,
        )

    endpoint = cfg.endpoint or settings.default_llm_endpoint
    if not endpoint:
        raise ValueError(
            "No LLM endpoint configured. Set llm_config.endpoint on the space "
            "or GLOSSA_DEFAULT_LLM_ENDPOINT in the environment."
        )
    api_key = _resolve_api_key(cfg.api_key_ref, settings)
    return BYOLLMDriver(
        endpoint=endpoint,
        api_key=api_key,
        default_model=cfg.model or settings.default_llm_model,
    )
