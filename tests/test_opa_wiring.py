"""S1-1 — OPAClient wired into the orchestrator.

Test matrix (roadmap item S1-1):

  Unit — allow path
      Mock PolicyEngine.evaluate() → PolicyResult(allowed=True); assert orchestrator
      proceeds to the LLM adapter stage.

  Unit — deny path
      Mock PolicyEngine.evaluate() → PolicyResult(allowed=False, reasons=[...]); assert
      PolicyDeniedError is raised, the LLM adapter is NOT called, and the reasons list
      is forwarded to the AuditLogger.

  Integration — live OPA (testcontainers)
      Start an OPA container; load policies/agent_access.rego; test all five registered
      agent types for allow and an unregistered type plus an expired-token scenario for
      deny.

  Negative — OPA unavailable (fail-closed)
      Simulate OpaUnavailableError from PolicyEngine; assert PolicyDeniedError is raised
      (never allowed through) and an opa_unavailable audit event is emitted.
"""

from __future__ import annotations

import time
import unittest.mock as mock
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.adapters.base import BaseAdapter, LLMRequest, LLMResponse
from src.audit_vault.logger import AuditLogger
from src.control_plane.orchestrator import (
    Orchestrator,
    OrchestratorRequest,
    PolicyDeniedError,
)
from src.governance.guardrails import Guardrails, MaskResult
from src.governance.policy_engine.opa_client import (
    OpaUnavailableError,
    PolicyEngine,
    PolicyInput,
    PolicyResult,
)
from src.governance.session_mgr import SessionManager

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

SENTINEL_TOKEN = "jwt.s1-1.sentinel"
SENTINEL_CONTENT = "stub LLM content"

_GOOD_REQUEST = OrchestratorRequest(
    prompt="Summarise the quarterly earnings report.",
    agent_type="finance",
    requester_id="user-s1-test-001",
    model="gpt-4o-mini",
)


# ---------------------------------------------------------------------------
# Reusable helper factories
# ---------------------------------------------------------------------------


class _StubAdapter(BaseAdapter):
    """Minimal async LLM adapter that always succeeds and records whether it was called."""

    def __init__(self) -> None:
        self.called: bool = False

    @property
    def provider_name(self) -> str:
        return "stub"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.called = True
        return LLMResponse(
            content=SENTINEL_CONTENT,
            tokens_used=5,
            model=request.model,
            provider="stub",
            finish_reason="stop",
        )


def _passthrough_guardrails() -> Guardrails:
    """Guardrails mock that approves every prompt and returns it unmodified."""
    g = MagicMock(spec=Guardrails)
    g.check_prompt_injection.return_value = None
    g.mask_pii.side_effect = lambda text: MaskResult(text=text, found_types=[])
    return g


def _allow_policy_engine() -> PolicyEngine:
    """PolicyEngine mock that always returns PolicyResult(allowed=True)."""
    pe = MagicMock(spec=PolicyEngine)

    async def _allow(*_a: Any, **_kw: Any) -> PolicyResult:
        return PolicyResult(allowed=True)

    pe.evaluate = _allow
    return pe


def _deny_policy_engine(reasons: list[str]) -> PolicyEngine:
    """PolicyEngine mock that always returns PolicyResult(allowed=False)."""
    pe = MagicMock(spec=PolicyEngine)

    async def _deny(*_a: Any, **_kw: Any) -> PolicyResult:
        return PolicyResult(allowed=False, reasons=reasons)

    pe.evaluate = _deny
    return pe


def _unavailable_policy_engine(message: str = "OPA returned HTTP 503") -> PolicyEngine:
    """PolicyEngine mock that always raises OpaUnavailableError."""
    pe = MagicMock(spec=PolicyEngine)

    async def _fail(*_a: Any, **_kw: Any) -> PolicyResult:
        raise OpaUnavailableError(message)

    pe.evaluate = _fail
    return pe


def _session_mgr_stub() -> SessionManager:
    """SessionManager mock that issues the sentinel token."""
    sm = MagicMock(spec=SessionManager)
    sm.issue_token.return_value = SENTINEL_TOKEN
    return sm


