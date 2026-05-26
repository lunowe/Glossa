import httpx

from glossa.llm.base import LLMDriver, LLMMessage, LLMResponse


class BYOLLMDriver(LLMDriver):
    """Tenant-provided OpenAI-compatible endpoint.

    The tenant supplies the URL, model, and API key on their Space's
    ``llm_config``. Glossa makes no inference calls of its own.
    """

    def __init__(self, endpoint: str, api_key: str | None, default_model: str = "gpt-4o-mini"):
        if not endpoint:
            raise ValueError("BYOLLMDriver requires an endpoint")
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._default_model = default_model

    async def chat(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body: dict = {
            "model": model or self._default_model,
            "messages": [m.model_dump() for m in messages],
            "temperature": temperature,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{self._endpoint}/chat/completions", headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
        return LLMResponse(
            content=data["choices"][0]["message"]["content"],
            usage=data.get("usage", {}),
        )
