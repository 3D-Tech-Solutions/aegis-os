"""Tests for the Watchdog Budget Enforcer.

Covers W1-1 — BudgetEnforcer raises BudgetExceededError synchronously:
    1. Unit — synchronous raise in call frame.
    2. Unit — boundary exactness (Decimal arithmetic).
    3. Unit — no LLM adapter call after budget breach.
    4. Integration — ``budget.exceeded`` audit event on breach carries all required fields.

Pre-existing baseline tests are preserved at the top of the file.
"""

from __future__ import annotations

import threading
import traceback
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.adapters.base import LLMResponse
from src.audit_vault.logger import AuditLogger
from src.control_plane.orchestrator import BudgetLimitError, Orchestrator, OrchestratorRequest
from src.governance.guardrails import Guardrails, MaskResult
from src.governance.policy_engine.opa_client import PolicyEngine, PolicyResult
from src.governance.session_mgr import SessionManager
from src.watchdog.budget_enforcer import BudgetEnforcer, BudgetExceededError, BudgetSession


@pytest.fixture()
def enforcer() -> BudgetEnforcer:
    return BudgetEnforcer()


def test_create_session_returns_budget_session(enforcer: BudgetEnforcer) -> None:
    sid = uuid4()
    session = enforcer.create_session(sid, agent_type="finance", budget_limit_usd=5.0)
    assert isinstance(session, BudgetSession)
    assert session.budget_limit_usd == 5.0
    assert session.tokens_used == 0
    assert session.cost_usd == 0.0


def test_record_tokens_increments_usage(enforcer: BudgetEnforcer) -> None:
    sid = uuid4()
    enforcer.create_session(sid, agent_type="hr", budget_limit_usd=1.0)
    session = enforcer.record_tokens(sid, tokens=100)
    assert session.tokens_used == 100
    assert session.cost_usd > 0.0


def test_budget_exceeded_raises_error(enforcer: BudgetEnforcer) -> None:
    sid = uuid4()
    # Very small budget - 1 token will exceed it with any nonzero cost_per_token
    enforcer.create_session(sid, agent_type="it", budget_limit_usd=0.000_001)
    with pytest.raises(BudgetExceededError):
        enforcer.record_tokens(sid, tokens=1000)


def test_budget_not_exceeded_within_limit(enforcer: BudgetEnforcer) -> None:
    sid = uuid4()
    enforcer.create_session(sid, agent_type="general", budget_limit_usd=100.0)
    session = enforcer.record_tokens(sid, tokens=100)
    assert not session.alerts


def test_get_session_returns_none_for_unknown(enforcer: BudgetEnforcer) -> None:
    assert enforcer.get_session(uuid4()) is None


def test_record_tokens_unknown_session_raises(enforcer: BudgetEnforcer) -> None:
    with pytest.raises(KeyError):
        enforcer.record_tokens(uuid4(), tokens=10)


def test_multiple_token_recordings_accumulate(enforcer: BudgetEnforcer) -> None:
    sid = uuid4()
    enforcer.create_session(sid, agent_type="finance", budget_limit_usd=50.0)
    enforcer.record_tokens(sid, tokens=100)
    session = enforcer.record_tokens(sid, tokens=200)
    assert session.tokens_used == 300


# ---------------------------------------------------------------------------
# W1-1 — Test 1: Synchronous raise in the same call frame
# ---------------------------------------------------------------------------


def test_synchronous_raise_in_call_frame() -> None:
    """BudgetExceededError must originate inside record_spend(), never deferred.

    The test calls record_spend() inside a worker thread and then inspects
    ``exc.__traceback__`` to confirm the raise occurred directly within the
    ``record_spend`` stack frame, not via a background callback or future.
    """
    enforcer = BudgetEnforcer()
    sid = uuid4()
    enforcer.create_session(sid, agent_type="finance", budget_limit_usd=Decimal("1.00"))

    exc_info: dict[str, Any] = {}

    def _worker() -> None:
        try:
            # Amount exceeds the $1.00 cap — should raise immediately.
            enforcer.record_spend(sid, Decimal("1.50"))
        except BudgetExceededError as exc:
            exc_info["exc"] = exc
            exc_info["tb"] = traceback.extract_tb(exc.__traceback__)

    t = threading.Thread(target=_worker)
    t.start()
    t.join(timeout=2.0)

    assert not t.is_alive(), "Thread must complete without blocking (synchronous raise)"
    assert "exc" in exc_info, "BudgetExceededError was not raised at all"

    frame_names = [frame.name for frame in exc_info["tb"]]
    assert "record_spend" in frame_names, (
        "BudgetExceededError must be raised directly inside record_spend(), "
        f"not deferred. Traceback frames: {frame_names}"
    )
    # The calling thread function must also appear in the traceback.
    assert "_worker" in frame_names


