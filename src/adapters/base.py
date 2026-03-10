# Copyright 2026 Tim Escolopio / 3D Tech Solutions
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Base adapter interface for LLM provider integrations."""

from abc import ABC, abstractmethod
from collections.abc import Mapping

from pydantic import BaseModel, Field

from src.config import settings
from src.governance.session_mgr import (
    DPoPProofError,
    DPoPReplayError,
    SessionManager,
    TokenBindingError,
    TokenClaims,
)

AegisMetadata = Mapping[str, str]
AegisTokenKey = "aegis_token"
AegisProofKey = "aegis_dpop_proof"
AegisProtectedKey = "aegis_protected"


class AdapterSecurityError(PermissionError):
    """Raised when an outbound adapter call lacks required sender constraints."""


class LLMRequest(BaseModel):
    """Standardized request to an LLM provider."""

    prompt: str
    model: str
    max_tokens: int = 1024
    temperature: float = 0.7
    system_prompt: str = ""
    # Orchestrator-level metadata injected by the governance pipeline.
    # ``aegis_token`` is always present in production; adapters may inspect or
    # forward this field but must never expose it to end users.
    metadata: dict[str, str] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    """Standardized response from an LLM provider."""

    content: str
    tokens_used: int
    model: str
    provider: str
    finish_reason: str = "stop"


class BaseAdapter(ABC):
    """Abstract base class for all LLM provider adapters."""

    def outbound_request_binding(self, request: LLMRequest) -> tuple[str, str] | None:
        """Return ``(method, url)`` for the protected outbound request, if known."""
        return None

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return the name of the LLM provider."""

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Send a completion request to the LLM provider."""


def require_sender_constrained_request(
    request: LLMRequest,
    *,
    session_mgr: SessionManager,
    http_method: str,
    http_url: str,
) -> TokenClaims | None:
    """Validate sender-constrained metadata before an adapter performs I/O."""
    metadata = request.metadata
    protected = (
        metadata.get(AegisProtectedKey) == "true"
        or settings.require_sender_constrained_tokens
    )
    if not protected:
        return None

    token = metadata.get(AegisTokenKey)
    proof = metadata.get(AegisProofKey)
    if not token or not proof:
        raise AdapterSecurityError(
            "Protected outbound LLM requests must include both aegis_token and "
            "aegis_dpop_proof metadata"
        )

    try:
        return session_mgr.validate_sender_constrained_token(
            token,
            proof,
            http_method=http_method,
            http_url=http_url,
        )
    except (DPoPReplayError, DPoPProofError, TokenBindingError) as exc:
        raise AdapterSecurityError(str(exc)) from exc
