from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel


class LLMMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class LLMResponse(BaseModel):
    content: str
    usage: dict = {}


class LLMDriver(ABC):
    """Abstract LLM call surface. Implementations wrap a specific provider
    (OpenAI, Anthropic, local router, etc.) so the ingest workflow stays
    provider-agnostic."""

    @abstractmethod
    async def chat(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...