def _make_orchestrator(
    *,
    adapter: BaseAdapter | None = None,
    policy_engine: PolicyEngine | None = None,
    audit_logger: AuditLogger | None = None,
) -> tuple[Orchestrator, _StubAdapter, MagicMock]:
    """Construct a fully-mocked orchestrator; return (orchestrator, adapter, audit_mock)."""
    stub = adapter if isinstance(adapter, _StubAdapter) else _StubAdapter()
    audit = audit_logger if audit_logger is not None else MagicMock(spec=AuditLogger)
    orc = Orchestrator(
        adapter=stub,
        guardrails=_passthrough_guardrails(),
        policy_engine=policy_engine if policy_engine is not None else _allow_policy_engine(),
        session_mgr=_session_mgr_stub(),
        audit_logger=audit,
    )
    return orc, stub, audit


# ===========================================================================
# Unit — allow path
# ===========================================================================


class TestAllowPath:
    """OPA allows → orchestrator must proceed to the LLM adapter."""

    @pytest.mark.asyncio
    async def test_allow_calls_llm_adapter(self) -> None:
        """When OPA returns allow=True the LLM adapter must be invoked."""
        orc, adapter, _ = _make_orchestrator(policy_engine=_allow_policy_engine())
        result = await orc.run(_GOOD_REQUEST)

        assert adapter.called, "LLM adapter must be called when OPA allows"
        assert result.response.content == SENTINEL_CONTENT

    @pytest.mark.asyncio
    async def test_allow_returns_session_token(self) -> None:
        """A successful allow path must include a session token in the result."""
        orc, _, _ = _make_orchestrator()
        result = await orc.run(_GOOD_REQUEST)

        assert result.session_token == SENTINEL_TOKEN

    @pytest.mark.asyncio
    async def test_allow_emits_no_deny_or_unavailable_event(self) -> None:
        """No policy_denied or opa_unavailable event must be emitted on the allow path."""
        audit = MagicMock(spec=AuditLogger)
        orc, _, _ = _make_orchestrator(audit_logger=audit)
        await orc.run(_GOOD_REQUEST)

        denied_calls = [
            c
            for c in audit.warning.call_args_list + audit.error.call_args_list
            if c.args and c.args[0] in {"policy_denied", "opa_unavailable"}
        ]
        assert not denied_calls, (
            f"Unexpected deny/unavailable audit events on allow path: {denied_calls}"
        )


# ===========================================================================
# Unit — deny path
# ===========================================================================


class TestDenyPath:
    """OPA returns allow=False → orchestrator must raise and block the LLM adapter."""

    DENY_REASONS: list[str] = ["agent_type_not_permitted"]

    @pytest.mark.asyncio
    async def test_deny_raises_policy_denied_error(self) -> None:
        """PolicyDeniedError must be raised when OPA returns allow=False."""
        orc, _, _ = _make_orchestrator(
            policy_engine=_deny_policy_engine(self.DENY_REASONS)
        )
        with pytest.raises(PolicyDeniedError):
            await orc.run(_GOOD_REQUEST)

    @pytest.mark.asyncio
    async def test_deny_does_not_call_llm_adapter(self) -> None:
        """The LLM adapter must NOT be called after a policy deny."""
        orc, adapter, _ = _make_orchestrator(
            policy_engine=_deny_policy_engine(self.DENY_REASONS)
        )
        with pytest.raises(PolicyDeniedError):
            await orc.run(_GOOD_REQUEST)

        assert not adapter.called, "LLM adapter must NOT be called after policy deny"

    @pytest.mark.asyncio
    async def test_deny_emits_policy_denied_audit_event(self) -> None:
        """AuditLogger.stage_event('policy.denied', outcome='deny', ...) must be called once."""
        audit = MagicMock(spec=AuditLogger)
        orc, _, _ = _make_orchestrator(
            policy_engine=_deny_policy_engine(self.DENY_REASONS),
            audit_logger=audit,
        )
        with pytest.raises(PolicyDeniedError):
            await orc.run(_GOOD_REQUEST)

        # The orchestrator routes deny events through stage_event(), not warning() directly.
        deny_calls = [
            c for c in audit.stage_event.call_args_list
            if c.args and c.args[0] == "policy.denied"
        ]
        assert len(deny_calls) == 1, (
            f"Expected exactly one 'policy.denied' stage_event call, got {deny_calls}"
        )

    @pytest.mark.asyncio
    async def test_deny_reasons_forwarded_to_audit_event(self) -> None:
        """The reasons list from the OPA result must appear in the policy_denied audit event."""
        audit = MagicMock(spec=AuditLogger)
        expected = ["agent_type_not_permitted"]
        orc, _, _ = _make_orchestrator(
            policy_engine=_deny_policy_engine(expected),
            audit_logger=audit,
        )
        with pytest.raises(PolicyDeniedError):
            await orc.run(_GOOD_REQUEST)

        deny_calls = [
            c for c in audit.stage_event.call_args_list
            if c.args and c.args[0] == "policy.denied"
        ]
        assert len(deny_calls) == 1, (
            f"Expected exactly one 'policy.denied' stage_event call, got {deny_calls}"
        )
        called_kwargs = deny_calls[0].kwargs
        assert "reasons" in called_kwargs, (
            "audit event must carry a 'reasons' kwarg"
        )
        assert called_kwargs["reasons"] == expected, (
            f"reasons mismatch: expected {expected!r}, got {called_kwargs['reasons']!r}"
        )

    @pytest.mark.asyncio
    async def test_deny_session_mgr_not_called(self) -> None:
        """SessionManager must not be invoked after a policy deny (stage 3 must be skipped)."""
        sm = MagicMock(spec=SessionManager)
        orc = Orchestrator(
            adapter=_StubAdapter(),
            guardrails=_passthrough_guardrails(),
            policy_engine=_deny_policy_engine(self.DENY_REASONS),
            session_mgr=sm,
            audit_logger=MagicMock(spec=AuditLogger),
        )
        with pytest.raises(PolicyDeniedError):
            await orc.run(_GOOD_REQUEST)

        sm.issue_token.assert_not_called()
        sm.validate_token.assert_not_called()


