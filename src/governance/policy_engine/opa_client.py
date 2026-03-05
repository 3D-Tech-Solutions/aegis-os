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
    """Result returned by OPA policy evaluation.

    Attributes:
        allowed: Whether the request is permitted.
        reasons: Human-readable list of denial reasons (populated when
            ``allowed`` is ``False``).
        action: Orchestrator instruction returned by the Rego policy.
            Possible values:

            * ``"allow"`` – proceed normally (default when ``allowed=True``).
            * ``"mask"`` – re-apply PII scrubbing to the fields listed in
              :attr:`fields` before forwarding to the LLM adapter.
            * ``"reject"`` – deny the request (default when ``allowed=False``).
        fields: Which request fields should be re-masked when
            ``action == "mask"``; typically ``["prompt"]``.
    """

    allowed: bool
    reasons: list[str] = []
    action: str = "allow"
    fields: list[str] = []


class OpaUnavailableError(RuntimeError):
    """Raised when the OPA server is unreachable, times out, or returns a 5xx error.

    The orchestrator must catch this and fail closed (deny the request) rather than
    allowing execution to continue.  Silent failure-open is never acceptable.
    """


class PolicyEngine:
    """Evaluates agent actions against OPA policies.

    Policies are written in Rego and served by an OPA server instance.
    The policy path follows the convention: /v1/data/aegis/<policy_name>.

    Any connectivity failure or server-side 5xx from OPA raises
    :exc:`OpaUnavailableError`.  The caller is responsible for treating this as a
    hard deny (fail-closed).
    """

    def __init__(self, opa_url: str | None = None) -> None:
        self._opa_url = opa_url or settings.opa_url

    async def evaluate(self, policy_name: str, input_data: PolicyInput) -> PolicyResult:
        """Evaluate a named OPA policy with the given input.

        Args:
            policy_name: The Rego package/rule path (e.g., ``agent_access``).
            input_data: The structured input to evaluate against the policy.

        Returns:
            A :class:`PolicyResult` indicating whether the action is allowed.

        Raises:
            OpaUnavailableError: OPA returned a 5xx status code, a connection could
                not be established, or the request timed out.
        """
        url = f"{self._opa_url}/v1/data/aegis/{policy_name}"
        payload = {"input": input_data.model_dump()}

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(url, json=payload)
        except httpx.ConnectError as exc:
            raise OpaUnavailableError(
                f"OPA server unreachable at {self._opa_url!r}: {exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise OpaUnavailableError(
                f"OPA request timed out for policy {policy_name!r}: {exc}"
            ) from exc

        if response.status_code >= 500:
            raise OpaUnavailableError(
                f"OPA server returned HTTP {response.status_code} for policy {policy_name!r}"
            )

        response.raise_for_status()
        body = response.json()

        result = body.get("result", {})
        allowed: bool = bool(result.get("allow", False))
        reasons: list[str] = result.get("reasons", [])
        # ``action`` defaults to "reject" when denied, "allow" when permitted.
        # The Rego policy can override with "mask" to trigger re-scrubbing.
        default_action = "reject" if not allowed else "allow"
        action: str = result.get("action", default_action)
        fields: list[str] = result.get("fields", [])
        return PolicyResult(allowed=allowed, reasons=reasons, action=action, fields=fields)

    async def is_allowed(self, policy_name: str, input_data: PolicyInput) -> bool:
        """Convenience method returning only the boolean allow decision."""
        result = await self.evaluate(policy_name, input_data)
        return result.allowed