def test_synchronous_raise_record_spend_is_innermost_frame() -> None:
    """``record_spend`` must be the innermost frame in the raised exception traceback."""
    enforcer = BudgetEnforcer()
    sid = uuid4()
    enforcer.create_session(sid, agent_type="hr", budget_limit_usd=Decimal("0.01"))

    with pytest.raises(BudgetExceededError) as exc_info:
        enforcer.record_spend(sid, Decimal("1.00"))

    tb_frames = traceback.extract_tb(exc_info.value.__traceback__)
    assert tb_frames[-1].name == "record_spend", (
        "The innermost traceback frame must be 'record_spend'; found: "
        f"{tb_frames[-1].name!r}"
    )


# ---------------------------------------------------------------------------
# W1-1 — Test 2: Decimal boundary exactness
# ---------------------------------------------------------------------------


def test_boundary_exactness_below_limit_no_error() -> None:
    """Spending $0.999999 on a $1.00 cap must NOT raise BudgetExceededError."""
    enforcer = BudgetEnforcer()
    sid = uuid4()
    enforcer.create_session(sid, agent_type="finance", budget_limit_usd=Decimal("1.000000"))

    # $0.999999 ≤ $1.000000 — no raise expected.
    session = enforcer.record_spend(sid, Decimal("0.999999"))
    assert session.cost_usd == Decimal("0.999999")
    assert len(session.alerts) == 0


def test_boundary_exactness_at_exact_limit_no_error() -> None:
    """Spending exactly $1.000000 on a $1.00 cap must NOT raise (strictly greater check)."""
    enforcer = BudgetEnforcer()
    sid = uuid4()
    enforcer.create_session(sid, agent_type="finance", budget_limit_usd=Decimal("1.000000"))

    session = enforcer.record_spend(sid, Decimal("1.000000"))
    assert session.cost_usd == Decimal("1.000000")
    assert len(session.alerts) == 0


def test_boundary_exactness_one_unit_over_raises() -> None:
    """Spending $0.999999 then adding $0.000002 (total $1.000001) must raise immediately."""
    enforcer = BudgetEnforcer()
    sid = uuid4()
    enforcer.create_session(sid, agent_type="finance", budget_limit_usd=Decimal("1.000000"))

    # First spend: $0.999999 — no raise.
    enforcer.record_spend(sid, Decimal("0.999999"))

    # Second spend: $0.000002 pushes total to $1.000001 — must raise.
    with pytest.raises(BudgetExceededError) as exc_info:
        enforcer.record_spend(sid, Decimal("0.000002"))

    session = enforcer.get_session(sid)
    assert session is not None
    assert session.cost_usd == Decimal("1.000001"), (
        f"Expected cost_usd=1.000001 after breach, got {session.cost_usd}"
    )
    assert "1.000001" in str(exc_info.value)
    assert "1.000000" in str(exc_info.value)


def test_boundary_exactness_float_budget_converts_correctly() -> None:
    """Float budget limits must be converted to Decimal without representation error."""
    enforcer = BudgetEnforcer()
    sid = uuid4()
    # Passing a float: must be stored as Decimal("1.0") not subject to IEEE 754 drift.
    enforcer.create_session(sid, agent_type="general", budget_limit_usd=1.0)

    session = enforcer.get_session(sid)
    assert session is not None
    assert isinstance(session.budget_limit_usd, Decimal)
    assert session.budget_limit_usd == Decimal("1.0")


