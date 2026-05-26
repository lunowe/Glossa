from collections.abc import Callable

from glossa.llm.base import LLMDriver, LLMMessage, LLMResponse


class FakeLLMDriver(LLMDriver):
    """LLM stub for tests.

    Construct with either a list of response strings (served in order) or a
    callable ``(messages) -> response_text`` for more dynamic behaviour.
    """

    def __init__(self, responses: list[str] | Callable[[list[LLMMessage]], str]):
        self._responses = responses
        self._idx = 0
        self.calls: list[list[LLMMessage]] = []

    async def chat(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        self.calls.append(messages)
        if callable(self._responses):
            content = self._responses(messages)
        else:
            content = self._responses[self._idx]
            self._idx += 1
        return LLMResponse(content=content)
