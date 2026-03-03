"""Local Llama adapter for Aegis-OS MCP (Model Context Protocol) integration.

Communicates with a locally running Ollama or llama.cpp server that exposes
an OpenAI-compatible ``/v1/chat/completions`` endpoint.
"""

import httpx

from src.adapters.base import BaseAdapter, LLMRequest, LLMResponse


class LocalLlamaAdapter(BaseAdapter):
    """Adapter for a locally-hosted Llama model served via Ollama or llama.cpp.

    The local server must expose an OpenAI-compatible API at ``base_url``.
    No API key is required for local deployments.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434/v1",
        default_model: str = "llama3",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model

    @property
    def provider_name(self) -> str:
        return "local_llama"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Send a chat completion request to the local Llama server."""
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

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
            )
            response.raise_for_status()
            body = response.json()

        choice = body["choices"][0]
        usage = body.get("usage", {})
        tokens_used = usage.get("total_tokens", 0)

        return LLMResponse(
            content=choice["message"]["content"],
            tokens_used=tokens_used,
            model=model,
            provider=self.provider_name,
            finish_reason=choice.get("finish_reason", "stop"),
        )
