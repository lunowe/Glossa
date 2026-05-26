"""Hosted LLMDriver — Glossa-managed inference via the Anthropic API.

Translates the OpenAI-style LLMMessage list (with inline ``system`` role) into
the Anthropic Messages shape (``system`` as a separate parameter, ``messages``
alternating user/assistant). Defaults to ``claude-opus-4-7`` with adaptive
thinking and ``effort: high``, since wiki maintenance is intelligence-sensitive
merging and synthesis work.

Always uses ``messages.stream`` rather than ``messages.create``: with adaptive
thinking + high effort + non-trivial ``max_tokens`` the SDK refuses the
non-streaming path (`Streaming is required for operations that may take longer
than 10 minutes`). Streaming avoids the idle-connection timeout entirely.

Prompt caching is on by default: the system block is annotated with
``cache_control: {type: "ephemeral"}``. Glossa reuses the same system prompts
(extract, update-page, query-route, query-answer) across hundreds of LLM calls,
so the cache writes pay back fast.
"""

from anthropic import AsyncAnthropic

from glossa.llm.base import LLMDriver, LLMMessage, LLMResponse


class HostedLLMDriver(LLMDriver):
    def __init__(
        self,
        *,
        api_key: str,
        default_model: str = "claude-opus-4-7",
        default_effort: str = "high",
        default_max_tokens: int = 16000,
        enable_thinking: bool = True,
    ):
        if not api_key:
            raise ValueError("HostedLLMDriver requires an Anthropic API key")
        self._client = AsyncAnthropic(api_key=api_key)
        self._default_model = default_model
        self._default_effort = default_effort
        self._default_max_tokens = default_max_tokens
        self._enable_thinking = enable_thinking

    async def chat(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        system_text, user_assistant_messages = _split_system(messages)

        request: dict = {
            "model": model or self._default_model,
            "max_tokens": max_tokens or self._default_max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in user_assistant_messages],
            "output_config": {"effort": self._default_effort},
        }
        if system_text:
            request["system"] = [
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        if self._enable_thinking:
            request["thinking"] = {"type": "adaptive"}

        async with self._client.messages.stream(**request) as stream:
            response = await stream.get_final_message()

        text = "".join(block.text for block in response.content if block.type == "text")
        usage = {
            "input_tokens": getattr(response.usage, "input_tokens", 0),
            "output_tokens": getattr(response.usage, "output_tokens", 0),
            "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        }
        return LLMResponse(content=text, usage=usage)


def _split_system(messages: list[LLMMessage]) -> tuple[str, list[LLMMessage]]:
    """Extract system messages and concatenate them.

    Anthropic accepts a single ``system`` parameter; multiple OpenAI-style
    system messages are joined with a blank line between them.
    """
    system_parts: list[str] = []
    other: list[LLMMessage] = []
    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
        else:
            other.append(m)
    return "\n\n".join(system_parts), other