# ===========================================================================
# Negative test — OPA unavailable (fail-closed)
# ===========================================================================


class TestOpaUnavailable:
    """OPA raises OpaUnavailableError → orchestrator must deny the request (fail-closed)."""

    @pytest.mark.asyncio
    async def test_opa_unavailable_raises_policy_denied_error(self) -> None:
        """Orchestrator must raise PolicyDeniedError, not let OpaUnavailableError escape."""
        orc, _, _ = _make_orchestrator(
            policy_engine=_unavailable_policy_engine("OPA returned HTTP 503")
        )
        with pytest.raises(PolicyDeniedError):
            await orc.run(_GOOD_REQUEST)

    @pytest.mark.asyncio
    async def test_opa_connect_error_raises_policy_denied_error(self) -> None:
        """A connection-refused scenario must also be fail-closed."""
        orc, _, _ = _make_orchestrator(
            policy_engine=_unavailable_policy_engine("OPA server unreachable: connection refused")
        )
        with pytest.raises(PolicyDeniedError):
            await orc.run(_GOOD_REQUEST)

    @pytest.mark.asyncio
    async def test_opa_unavailable_never_calls_llm_adapter(self) -> None:
        """The LLM adapter must NOT be reached when OPA is unavailable."""
        orc, adapter, _ = _make_orchestrator(
            policy_engine=_unavailable_policy_engine()
        )
        with pytest.raises(PolicyDeniedError):
            await orc.run(_GOOD_REQUEST)

        assert not adapter.called, "LLM adapter must NOT be called when OPA is unavailable"

    @pytest.mark.asyncio
    async def test_opa_unavailable_emits_opa_unavailable_event(self) -> None:
        """An opa_unavailable audit event must be emitted when OPA cannot be reached."""
        audit = MagicMock(spec=AuditLogger)
        orc, _, _ = _make_orchestrator(
            policy_engine=_unavailable_policy_engine("OPA returned HTTP 503"),
            audit_logger=audit,
        )
        with pytest.raises(PolicyDeniedError):
            await orc.run(_GOOD_REQUEST)

        # OPA-unavailable events are routed through stage_event(), not error() directly.
        unavail_calls = [
            c for c in audit.stage_event.call_args_list
            if c.args and c.args[0] == "policy.opa_unavailable"
        ]
        assert len(unavail_calls) == 1, (
            f"Expected exactly one 'policy.opa_unavailable' stage_event call, got {unavail_calls}"
        )
        called_kwargs = unavail_calls[0].kwargs
        assert "error_message" in called_kwargs, (
            "opa_unavailable event must carry an 'error_message' kwarg describing the failure"
        )

    @pytest.mark.asyncio
    async def test_opa_unavailable_does_not_emit_policy_denied_event(self) -> None:
        """opa_unavailable should NOT also emit a policy_denied audit event (distinct paths)."""
        audit = MagicMock(spec=AuditLogger)
        orc, _, _ = _make_orchestrator(
            policy_engine=_unavailable_policy_engine(),
            audit_logger=audit,
        )
        with pytest.raises(PolicyDeniedError):
            await orc.run(_GOOD_REQUEST)

        # The orchestrator routes through stage_event(); check that no policy.denied
        # stage_event was emitted — only policy.opa_unavailable should appear.
        policy_denied_calls = [
            c for c in audit.stage_event.call_args_list
            if c.args and c.args[0] == "policy.denied"
        ]
        assert not policy_denied_calls, (
            "policy.denied audit event must NOT be emitted when the failure is OPA unavailability"
        )


