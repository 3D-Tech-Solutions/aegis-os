"""Loop Detector - detects infinite loops and token-burn in agent executions."""

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from src.audit_vault.logger import AuditLogger
from src.config import settings

_logger = AuditLogger(component="watchdog.loop_detector")


class LoopSignal(StrEnum):
    PROGRESS = "progress"
    NO_PROGRESS = "no_progress"
    HUMAN_REQUIRED = "human_intervention_required"


@dataclass
class StepRecord:
    """Records metadata about a single agent step."""

    step_number: int
    token_delta: int
    signal: LoopSignal
    description: str = ""


@dataclass
class ExecutionContext:
    """Tracks execution state for loop detection."""

    session_id: UUID
    agent_type: str
    steps: list[StepRecord] = field(default_factory=list)
    total_tokens: int = 0
    loop_detected: bool = False
    intervention_required: bool = False


class LoopDetectedError(Exception):
    """Raised when a looping execution pattern is detected."""


class LoopDetector:
    """Detects infinite loops and excessive token burn in agent executions.

    The circuit breaker triggers when:
    - The step count exceeds ``settings.max_agent_steps`` without a PROGRESS signal, or
    - The token velocity (tokens per step) exceeds ``settings.max_token_velocity``.
    """

    def __init__(self) -> None:
        self._contexts: dict[UUID, ExecutionContext] = {}

    def create_context(self, session_id: UUID, agent_type: str) -> ExecutionContext:
        """Create a new execution context for loop tracking."""
        ctx = ExecutionContext(session_id=session_id, agent_type=agent_type)
        self._contexts[session_id] = ctx
        _logger.info(
            "loop_detector.context_created",
            session_id=str(session_id),
            agent_type=agent_type,
        )
        return ctx

    def record_step(
        self,
        session_id: UUID,
        token_delta: int,
        signal: LoopSignal = LoopSignal.NO_PROGRESS,
        description: str = "",
    ) -> ExecutionContext:
        """Record an agent step and check for loop conditions.

        Raises:
            LoopDetectedError: if the circuit breaker triggers.
        """
        ctx = self._get_context(session_id)
        step_number = len(ctx.steps) + 1
        ctx.steps.append(
            StepRecord(
                step_number=step_number,
                token_delta=token_delta,
                signal=signal,
                description=description,
            )
        )
        ctx.total_tokens += token_delta

        # Check token velocity
        if token_delta > settings.max_token_velocity:
            msg = (
                f"Token velocity exceeded on step {step_number}: "
                f"{token_delta} > {settings.max_token_velocity}"
            )
            ctx.loop_detected = True
            ctx.intervention_required = True
            _logger.warning(
                "loop_detector.velocity_exceeded",
                session_id=str(session_id),
                step=step_number,
            )
            raise LoopDetectedError(msg)

        # Check step count without progress
        no_progress_streak = self._no_progress_streak(ctx.steps)
        if no_progress_streak >= settings.max_agent_steps:
            msg = (
                f"No progress signal after {no_progress_streak} steps. "
                "Human intervention required."
            )
            ctx.loop_detected = True
            ctx.intervention_required = True
            _logger.warning(
                "loop_detector.no_progress",
                session_id=str(session_id),
                step=step_number,
                streak=no_progress_streak,
            )
            raise LoopDetectedError(msg)

        return ctx

    def get_context(self, session_id: UUID) -> ExecutionContext | None:
        return self._contexts.get(session_id)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_context(self, session_id: UUID) -> ExecutionContext:
        ctx = self._contexts.get(session_id)
        if ctx is None:
            raise KeyError(f"Execution context {session_id} not found")
        return ctx

    @staticmethod
    def _no_progress_streak(steps: list[StepRecord]) -> int:
        """Count consecutive trailing steps without a PROGRESS signal."""
        streak = 0
        for step in reversed(steps):
            if step.signal == LoopSignal.PROGRESS:
                break
            streak += 1
        return streak