# ---------------------------------------------------------------------------
# W1-1 — Test 3: No LLM adapter call after budget breach
# ---------------------------------------------------------------------------


def _make_mock_orchestrator_deps() -> tuple[
    AsyncMock, MagicMock, AsyncMock, MagicMock
]:
    """Return (adapter, guardrails_mock, policy_mock, session_mgr_mock) pre-wired."""
    adapter = AsyncMock()
    adapter.complete = AsyncMock(
        return_value=LLMResponse(
            content="Some response",
            tokens_used=500,
            model="gpt-4o-mini",
            provider="openai",
        )
    )

    guardrails = MagicMock(spec=Guardrails)
    guardrails.check_prompt_injection.return_value = None
    # Return the input text unchanged so Stage 5 does not corrupt the response content.
    guardrails.mask_pii.side_effect = lambda text: MaskResult(text=text, found_types=[])

    policy_engine = MagicMock(spec=PolicyEngine)
    policy_engine.evaluate = AsyncMock(
        return_value=PolicyResult(allowed=True, action="allow", reasons=[], fields=[])
    )

    session_mgr = MagicMock(spec=SessionManager)
    session_mgr.issue_token.return_value = "eyJ.stub.token"
    claims_mock = MagicMock()
    claims_mock.agent_type = "finance"
    claims_mock.jti = "stub-jti-0001"
    session_mgr.validate_token.return_value = claims_mock
    session_mgr.is_expired.return_value = False

    return adapter, guardrails, policy_engine, session_mgr


async def test_no_llm_call_after_budget_breach() -> None:
    """LLM adapter.complete() must never be called when the budget is already exhausted.

    The Watchdog pre-check (stage.watchdog_pre) runs before Stage 4; if the
    budget session is at or beyond the cap the orchestrator must deny the
    request and leave the adapter untouched.
    """
    adapter, guardrails, policy_engine, session_mgr = _make_mock_orchestrator_deps()

    enforcer = BudgetEnforcer()
    sid = uuid4()
    enforcer.create_session(sid, agent_type="finance", budget_limit_usd=Decimal("0.01"))
    # Exhaust the budget by recording exactly the limit — check_budget triggers
    # when cost_usd >= budget_limit_usd.
    enforcer.record_spend(sid, Decimal("0.01"))

    orchestrator = Orchestrator(
        adapter=adapter,
        guardrails=guardrails,
        policy_engine=policy_engine,
        session_mgr=session_mgr,
        budget_enforcer=enforcer,
    )
    request = OrchestratorRequest(
        prompt="Generate a financial summary report.",
        agent_type="finance",
        requester_id="test-user-001",
        budget_session_id=sid,
    )

    with pytest.raises(BudgetLimitError):
        await orchestrator.run(request)

    adapter.complete.assert_not_called()


async def test_no_llm_call_budget_enforcer_inactive_without_session_id() -> None:
    """When no budget_session_id is set the watchdog is inactive and the adapter runs."""
    adapter, guardrails, policy_engine, session_mgr = _make_mock_orchestrator_deps()

    enforcer = BudgetEnforcer()
    # No session created — should not matter since budget_session_id is None.

    orchestrator = Orchestrator(
        adapter=adapter,
        guardrails=guardrails,
        policy_engine=policy_engine,
        session_mgr=session_mgr,
        budget_enforcer=enforcer,
    )
    request = OrchestratorRequest(
        prompt="Hello, summarise Q3 results.",
        agent_type="finance",
        requester_id="test-user-002",
        # budget_session_id intentionally omitted
    )

    result = await orchestrator.run(request)
    assert result.response.content == "Some response"
    adapter.complete.assert_called_once()


