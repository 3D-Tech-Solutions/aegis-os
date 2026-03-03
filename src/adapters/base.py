"""Base adapter interface for LLM provider integrations."""

from abc import ABC, abstractmethod

from pydantic import BaseModel


class LLMRequest(BaseModel):
    """Standardized request to an LLM provider."""

    prompt: str
    model: str
    max_tokens: int = 1024
    temperature: float = 0.7
    system_prompt: str = ""


class LLMResponse(BaseModel):
    """Standardized response from an LLM provider."""

    content: str
    tokens_used: int
    model: str
    provider: str
    finish_reason: str = "stop"


class BaseAdapter(ABC):
    """Abstract base class for all LLM provider adapters."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return the name of the LLM provider."""

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Send a completion request to the LLM provider."""
