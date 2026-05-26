"""Tests for the hosted Anthropic LLM driver.

The Anthropic client is fully mocked — the focus is on verifying that the
OpenAI-style LLMMessage list is correctly translated into the Anthropic
Messages shape (system extracted, cache_control attached, adaptive thinking
configured)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from glossa.llm.base import LLMMessage
from glossa.llm.hosted import HostedLLMDriver, _split_system


def _fake_response(text: str = "ok", **usage):
    """Build a stand-in for an Anthropic Message response."""
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=usage.get("input_tokens", 100),
            output_tokens=usage.get("output_tokens", 50),
            cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
            cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
        ),
    )


class _FakeStream:
    """Async context manager returned by client.messages.stream(...)."""

    def __init__(self, message):
        self._message = message

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get_final_message(self):
        return self._message


def _stream_mock(response):
    """Return a MagicMock whose call returns a _FakeStream wrapping ``response``.

    ``call_args.kwargs`` still records the kwargs passed to messages.stream(...).
    """
    return MagicMock(side_effect=lambda **kwargs: _FakeStream(response))


class TestSplitSystem:
    def test_separates_system_from_messages(self):
        messages = [
            LLMMessage(role="system", content="You are Glossa."),
            LLMMessage(role="user", content="Hello"),
            LLMMessage(role="assistant", content="Hi"),
        ]
        system, rest = _split_system(messages)
        assert system == "You are Glossa."
        assert len(rest) == 2
        assert rest[0].role == "user"

    def test_concatenates_multiple_system_messages(self):
        messages = [
            LLMMessage(role="system", content="Part 1"),
            LLMMessage(role="system", content="Part 2"),
            LLMMessage(role="user", content="Hi"),
        ]
        system, rest = _split_system(messages)
        assert system == "Part 1\n\nPart 2"
        assert len(rest) == 1

    def test_no_system_returns_empty(self):
        messages = [LLMMessage(role="user", content="Hi")]
        system, rest = _split_system(messages)
        assert system == ""
        assert len(rest) == 1


class TestHostedLLMDriver:
    def test_requires_api_key(self):
        with pytest.raises(ValueError, match="API key"):
            HostedLLMDriver(api_key="")

    async def test_chat_extracts_system_and_attaches_cache_control(self):
        driver = HostedLLMDriver(api_key="sk-test")
        mock_stream = _stream_mock(_fake_response("hello"))

        with patch.object(driver._client.messages, "stream", mock_stream):
            await driver.chat(
                [
                    LLMMessage(role="system", content="You are Glossa."),
                    LLMMessage(role="user", content="Hi"),
                ]
            )

        kwargs = mock_stream.call_args.kwargs
        assert kwargs["system"] == [
            {
                "type": "text",
                "text": "You are Glossa.",
                "cache_control": {"type": "ephemeral"},
            }
        ]
        assert kwargs["messages"] == [{"role": "user", "content": "Hi"}]
        assert "system" not in [m["role"] for m in kwargs["messages"]]

    async def test_chat_uses_claude_opus_4_7_by_default(self):
        driver = HostedLLMDriver(api_key="sk-test")
        mock_stream = _stream_mock(_fake_response("ok"))

        with patch.object(driver._client.messages, "stream", mock_stream):
            await driver.chat([LLMMessage(role="user", content="Hi")])

        assert mock_stream.call_args.kwargs["model"] == "claude-opus-4-7"

    async def test_chat_enables_adaptive_thinking_by_default(self):
        driver = HostedLLMDriver(api_key="sk-test", enable_thinking=True)
        mock_stream = _stream_mock(_fake_response("ok"))

        with patch.object(driver._client.messages, "stream", mock_stream):
            await driver.chat([LLMMessage(role="user", content="Hi")])

        kwargs = mock_stream.call_args.kwargs
        assert kwargs["thinking"] == {"type": "adaptive"}
        assert kwargs["output_config"] == {"effort": "high"}

    async def test_chat_can_disable_thinking(self):
        driver = HostedLLMDriver(api_key="sk-test", enable_thinking=False)
        mock_stream = _stream_mock(_fake_response("ok"))

        with patch.object(driver._client.messages, "stream", mock_stream):
            await driver.chat([LLMMessage(role="user", content="Hi")])

        assert "thinking" not in mock_stream.call_args.kwargs

    async def test_chat_respects_model_override(self):
        driver = HostedLLMDriver(api_key="sk-test")
        mock_stream = _stream_mock(_fake_response("ok"))

        with patch.object(driver._client.messages, "stream", mock_stream):
            await driver.chat(
                [LLMMessage(role="user", content="Hi")],
                model="claude-sonnet-4-6",
            )

        assert mock_stream.call_args.kwargs["model"] == "claude-sonnet-4-6"

    async def test_chat_does_not_send_sampling_params(self):
        """Opus 4.7 returns 400 on temperature/top_p/top_k; we must not pass them."""
        driver = HostedLLMDriver(api_key="sk-test")
        mock_stream = _stream_mock(_fake_response("ok"))

        with patch.object(driver._client.messages, "stream", mock_stream):
            await driver.chat(
                [LLMMessage(role="user", content="Hi")],
                temperature=0.7,
            )

        kwargs = mock_stream.call_args.kwargs
        assert "temperature" not in kwargs
        assert "top_p" not in kwargs
        assert "top_k" not in kwargs

    async def test_chat_returns_text_and_usage(self):
        driver = HostedLLMDriver(api_key="sk-test")
        mock_stream = _stream_mock(
            _fake_response(
                "Allianz answer.",
                input_tokens=500,
                output_tokens=120,
                cache_creation_input_tokens=400,
                cache_read_input_tokens=100,
            )
        )

        with patch.object(driver._client.messages, "stream", mock_stream):
            response = await driver.chat(
                [
                    LLMMessage(role="system", content="You are Glossa."),
                    LLMMessage(role="user", content="Tell me about Allianz."),
                ]
            )

        assert response.content == "Allianz answer."
        assert response.usage == {
            "input_tokens": 500,
            "output_tokens": 120,
            "cache_creation_input_tokens": 400,
            "cache_read_input_tokens": 100,
        }

    async def test_chat_skips_thinking_blocks_in_output(self):
        """Thinking blocks should not appear in the returned text."""
        driver = HostedLLMDriver(api_key="sk-test")
        response_with_thinking = SimpleNamespace(
            content=[
                SimpleNamespace(type="thinking", thinking="Internal reasoning..."),
                SimpleNamespace(type="text", text="Final answer."),
            ],
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=50,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )
        mock_stream = _stream_mock(response_with_thinking)

        with patch.object(driver._client.messages, "stream", mock_stream):
            response = await driver.chat([LLMMessage(role="user", content="Hi")])

        assert response.content == "Final answer."

    async def test_chat_respects_max_tokens_override(self):
        driver = HostedLLMDriver(api_key="sk-test", default_max_tokens=16000)
        mock_stream = _stream_mock(_fake_response("ok"))

        with patch.object(driver._client.messages, "stream", mock_stream):
            await driver.chat(
                [LLMMessage(role="user", content="Hi")],
                max_tokens=64000,
            )

        assert mock_stream.call_args.kwargs["max_tokens"] == 64000


class TestHostedDriverViaFactory:
    def test_factory_builds_hosted_driver_from_settings(self):
        from datetime import UTC, datetime

        from glossa.config import Settings
        from glossa.llm.factory import build_driver
        from glossa.models.space import LLMConfig, LLMMode, Space, SpaceStats

        # Construct Settings explicitly to bypass any local .env file that
        # would otherwise pollute the test.
        settings = Settings(
            _env_file=None,
            hosted_anthropic_api_key="sk-from-env",
            hosted_default_model="claude-opus-4-7",
        )

        now = datetime.now(UTC)
        space = Space(
            id="gls_x",
            tenant_id="t1",
            name="Hosted Space",
            slug="hosted-space",
            bucket_uri="mem://gls_x/",
            llm_config=LLMConfig(mode=LLMMode.HOSTED),
            stats=SpaceStats(),
            created_at=now,
            updated_at=now,
        )
        driver = build_driver(space, settings)
        assert isinstance(driver, HostedLLMDriver)
        assert driver._default_model == "claude-opus-4-7"

    def test_factory_errors_when_hosted_api_key_missing(self):
        from datetime import UTC, datetime

        from glossa.config import Settings
        from glossa.llm.factory import build_driver
        from glossa.models.space import LLMConfig, LLMMode, Space, SpaceStats

        settings = Settings(_env_file=None, hosted_anthropic_api_key=None)

        now = datetime.now(UTC)
        space = Space(
            id="gls_x",
            tenant_id="t1",
            name="Hosted Space",
            slug="hosted-space",
            bucket_uri="mem://gls_x/",
            llm_config=LLMConfig(mode=LLMMode.HOSTED),
            stats=SpaceStats(),
            created_at=now,
            updated_at=now,
        )
        with pytest.raises(ValueError, match="Anthropic API key"):
            build_driver(space, settings)