async def test_budget_breach_on_post_llm_record_spend() -> None:
    """BudgetLimitError is raised after the LLM call when that spend tip the cap."""
    adapter, guardrails, policy_engine, session_mgr = _make_mock_orchestrator_deps()
    # Adapter returns 500 tokens; at $0.000002/token = $0.001 total.
    # Set cap just below the cost of the first response.
    enforcer = BudgetEnforcer()
    sid = uuid4()
    enforcer.create_session(sid, agent_type="finance", budget_limit_usd=Decimal("0.0005"))

    orchestrator = Orchestrator(
        adapter=adapter,
        guardrails=guardrails,
        policy_engine=policy_engine,
        session_mgr=session_mgr,
        budget_enforcer=enforcer,
    )
    request = OrchestratorRequest(
        prompt="Write a detailed risk analysis.",
        agent_type="finance",
        requester_id="test-user-003",
        budget_session_id=sid,
        cost_per_token=Decimal("0.000002"),
    )

    with pytest.raises(BudgetLimitError):
        await orchestrator.run(request)

    # The adapter WAS called (pre-check passed; breach detected post-LLM).
    adapter.complete.assert_called_once()


# ---------------------------------------------------------------------------
# W1-1 — Test 4: Integration — budget.exceeded audit event fields
# ---------------------------------------------------------------------------


async def test_integration_audit_event_on_breach_via_record_spend() -> None:
    """budget.exceeded audit event must carry session_id, agent_type, spent_usd, limit_usd.

    Runs the orchestrator pipeline to budget exhaustion via the post-LLM
    record_spend() path.  Asserts the injected AuditLogger received a
    ``budget.exceeded`` warning event with all four required fields.
    """
    mock_audit_logger = MagicMock(spec=AuditLogger)
    enforcer = BudgetEnforcer(audit_logger=mock_audit_logger)

    sid = uuid4()
    # Limit chosen so the LLM response cost (500 tokens * $0.000002 = $0.001) exceeds it.
    enforcer.create_session(sid, agent_type="hr", budget_limit_usd=Decimal("0.0005"))

    adapter, guardrails, policy_engine, session_mgr = _make_mock_orchestrator_deps()
    # Re-wire session_mgr claims to agent_type="hr".
    claims_mock = MagicMock()
    claims_mock.agent_type = "hr"
    claims_mock.jti = "stub-jti-hr-0001"
    session_mgr.validate_token.return_value = claims_mock

    orchestrator = Orchestrator(
        adapter=adapter,
        guardrails=guardrails,
        policy_engine=policy_engine,
        session_mgr=session_mgr,
        budget_enforcer=enforcer,
    )
    request = OrchestratorRequest(
        prompt="List HR policy changes from last quarter.",
        agent_type="hr",
        requester_id="hr-user-001",
        budget_session_id=sid,
        cost_per_token=Decimal("0.000002"),
    )

    with pytest.raises(BudgetLimitError):
        await orchestrator.run(request)

    # Find the budget.exceeded warning call.
    warning_calls = mock_audit_logger.warning.call_args_list
    exceeded_calls = [
        c for c in warning_calls if c.args and c.args[0] == "budget.exceeded"
    ]
    assert len(exceeded_calls) >= 1, (
        f"Expected at least one 'budget.exceeded' warning event; "
        f"got: {[c.args[0] for c in warning_calls]}"
    )

    event_kwargs = exceeded_calls[0].kwargs
    assert event_kwargs["session_id"] == str(sid), (
        f"session_id mismatch: {event_kwargs['session_id']!r} != {str(sid)!r}"
    )
    assert event_kwargs["agent_type"] == "hr", (
        f"agent_type mismatch: {event_kwargs['agent_type']!r}"
    )
    # spent_usd must be a string representation of the Decimal total.
    assert "spent_usd" in event_kwargs, "'spent_usd' field missing from budget.exceeded event"
    assert "limit_usd" in event_kwargs, "'limit_usd' field missing from budget.exceeded event"
    # The spent amount must exceed the limit.
    assert Decimal(event_kwargs["spent_usd"]) > Decimal(event_kwargs["limit_usd"]), (
        f"spent_usd ({event_kwargs['spent_usd']}) must exceed "
        f"limit_usd ({event_kwargs['limit_usd']})"
    )