# ===========================================================================
# Unit — PolicyEngine HTTP error handling
# ===========================================================================


class TestPolicyEngineHttpHandling:
    """PolicyEngine must raise OpaUnavailableError on 5xx responses and connection failures."""

    @pytest.mark.asyncio
    async def test_503_response_raises_opa_unavailable_error(self) -> None:
        """PolicyEngine.evaluate() must raise OpaUnavailableError for a 503 response."""
        engine = PolicyEngine(opa_url="http://opa-test-placeholder:8181")
        policy_input = PolicyInput(
            agent_type="finance",
            requester_id="test-user",
            action="llm.complete",
            resource="model:gpt-4o-mini",
        )

        mock_response = MagicMock()
        mock_response.status_code = 503

        async def _mock_post(*_a: Any, **_kw: Any) -> MagicMock:
            return mock_response

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = _mock_post

        with pytest.raises(OpaUnavailableError, match="503"):
            with mock.patch("httpx.AsyncClient", return_value=mock_client):
                await engine.evaluate("agent_access", policy_input)

    @pytest.mark.asyncio
    async def test_500_response_raises_opa_unavailable_error(self) -> None:
        """PolicyEngine.evaluate() must raise OpaUnavailableError for any 5xx response."""
        engine = PolicyEngine(opa_url="http://opa-test-placeholder:8181")
        policy_input = PolicyInput(
            agent_type="finance",
            requester_id="test-user",
            action="llm.complete",
            resource="model:gpt-4o-mini",
        )

        mock_response = MagicMock()
        mock_response.status_code = 500

        async def _mock_post(*_a: Any, **_kw: Any) -> MagicMock:
            return mock_response

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = _mock_post

        with pytest.raises(OpaUnavailableError, match="500"):
            with mock.patch("httpx.AsyncClient", return_value=mock_client):
                await engine.evaluate("agent_access", policy_input)

    @pytest.mark.asyncio
    async def test_connect_error_raises_opa_unavailable_error(self) -> None:
        """A httpx.ConnectError must be converted to OpaUnavailableError."""
        engine = PolicyEngine(opa_url="http://opa-test-placeholder:8181")
        policy_input = PolicyInput(
            agent_type="finance",
            requester_id="test-user",
            action="llm.complete",
            resource="model:gpt-4o-mini",
        )

        async def _raise_connect(*_a: Any, **_kw: Any) -> None:
            raise httpx.ConnectError("connection refused")

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = _raise_connect

        with pytest.raises(OpaUnavailableError, match="unreachable"):
            with mock.patch("httpx.AsyncClient", return_value=mock_client):
                await engine.evaluate("agent_access", policy_input)

    @pytest.mark.asyncio
    async def test_timeout_raises_opa_unavailable_error(self) -> None:
        """A httpx.TimeoutException must be converted to OpaUnavailableError."""
        engine = PolicyEngine(opa_url="http://opa-test-placeholder:8181")
        policy_input = PolicyInput(
            agent_type="finance",
            requester_id="test-user",
            action="llm.complete",
            resource="model:gpt-4o-mini",
        )

        async def _raise_timeout(*_a: Any, **_kw: Any) -> None:
            raise httpx.TimeoutException("request timed out")

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = _raise_timeout

        with pytest.raises(OpaUnavailableError, match="timed out"):
            with mock.patch("httpx.AsyncClient", return_value=mock_client):
                await engine.evaluate("agent_access", policy_input)


# ===========================================================================
# Integration — live OPA container (testcontainers)
# ===========================================================================


