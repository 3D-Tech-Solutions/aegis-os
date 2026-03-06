"""Tests for the Watchdog LoopDetector — W1-2.

Covers:
  1. Unit — step-count breach at exactly max_agent_steps (injectable parameter).
  2. Unit — token-velocity breach raises TokenVelocityError (distinct from LoopDetectedError).
  3. Unit — PROGRESS signal resets the NO_PROGRESS streak counter.
  4. Integration — orchestrator loop terminates and emits loop.detected audit event.
  5. Negative test — HUMAN_REQUIRED signal raises PendingApprovalError (not LoopDetectedError).

Pre-existing baseline tests are preserved and updated to reflect the new
TokenVelocityError / PendingApprovalError split.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.adapters.base import LLMResponse
from src.audit_vault.logger import AuditLogger
from src.control_plane.orchestrator import (
    LoopApprovalError,
    LoopHaltError,
    Orchestrator,
    OrchestratorRequest,
)
from src.governance.guardrails import Guardrails, MaskResult
from src.governance.policy_engine.opa_client import PolicyEngine, PolicyResult
from src.governance.session_mgr import SessionManager
from src.watchdog.loop_detector import (
    LoopDetectedError,
    LoopDetector,
    LoopSignal,
    PendingApprovalError,
    TokenVelocityError,
)

# ---------------------------------------------------------------------------
# Shared fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def detector() -> LoopDetector:
    """Default detector using settings.max_agent_steps / settings.max_token_velocity."""
    return LoopDetector()


@pytest.fixture()
def detector_3() -> LoopDetector:
    """Detector with max_agent_steps=3, max_token_velocity=500 for precise boundary tests."""
    return LoopDetector(max_agent_steps=3, max_token_velocity=500)


def _make_orchestrator_mocks() -> tuple[AsyncMock, MagicMock, AsyncMock, MagicMock]:
    """Return (adapter, guardrails, policy_engine, session_mgr) pre-wired stubs."""
    adapter = AsyncMock()
    adapter.complete = AsyncMock(
        return_value=LLMResponse(
            content="step response",
            tokens_used=100,
            model="gpt-4o-mini",
            provider="stub",
        )
    )

    guardrails = MagicMock(spec=Guardrails)
    guardrails.check_prompt_injection.return_value = None
    guardrails.mask_pii.side_effect = lambda text: MaskResult(text=text, found_types=[])

    policy_engine = MagicMock(spec=PolicyEngine)
    policy_engine.evaluate = AsyncMock(
        return_value=PolicyResult(allowed=True, action="allow", reasons=[], fields=[])
    )

    session_mgr = MagicMock(spec=SessionManager)
    session_mgr.issue_token.return_value = "eyJ.stub.token"
    claims = MagicMock()
    claims.agent_type = "finance"
    claims.jti = "stub-jti-loop-001"
    session_mgr.validate_token.return_value = claims
    session_mgr.is_expired.return_value = False

    return adapter, guardrails, policy_engine, session_mgr


# ---------------------------------------------------------------------------
# Baseline tests (updated)
# ---------------------------------------------------------------------------


def test_create_context(detector: LoopDetector) -> None:
    sid = uuid4()
    ctx = detector.create_context(sid, agent_type="finance")
    assert ctx.session_id == sid
    assert ctx.agent_type == "finance"
    assert not ctx.loop_detected


def test_record_step_with_progress(detector: LoopDetector) -> None:
    sid = uuid4()
    detector.create_context(sid, agent_type="general")
    ctx = detector.record_step(sid, token_delta=100, signal=LoopSignal.PROGRESS)
    assert len(ctx.steps) == 1
    assert ctx.total_tokens == 100


def test_no_loop_within_step_limit(detector: LoopDetector) -> None:
    sid = uuid4()
    detector.create_context(sid, agent_type="general")
    for _ in range(5):
        detector.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)
    ctx = detector.get_context(sid)
    assert ctx is not None
    assert not ctx.loop_detected


def test_loop_detected_after_max_steps(detector: LoopDetector) -> None:
    sid = uuid4()
    detector.create_context(sid, agent_type="general")
    with pytest.raises(LoopDetectedError):
        for _ in range(15):  # exceeds default max_agent_steps=10
            detector.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)


def test_progress_signal_resets_streak(detector: LoopDetector) -> None:
    sid = uuid4()
    detector.create_context(sid, agent_type="general")
    for _ in range(9):
        detector.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)
    detector.record_step(sid, token_delta=10, signal=LoopSignal.PROGRESS)
    for _ in range(9):
        detector.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)
    ctx = detector.get_context(sid)
    assert ctx is not None
    assert not ctx.loop_detected


def test_token_velocity_exceeded_raises_token_velocity_error(detector: LoopDetector) -> None:
    """Velocity breach raises TokenVelocityError, not LoopDetectedError."""
    sid = uuid4()
    detector.create_context(sid, agent_type="it")
    with pytest.raises(TokenVelocityError):
        detector.record_step(sid, token_delta=100_000, signal=LoopSignal.PROGRESS)


def test_get_context_returns_none_for_unknown(detector: LoopDetector) -> None:
    assert detector.get_context(uuid4()) is None


def test_record_step_unknown_session_raises(detector: LoopDetector) -> None:
    with pytest.raises(KeyError):
        detector.record_step(uuid4(), token_delta=10)


# ---------------------------------------------------------------------------
# W1-2 Test 1: Step-count breach at exactly max_agent_steps
# ---------------------------------------------------------------------------


class TestStepCountBreach:
    """LoopDetectedError must be raised on exactly the Nth NO_PROGRESS step."""

    def test_breach_on_third_step_exactly(self, detector_3: LoopDetector) -> None:
        """Raise on step 3, not step 4, when max_agent_steps=3."""
        sid = uuid4()
        detector_3.create_context(sid, agent_type="finance")

        # Steps 1 and 2 — no raise.
        detector_3.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)
        detector_3.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)

        # Step 3 — must raise.
        with pytest.raises(LoopDetectedError):
            detector_3.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)

    def test_not_raised_before_third_step(self, detector_3: LoopDetector) -> None:
        """Steps 1 and 2 must never raise LoopDetectedError."""
        sid = uuid4()
        detector_3.create_context(sid, agent_type="hr")

        ctx1 = detector_3.record_step(sid, token_delta=5, signal=LoopSignal.NO_PROGRESS)
        assert not ctx1.loop_detected

        ctx2 = detector_3.record_step(sid, token_delta=5, signal=LoopSignal.NO_PROGRESS)
        assert not ctx2.loop_detected

    def test_loop_detected_flag_set_on_breach(self, detector_3: LoopDetector) -> None:
        """ExecutionContext.loop_detected must be True after the circuit trips."""
        sid = uuid4()
        detector_3.create_context(sid, agent_type="it")
        try:
            for _ in range(3):
                detector_3.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)
        except LoopDetectedError:
            pass
        ctx = detector_3.get_context(sid)
        assert ctx is not None
        assert ctx.loop_detected is True

    def test_loop_detected_audit_event_emitted(self) -> None:
        """A loop.detected audit event must be emitted before the raise."""
        mock_logger = MagicMock(spec=AuditLogger)
        det = LoopDetector(max_agent_steps=2, audit_logger=mock_logger)
        sid = uuid4()
        det.create_context(sid, agent_type="finance")

        with pytest.raises(LoopDetectedError):
            det.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)
            det.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)

        warning_calls = mock_logger.warning.call_args_list
        loop_events = [c for c in warning_calls if c.args and c.args[0] == "loop.detected"]
        assert len(loop_events) >= 1

        event_kwargs = loop_events[0].kwargs
        assert event_kwargs["reason"] == "no_progress_streak"
        assert event_kwargs["session_id"] == str(sid)

    def test_injectable_max_agent_steps_is_respected(self) -> None:
        """max_agent_steps=5 must trip on step 5, not earlier."""
        det = LoopDetector(max_agent_steps=5, max_token_velocity=99_999)
        sid = uuid4()
        det.create_context(sid, agent_type="general")

        for i in range(4):
            ctx = det.record_step(sid, token_delta=1, signal=LoopSignal.NO_PROGRESS)
            assert not ctx.loop_detected, f"Should not trip before step 5 (step {i + 1})"

        with pytest.raises(LoopDetectedError):
            det.record_step(sid, token_delta=1, signal=LoopSignal.NO_PROGRESS)


# ---------------------------------------------------------------------------
# W1-2 Test 2: Token-velocity breach raises TokenVelocityError
# ---------------------------------------------------------------------------


class TestTokenVelocityBreach:
    """TokenVelocityError must be raised regardless of step count or signal."""

    def test_raises_token_velocity_error_not_loop_detected_error(
        self, detector_3: LoopDetector
    ) -> None:
        """The raised exception must be TokenVelocityError, not LoopDetectedError."""
        sid = uuid4()
        detector_3.create_context(sid, agent_type="finance")
        with pytest.raises(TokenVelocityError):
            detector_3.record_step(sid, token_delta=501, signal=LoopSignal.PROGRESS)

    def test_does_not_raise_loop_detected_error_on_velocity_breach(
        self, detector_3: LoopDetector
    ) -> None:
        """Velocity breach must never raise LoopDetectedError."""
        sid = uuid4()
        detector_3.create_context(sid, agent_type="hr")
        with pytest.raises(TokenVelocityError):
            detector_3.record_step(sid, token_delta=1_000, signal=LoopSignal.NO_PROGRESS)

        # LoopDetectedError must not be the raised type.
        try:
            detector_3.record_step(sid, token_delta=1_000, signal=LoopSignal.NO_PROGRESS)
        except TokenVelocityError:
            pass
        except LoopDetectedError:
            pytest.fail("LoopDetectedError raised instead of TokenVelocityError")

    def test_velocity_breach_on_first_step(self, detector_3: LoopDetector) -> None:
        """Velocity check fires on step 1 (regardless of step count)."""
        sid = uuid4()
        detector_3.create_context(sid, agent_type="it")
        with pytest.raises(TokenVelocityError):
            # Step 1, well below max_agent_steps=3.
            detector_3.record_step(sid, token_delta=600, signal=LoopSignal.NO_PROGRESS)

    def test_velocity_audit_event_contains_reason(self) -> None:
        """loop.detected event for velocity breach must carry reason=token_velocity_exceeded."""
        mock_logger = MagicMock(spec=AuditLogger)
        det = LoopDetector(max_agent_steps=10, max_token_velocity=100, audit_logger=mock_logger)
        sid = uuid4()
        det.create_context(sid, agent_type="finance")

        with pytest.raises(TokenVelocityError):
            det.record_step(sid, token_delta=200, signal=LoopSignal.NO_PROGRESS)

        warning_calls = mock_logger.warning.call_args_list
        velocity_events = [
            c
            for c in warning_calls
            if c.args
            and c.args[0] == "loop.detected"
            and c.kwargs.get("reason") == "token_velocity_exceeded"
        ]
        assert len(velocity_events) >= 1

    def test_exact_velocity_boundary_no_raise(self) -> None:
        """token_delta == max_token_velocity must NOT raise (strictly greater-than check)."""
        det = LoopDetector(max_agent_steps=10, max_token_velocity=500)
        sid = uuid4()
        det.create_context(sid, agent_type="general")
        # Exactly at boundary — no raise expected.
        ctx = det.record_step(sid, token_delta=500, signal=LoopSignal.PROGRESS)
        assert ctx is not None

    def test_one_over_velocity_boundary_raises(self) -> None:
        """token_delta == max_token_velocity + 1 must raise TokenVelocityError."""
        det = LoopDetector(max_agent_steps=10, max_token_velocity=500)
        sid = uuid4()
        det.create_context(sid, agent_type="general")
        with pytest.raises(TokenVelocityError):
            det.record_step(sid, token_delta=501, signal=LoopSignal.PROGRESS)


# ---------------------------------------------------------------------------
# W1-2 Test 3: PROGRESS signal resets the NO_PROGRESS streak
# ---------------------------------------------------------------------------


class TestProgressResetsStreak:
    """A PROGRESS signal must reset the trailing NO_PROGRESS counter to zero."""

    def test_two_no_progress_then_progress_then_two_more_no_raise(
        self, detector_3: LoopDetector
    ) -> None:
        """2 NO_PROGRESS → 1 PROGRESS → 2 NO_PROGRESS must NOT trip max_agent_steps=3."""
        sid = uuid4()
        detector_3.create_context(sid, agent_type="finance")

        detector_3.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)
        detector_3.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)
        # Streak resets here.
        detector_3.record_step(sid, token_delta=10, signal=LoopSignal.PROGRESS)
        detector_3.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)
        detector_3.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)

        ctx = detector_3.get_context(sid)
        assert ctx is not None
        assert not ctx.loop_detected

    def test_two_no_progress_then_progress_then_three_trips(
        self, detector_3: LoopDetector
    ) -> None:
        """2 NO_PROGRESS → 1 PROGRESS → 3 NO_PROGRESS must trip on the 3rd post-reset step."""
        sid = uuid4()
        detector_3.create_context(sid, agent_type="hr")

        detector_3.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)
        detector_3.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)
        detector_3.record_step(sid, token_delta=10, signal=LoopSignal.PROGRESS)
        detector_3.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)
        detector_3.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)

        with pytest.raises(LoopDetectedError):
            detector_3.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)

    def test_progress_at_step_one_means_fresh_counter(self, detector_3: LoopDetector) -> None:
        """PROGRESS on step 1 resets any hypothetical trailing count; max steps still apply."""
        sid = uuid4()
        detector_3.create_context(sid, agent_type="general")
        detector_3.record_step(sid, token_delta=5, signal=LoopSignal.PROGRESS)

        # Now 2 more NO_PROGRESS: streak=2, max=3 → no raise.
        detector_3.record_step(sid, token_delta=5, signal=LoopSignal.NO_PROGRESS)
        ctx = detector_3.record_step(sid, token_delta=5, signal=LoopSignal.NO_PROGRESS)
        assert not ctx.loop_detected

        # Third NO_PROGRESS → trips.
        with pytest.raises(LoopDetectedError):
            detector_3.record_step(sid, token_delta=5, signal=LoopSignal.NO_PROGRESS)

    def test_multiple_progress_signals_each_reset_counter(
        self, detector_3: LoopDetector
    ) -> None:
        """Each PROGRESS resets independently; the counter never accumulates across resets."""
        sid = uuid4()
        detector_3.create_context(sid, agent_type="it")

        for _cycle in range(5):
            detector_3.record_step(sid, token_delta=1, signal=LoopSignal.NO_PROGRESS)
            detector_3.record_step(sid, token_delta=1, signal=LoopSignal.NO_PROGRESS)
            detector_3.record_step(sid, token_delta=1, signal=LoopSignal.PROGRESS)

        ctx = detector_3.get_context(sid)
        assert ctx is not None
        assert not ctx.loop_detected
        assert len(ctx.steps) == 15


# ---------------------------------------------------------------------------
# W1-2 Test 4: Integration — halt propagates through orchestrator
# ---------------------------------------------------------------------------


class TestIntegrationHaltPropagates:
    """Orchestrator loop terminates on max_agent_steps and emits loop.detected."""

    async def test_orchestrator_halts_within_max_agent_steps(self) -> None:
        """Orchestrator raises LoopHaltError after max_agent_steps NO_PROGRESS iterations."""
        mock_audit = MagicMock(spec=AuditLogger)
        loop_det = LoopDetector(
            max_agent_steps=3, max_token_velocity=99_999, audit_logger=mock_audit
        )

        adapter, guardrails, policy_engine, session_mgr = _make_orchestrator_mocks()
        orchestrator = Orchestrator(
            adapter=adapter,
            guardrails=guardrails,
            policy_engine=policy_engine,
            session_mgr=session_mgr,
            loop_detector=loop_det,
        )

        loop_sid = uuid4()
        loop_det.create_context(loop_sid, agent_type="finance")

        request = OrchestratorRequest(
            prompt="Analyse quarterly data.",
            agent_type="finance",
            requester_id="test-user-loop-001",
            loop_session_id=loop_sid,
            loop_signal=LoopSignal.NO_PROGRESS,
        )

        # Run in a loop until halted; tally iteration count.
        iterations = 0
        halt_raised = False
        while iterations < 10:  # safety ceiling far above max_agent_steps=3
            try:
                await orchestrator.run(request)
                iterations += 1
            except LoopHaltError:
                halt_raised = True
                iterations += 1
                break

        assert halt_raised, "LoopHaltError was never raised by the orchestrator"
        assert iterations <= 3, (
            f"Orchestrator should halt within max_agent_steps=3 iterations; "
            f"ran {iterations}"
        )

    async def test_loop_detected_audit_event_emitted_by_orchestrator(self) -> None:
        """A loop.detected warning event must be emitted before LoopHaltError propagates."""
        mock_audit = MagicMock(spec=AuditLogger)
        loop_det = LoopDetector(
            max_agent_steps=2, max_token_velocity=99_999, audit_logger=mock_audit
        )

        adapter, guardrails, policy_engine, session_mgr = _make_orchestrator_mocks()
        orchestrator = Orchestrator(
            adapter=adapter,
            guardrails=guardrails,
            policy_engine=policy_engine,
            session_mgr=session_mgr,
            loop_detector=loop_det,
        )

        loop_sid = uuid4()
        loop_det.create_context(loop_sid, agent_type="finance")

        request = OrchestratorRequest(
            prompt="Generate risk report.",
            agent_type="finance",
            requester_id="test-user-loop-002",
            loop_session_id=loop_sid,
            loop_signal=LoopSignal.NO_PROGRESS,
        )

        for _i in range(3):
            try:
                await orchestrator.run(request)
            except LoopHaltError:
                break

        warning_calls = mock_audit.warning.call_args_list
        loop_events = [c for c in warning_calls if c.args and c.args[0] == "loop.detected"]
        assert len(loop_events) >= 1, (
            "Expected at least one loop.detected audit event; "
            f"warning calls: {[c.args[0] for c in warning_calls if c.args]}"
        )

    async def test_loop_halt_wraps_loop_detected_error(self) -> None:
        """LoopHaltError.__cause__ must be the underlying LoopDetectedError."""
        loop_det = LoopDetector(max_agent_steps=1, max_token_velocity=99_999)
        adapter, guardrails, policy_engine, session_mgr = _make_orchestrator_mocks()
        orchestrator = Orchestrator(
            adapter=adapter,
            guardrails=guardrails,
            policy_engine=policy_engine,
            session_mgr=session_mgr,
            loop_detector=loop_det,
        )

        loop_sid = uuid4()
        loop_det.create_context(loop_sid, agent_type="finance")

        request = OrchestratorRequest(
            prompt="Hello.",
            agent_type="finance",
            requester_id="test-user-loop-003",
            loop_session_id=loop_sid,
            loop_signal=LoopSignal.NO_PROGRESS,
        )

        with pytest.raises(LoopHaltError) as exc_info:
            await orchestrator.run(request)

        assert isinstance(exc_info.value.__cause__, LoopDetectedError), (
            "LoopHaltError.__cause__ must be a LoopDetectedError"
        )

    async def test_no_loop_check_when_session_id_absent(self) -> None:
        """When loop_session_id is None, loop detector is inactive and run() succeeds."""
        loop_det = LoopDetector(max_agent_steps=1, max_token_velocity=99_999)
        adapter, guardrails, policy_engine, session_mgr = _make_orchestrator_mocks()
        orchestrator = Orchestrator(
            adapter=adapter,
            guardrails=guardrails,
            policy_engine=policy_engine,
            session_mgr=session_mgr,
            loop_detector=loop_det,
        )

        request = OrchestratorRequest(
            prompt="Simple query.",
            agent_type="finance",
            requester_id="test-user-loop-004",
            # loop_session_id intentionally absent
        )

        result = await orchestrator.run(request)
        assert result.response.content == "step response"


# ---------------------------------------------------------------------------
# W1-2 Test 5: Negative test — HUMAN_REQUIRED enters PendingApproval state
# ---------------------------------------------------------------------------


class TestHumanRequiredSignal:
    """HUMAN_REQUIRED must raise PendingApprovalError, not LoopDetectedError."""

    def test_human_required_raises_pending_approval_error(
        self, detector_3: LoopDetector
    ) -> None:
        """PendingApprovalError is raised when signal is HUMAN_REQUIRED."""
        sid = uuid4()
        detector_3.create_context(sid, agent_type="finance")

        with pytest.raises(PendingApprovalError):
            detector_3.record_step(sid, token_delta=10, signal=LoopSignal.HUMAN_REQUIRED)

    def test_human_required_does_not_raise_loop_detected_error(
        self, detector_3: LoopDetector
    ) -> None:
        """PendingApprovalError must not be a LoopDetectedError."""
        sid = uuid4()
        detector_3.create_context(sid, agent_type="hr")

        try:
            detector_3.record_step(sid, token_delta=10, signal=LoopSignal.HUMAN_REQUIRED)
        except PendingApprovalError:
            pass
        except LoopDetectedError:
            pytest.fail("LoopDetectedError raised; expected PendingApprovalError")

    def test_intervention_required_flag_set(self, detector_3: LoopDetector) -> None:
        """ExecutionContext.intervention_required must be True after HUMAN_REQUIRED."""
        sid = uuid4()
        detector_3.create_context(sid, agent_type="it")
        try:
            detector_3.record_step(sid, token_delta=10, signal=LoopSignal.HUMAN_REQUIRED)
        except PendingApprovalError:
            pass
        ctx = detector_3.get_context(sid)
        assert ctx is not None
        assert ctx.intervention_required is True

    def test_loop_detected_flag_not_set_on_human_required(
        self, detector_3: LoopDetector
    ) -> None:
        """loop_detected must remain False on HUMAN_REQUIRED (it is not an error condition)."""
        sid = uuid4()
        detector_3.create_context(sid, agent_type="legal")
        try:
            detector_3.record_step(sid, token_delta=10, signal=LoopSignal.HUMAN_REQUIRED)
        except PendingApprovalError:
            pass
        ctx = detector_3.get_context(sid)
        assert ctx is not None
        assert ctx.loop_detected is False

    def test_human_required_after_no_progress_does_not_trip_circuit_breaker(
        self, detector_3: LoopDetector
    ) -> None:
        """Two NO_PROGRESS steps then HUMAN_REQUIRED → PendingApprovalError,
        not LoopDetectedError.
        """
        sid = uuid4()
        detector_3.create_context(sid, agent_type="finance")
        detector_3.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)
        detector_3.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)

        with pytest.raises(PendingApprovalError):
            detector_3.record_step(sid, token_delta=10, signal=LoopSignal.HUMAN_REQUIRED)

    async def test_orchestrator_raises_loop_approval_error_on_human_required(self) -> None:
        """Orchestrator wraps PendingApprovalError as LoopApprovalError."""
        loop_det = LoopDetector(max_agent_steps=10, max_token_velocity=99_999)
        adapter, guardrails, policy_engine, session_mgr = _make_orchestrator_mocks()
        orchestrator = Orchestrator(
            adapter=adapter,
            guardrails=guardrails,
            policy_engine=policy_engine,
            session_mgr=session_mgr,
            loop_detector=loop_det,
        )

        loop_sid = uuid4()
        loop_det.create_context(loop_sid, agent_type="finance")

        request = OrchestratorRequest(
            prompt="Needs human review.",
            agent_type="finance",
            requester_id="test-user-approval-001",
            loop_session_id=loop_sid,
            loop_signal=LoopSignal.HUMAN_REQUIRED,
        )

        with pytest.raises(LoopApprovalError) as exc_info:
            await orchestrator.run(request)

        assert isinstance(exc_info.value.__cause__, PendingApprovalError), (
            "LoopApprovalError.__cause__ must be a PendingApprovalError"
        )

    async def test_orchestrator_approval_error_not_loop_halt_error(self) -> None:
        """LoopApprovalError and LoopHaltError are distinct exception types."""
        loop_det = LoopDetector(max_agent_steps=10, max_token_velocity=99_999)
        adapter, guardrails, policy_engine, session_mgr = _make_orchestrator_mocks()
        orchestrator = Orchestrator(
            adapter=adapter,
            guardrails=guardrails,
            policy_engine=policy_engine,
            session_mgr=session_mgr,
            loop_detector=loop_det,
        )

        loop_sid = uuid4()
        loop_det.create_context(loop_sid, agent_type="finance")

        request = OrchestratorRequest(
            prompt="Needs human review — distinct exception check.",
            agent_type="finance",
            requester_id="test-user-approval-002",
            loop_session_id=loop_sid,
            loop_signal=LoopSignal.HUMAN_REQUIRED,
        )

        try:
            await orchestrator.run(request)
        except LoopHaltError:
            pytest.fail("LoopHaltError raised; expected LoopApprovalError")
        except LoopApprovalError:
            pass  # expected


# ---------------------------------------------------------------------------
# W-prep-2: LoopDetector Temporal serialization primitives
# ---------------------------------------------------------------------------


class TestLoopDetectorSerialization:
    """LoopDetector.checkpoint / LoopDetector.restore round-trip contract (W-prep-2)."""

    def test_checkpoint_restore_full_roundtrip(self) -> None:
        """All context fields and steps must survive the checkpoint → restore cycle."""
        det = LoopDetector(max_agent_steps=10, max_token_velocity=1_000)
        sid = uuid4()
        det.create_context(sid, agent_type="finance")
        det.record_step(sid, token_delta=50, signal=LoopSignal.PROGRESS)
        det.record_step(sid, token_delta=30, signal=LoopSignal.NO_PROGRESS)

        snapshot = det.checkpoint(sid)

        det2 = LoopDetector(max_agent_steps=10, max_token_velocity=1_000)
        ctx = det2.restore(snapshot)

        assert ctx.session_id == sid
        assert ctx.agent_type == "finance"
        assert ctx.total_tokens == 80
        assert len(ctx.steps) == 2
        assert ctx.steps[0].signal == LoopSignal.PROGRESS
        assert ctx.steps[1].signal == LoopSignal.NO_PROGRESS
        assert not ctx.loop_detected

    def test_step_counter_survives_serialize_cycle(self) -> None:
        """NO_PROGRESS streak continues from the restored context — circuit trips on step 3."""
        det = LoopDetector(max_agent_steps=3, max_token_velocity=1_000)
        sid = uuid4()
        det.create_context(sid, agent_type="it")
        det.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)
        det.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)

        snapshot = det.checkpoint(sid)

        det2 = LoopDetector(max_agent_steps=3, max_token_velocity=1_000)
        restored_ctx = det2.restore(snapshot)

        # Two NO_PROGRESS steps already in the context — the third must trip the circuit.
        with pytest.raises(LoopDetectedError):
            det2.record_step(restored_ctx.session_id, token_delta=10, signal=LoopSignal.NO_PROGRESS)

    def test_checkpoint_missing_session_raises_key_error(self) -> None:
        """checkpoint() must raise KeyError for an unknown session_id."""
        det = LoopDetector()
        with pytest.raises(KeyError):
            det.checkpoint(uuid4())

    def test_restore_replaces_existing_context(self) -> None:
        """Restoring a snapshot for an existing session_id replaces the old context."""
        det = LoopDetector(max_agent_steps=10, max_token_velocity=1_000)
        sid = uuid4()
        det.create_context(sid, agent_type="hr")
        det.record_step(sid, token_delta=99, signal=LoopSignal.NO_PROGRESS)

        # Take a snapshot before the bad step is recorded.
        snapshot = det.checkpoint(sid)

        # Add more steps to the live context.
        det.record_step(sid, token_delta=1, signal=LoopSignal.PROGRESS)

        # Restore from the earlier snapshot — should have only 1 step.
        restored_ctx = det.restore(snapshot)
        assert len(restored_ctx.steps) == 1
        assert restored_ctx.steps[0].token_delta == 99