async def test_integration_audit_event_on_breach_via_check_budget() -> None:
    """budget.exceeded audit event is also emitted via the check_budget() pre-LLM path."""
    mock_audit_logger = MagicMock(spec=AuditLogger)
    enforcer = BudgetEnforcer(audit_logger=mock_audit_logger)

    sid = uuid4()
    enforcer.create_session(sid, agent_type="legal", budget_limit_usd=Decimal("0.01"))
    # Exhaust budget silently (record_spend raises; catch and discard for setup).
    try:
        enforcer.record_spend(sid, Decimal("0.02"))
    except BudgetExceededError:
        pass
    mock_audit_logger.reset_mock()  # Clear the breach event from setup.

    adapter, guardrails, policy_engine, session_mgr = _make_mock_orchestrator_deps()
    claims_mock = MagicMock()
    claims_mock.agent_type = "legal"
    claims_mock.jti = "stub-jti-legal-0001"
    session_mgr.validate_token.return_value = claims_mock

    orchestrator = Orchestrator(
        adapter=adapter,
        guardrails=guardrails,
        policy_engine=policy_engine,
        session_mgr=session_mgr,
        budget_enforcer=enforcer,
    )
    request = OrchestratorRequest(
        prompt="Review the contract clauses.",
        agent_type="legal",
        requester_id="legal-user-001",
        budget_session_id=sid,
    )

    with pytest.raises(BudgetLimitError):
        await orchestrator.run(request)

    warning_calls = mock_audit_logger.warning.call_args_list
    exceeded_calls = [
        c for c in warning_calls if c.args and c.args[0] == "budget.exceeded"
    ]
    assert len(exceeded_calls) >= 1

    event_kwargs = exceeded_calls[0].kwargs
    assert event_kwargs["session_id"] == str(sid)
    assert event_kwargs["agent_type"] == "legal"
    assert "spent_usd" in event_kwargs
    assert "limit_usd" in event_kwargs


# ---------------------------------------------------------------------------
# W-prep-1: BudgetSession Temporal serialization primitives
# ---------------------------------------------------------------------------


class TestBudgetSessionSerialization:
    """BudgetSession.serialize / BudgetSession.deserialize round-trip contract (W-prep-1)."""

    def test_serialize_deserialize_full_roundtrip(self, enforcer: BudgetEnforcer) -> None:
        """All fields must survive the serialize → deserialize cycle without loss."""
        sid = uuid4()
        session = enforcer.create_session(
            sid, agent_type="finance", budget_limit_usd=Decimal("5.00")
        )
        enforcer.record_tokens(session.session_id, 100)

        snapshot = session.serialize()
        restored = BudgetSession.deserialize(snapshot)

        assert restored.session_id == session.session_id
        assert restored.agent_type == session.agent_type
        assert restored.budget_limit_usd == session.budget_limit_usd
        assert restored.cost_usd == session.cost_usd
        assert restored.tokens_used == session.tokens_used
        assert restored.alerts == session.alerts

    def test_serialize_preserves_decimal_precision(self) -> None:
        """Decimal fields must survive the cycle without floating-point drift."""
        sid = uuid4()
        session = BudgetSession(
            session_id=sid,
            agent_type="hr",
            budget_limit_usd=Decimal("0.000002"),
            cost_usd=Decimal("0.000001"),
        )
        restored = BudgetSession.deserialize(session.serialize())

        assert restored.budget_limit_usd == Decimal("0.000002")
        assert restored.cost_usd == Decimal("0.000001")

    def test_serialize_alerts_list_is_independent_copy(self) -> None:
        """Mutating the original session's alerts must not affect the snapshot."""
        sid = uuid4()
        session = BudgetSession(
            session_id=sid,
            agent_type="it",
            budget_limit_usd=Decimal("1.00"),
        )
        snapshot = session.serialize()
        session.alerts.append("post-snapshot alert")

        assert "post-snapshot alert" not in snapshot["alerts"]

    def test_deserialize_restores_independent_session(self) -> None:
        """The deserialized session must be a new object, not a reference to the original."""
        sid = uuid4()
        session = BudgetSession(
            session_id=sid,
            agent_type="legal",
            budget_limit_usd=Decimal("10.00"),
        )
        restored = BudgetSession.deserialize(session.serialize())
        restored.alerts.append("mutation")

        assert session.alerts == []
