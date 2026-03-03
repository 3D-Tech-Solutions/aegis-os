"""OPA (Open Policy Agent) integration for Policy-as-Code enforcement."""

import httpx
from pydantic import BaseModel

from src.config import settings


class PolicyInput(BaseModel):
    """Input document sent to OPA for evaluation."""

    agent_type: str
    requester_id: str
    action: str
    resource: str
    metadata: dict[str, str] = {}


class PolicyResult(BaseModel):
    """Result returned by OPA policy evaluation."""

    allowed: bool
    reasons: list[str] = []


class PolicyEngine:
    """Evaluates agent actions against OPA policies.

    Policies are written in Rego and served by an OPA server instance.
    The policy path follows the convention: /v1/data/aegis/<policy_name>.
    """

    def __init__(self, opa_url: str | None = None) -> None:
        self._opa_url = opa_url or settings.opa_url

    async def evaluate(self, policy_name: str, input_data: PolicyInput) -> PolicyResult:
        """Evaluate a named OPA policy with the given input.

        Args:
            policy_name: The Rego package/rule path (e.g., ``agent_access``).
            input_data: The structured input to evaluate against the policy.

        Returns:
            A PolicyResult indicating whether the action is allowed.
        """
        url = f"{self._opa_url}/v1/data/aegis/{policy_name}"
        payload = {"input": input_data.model_dump()}

        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            body = response.json()

        result = body.get("result", {})
        allowed: bool = bool(result.get("allow", False))
        reasons: list[str] = result.get("reasons", [])
        return PolicyResult(allowed=allowed, reasons=reasons)

    async def is_allowed(self, policy_name: str, input_data: PolicyInput) -> bool:
        """Convenience method returning only the boolean allow decision."""
        result = await self.evaluate(policy_name, input_data)
        return result.allowed