@pytest.mark.integration
class TestLiveOpa:
    """Start a real OPA container; load agent_access.rego; assert allow/deny by agent type.

    Run with::

        pytest -m integration tests/test_opa_wiring.py

    Skipped automatically when Docker is unavailable.
    """

    @pytest.fixture(scope="class")
    def opa_url(self) -> str:  # type: ignore[return]
        """Spin up an OPA container and yield its base URL."""
        try:
            from testcontainers.core.container import DockerContainer
        except ImportError:
            pytest.skip("testcontainers not installed; skipping live OPA tests")

        policy_path = Path(__file__).parent.parent / "policies" / "agent_access.rego"
        policy_text = policy_path.read_text()

        container = DockerContainer("openpolicyagent/opa:0.68.0")
        container.with_command("run --server --addr :8181")
        container.with_exposed_ports(8181)

        try:
            container.start()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Docker unavailable – skipping live OPA tests: {exc}")

        try:
            port = container.get_exposed_port(8181)
            _wait_for_opa_http_ready(f"http://localhost:{port}", timeout=30.0)
        except TimeoutError:
            container.stop()
            pytest.skip("OPA container did not become ready in time; skipping")

        url = f"http://localhost:{port}"

        # Upload the agent_access policy via OPA's management API
        with httpx.Client(timeout=10.0) as client:
            resp = client.put(
                f"{url}/v1/policies/aegis/agent_access",
                content=policy_text,
                headers={"Content-Type": "text/plain"},
            )
            resp.raise_for_status()

        yield url

        container.stop()


def _wait_for_opa_http_ready(url: str, *, timeout: float) -> None:
    """Wait until the OPA HTTP endpoint responds successfully."""
    deadline = time.monotonic() + timeout
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{url}/", timeout=1.0)
            if response.status_code == 200:
                return
            last_error = f"HTTP {response.status_code}"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(0.5)
    raise TimeoutError(last_error or "OPA HTTP endpoint did not become ready")

    # --- Allow cases (all five registered agent types) ---

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "agent_type",
        ["finance", "hr", "it", "legal", "general"],
    )
    async def test_registered_agent_type_is_allowed_for_llm_complete(
        self, opa_url: str, agent_type: str
    ) -> None:
        """Each of the five registered agent types must be allowed for llm.complete."""
        engine = PolicyEngine(opa_url=opa_url)
        policy_input = PolicyInput(
            agent_type=agent_type,
            requester_id="integration-test",
            action="llm.complete",
            resource="model:gpt-4o-mini",
        )
        result = await engine.evaluate("agent_access", policy_input)
        assert result.allowed, (
            f"agent_type={agent_type!r} should be allowed for llm.complete; "
            f"reasons={result.reasons!r}"
        )

    # --- Deny cases ---

    @pytest.mark.asyncio
    async def test_unregistered_agent_type_is_denied(self, opa_url: str) -> None:
        """An unregistered agent type must be denied with agent_type_not_permitted."""
        engine = PolicyEngine(opa_url=opa_url)
        policy_input = PolicyInput(
            agent_type="adversarial",
            requester_id="integration-test",
            action="llm.complete",
            resource="model:gpt-4o-mini",
        )
        result = await engine.evaluate("agent_access", policy_input)
        assert not result.allowed, (
            "Unregistered agent_type='adversarial' must be denied"
        )
        assert "agent_type_not_permitted" in result.reasons, (
            f"Expected 'agent_type_not_permitted' in reasons; got {result.reasons!r}"
        )

    @pytest.mark.asyncio
    async def test_expired_token_is_denied_with_reason(self, opa_url: str) -> None:
        """Requests carrying token_expired=true must be denied with the token_expired reason."""
        # Bypass PolicyInput (which lacks token_expired) and call OPA directly.
        payload = {
            "input": {
                "agent_type": "finance",
                "requester_id": "integration-test",
                "action": "llm.complete",
                "resource": "model:gpt-4o-mini",
                "token_expired": True,
                "metadata": {},
            }
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{opa_url}/v1/data/aegis/agent_access",
                json=payload,
            )
            resp.raise_for_status()
            body = resp.json()

        result_data = body.get("result", {})
        assert not result_data.get("allow", False), (
            "token_expired=True must result in a deny decision"
        )
        reasons = result_data.get("reasons", [])
        assert "token_expired" in reasons, (
            f"Expected 'token_expired' in reasons; got {reasons!r}"
        )
