"""OpenAI adapter for Aegis-OS MCP (Model Context Protocol) integration."""

import httpx

from src.adapters.base import BaseAdapter, LLMRequest, LLMResponse


class OpenAIAdapter(BaseAdapter):
    """Adapter for the OpenAI Chat Completions API.

    Requires an ``api_key`` passed at construction time.  Never store API keys
    in source code - retrieve them from HashiCorp Vault at runtime.
    """

    BASE_URL = "https://api.openai.com/v1"

    def __init__(self, api_key: str, default_model: str = "gpt-4o-mini") -> None:
        self._api_key = api_key
        self._default_model = default_model

    @property
    def provider_name(self) -> str:
        return "openai"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Send a chat completion request to OpenAI."""
        model = request.model or self._default_model
        messages = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.prompt})

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.BASE_URL}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            response.raise_for_status()
            body = response.json()

        choice = body["choices"][0]
        return LLMResponse(
            content=choice["message"]["content"],
            tokens_used=body["usage"]["total_tokens"],
            model=model,
            provider=self.provider_name,
            finish_reason=choice.get("finish_reason", "stop"),
        )
