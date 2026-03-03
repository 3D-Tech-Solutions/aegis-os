"""Anthropic adapter for Aegis-OS MCP (Model Context Protocol) integration."""

import httpx

from src.adapters.base import BaseAdapter, LLMRequest, LLMResponse


class AnthropicAdapter(BaseAdapter):
    """Adapter for the Anthropic Messages API.

    Requires an ``api_key`` passed at construction time.  Never store API keys
    in source code - retrieve them from HashiCorp Vault at runtime.
    """

    BASE_URL = "https://api.anthropic.com/v1"
    ANTHROPIC_VERSION = "2023-06-01"

    def __init__(self, api_key: str, default_model: str = "claude-3-5-haiku-20241022") -> None:
        self._api_key = api_key
        self._default_model = default_model

    @property
    def provider_name(self) -> str:
        return "anthropic"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Send a messages request to Anthropic."""
        model = request.model or self._default_model
        payload: dict = {
            "model": model,
            "max_tokens": request.max_tokens,
            "messages": [{"role": "user", "content": request.prompt}],
        }
        if request.system_prompt:
            payload["system"] = request.system_prompt

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.BASE_URL}/messages",
                json=payload,
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": self.ANTHROPIC_VERSION,
                },
            )
            response.raise_for_status()
            body = response.json()

        content_block = body["content"][0]
        usage = body.get("usage", {})
        tokens_used = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)

        return LLMResponse(
            content=content_block["text"],
            tokens_used=tokens_used,
            model=model,
            provider=self.provider_name,
            finish_reason=body.get("stop_reason", "stop"),
        )
