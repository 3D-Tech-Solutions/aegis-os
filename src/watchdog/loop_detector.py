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

"""Loop Detector — circuit breaker that halts runaway agent executions.

The circuit breaker trips on two independent conditions:

* **Step-count breach**: the trailing count of ``NO_PROGRESS`` steps reaches
  ``max_agent_steps`` — raises :exc:`LoopDetectedError`.
* **Token-velocity breach**: a single step consumes more tokens than
  ``max_token_velocity`` — raises :exc:`TokenVelocityError`.

A ``HUMAN_REQUIRED`` signal is handled separately: it raises
:exc:`PendingApprovalError` to enter a supervised hold rather than
terminating the loop as a detected error.

A ``PROGRESS`` signal resets the trailing NO_PROGRESS counter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from src.audit_vault.logger import AuditLogger
from src.config import settings

_logger = AuditLogger(component="watchdog.loop_detector")


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


class LoopSignal(StrEnum):
    """Outcome signal supplied by the caller after each agent step."""

    PROGRESS = "progress"
    NO_PROGRESS = "no_progress"
    HUMAN_REQUIRED = "human_intervention_required"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LoopDetectedError(Exception):
    """Raised when consecutive NO_PROGRESS signals breach ``max_agent_steps``."""


class TokenVelocityError(Exception):
    """Raised when a single step's token count exceeds ``max_token_velocity``."""


class PendingApprovalError(Exception):
    """Raised when the agent signals that human intervention is required.

    This is **not** a circuit-breaker termination — the orchestrator should
    pause execution and await an external approval rather than treating the
    condition as an error.
    """


# ---------------------------------------------------------------------------
# LoopDetector
# ---------------------------------------------------------------------------


class LoopDetector:
    """Detects infinite loops and excessive token burn in agent executions.

    Parameters
    ----------
    max_agent_steps:
        Maximum consecutive ``NO_PROGRESS`` steps before the circuit trips.
        Defaults to ``settings.max_agent_steps``.
    max_token_velocity:
        Maximum tokens a single step may consume before ``TokenVelocityError``
        is raised.  Defaults to ``settings.max_token_velocity``.
    audit_logger:
        Optional injectable :class:`~src.audit_vault.logger.AuditLogger`.
        When omitted the module-level ``_logger`` is used.  Inject a mock in
        tests to capture emitted audit events.
    """

    def __init__(
        self,
        max_agent_steps: int | None = None,
        max_token_velocity: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        self._max_agent_steps: int = (
            max_agent_steps if max_agent_steps is not None else settings.max_agent_steps
        )
        self._max_token_velocity: int = (
            max_token_velocity if max_token_velocity is not None else settings.max_token_velocity
        )
        self._contexts: dict[UUID, ExecutionContext] = {}
        self._audit: AuditLogger = audit_logger if audit_logger is not None else _logger

    def create_context(self, session_id: UUID, agent_type: str) -> ExecutionContext:
        """Create a new execution context for loop tracking."""
        ctx = ExecutionContext(session_id=session_id, agent_type=agent_type)
        self._contexts[session_id] = ctx
        self._audit.info(
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
        """Record an agent step and check for circuit-breaker conditions.

        The checks run in this order:

        1. **Token-velocity check** — raises :exc:`TokenVelocityError` if
           *token_delta* exceeds ``max_token_velocity``.  The circuit is tripped
           regardless of the step signal.
        2. **HUMAN_REQUIRED** — raises :exc:`PendingApprovalError` immediately;
           the NO_PROGRESS streak counter is not updated.
        3. **Step-count check** — raises :exc:`LoopDetectedError` when the
           trailing NO_PROGRESS streak equals or exceeds ``max_agent_steps``.

        Parameters
        ----------
        session_id:
            The execution context to update.
        token_delta:
            Tokens consumed during this step.
        signal:
            Outcome signal for this step.  Defaults to ``NO_PROGRESS``.
        description:
            Optional human-readable description of the step outcome.

        Raises
        ------
        TokenVelocityError
            Single-step token count exceeds ``max_token_velocity``.
        PendingApprovalError
            The agent requests human intervention.
        LoopDetectedError
            The NO_PROGRESS streak has reached ``max_agent_steps``.
        KeyError
            No execution context exists for *session_id*.
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

        # ----------------------------------------------------------------
        # Check 1 — token velocity (highest priority; fires regardless of signal)
        # ----------------------------------------------------------------
        if token_delta > self._max_token_velocity:
            msg = (
                f"Token velocity exceeded on step {step_number}: "
                f"{token_delta} > {self._max_token_velocity}"
            )
            ctx.loop_detected = True
            self._audit.warning(
                "loop.detected",
                reason="token_velocity_exceeded",
                session_id=str(session_id),
                agent_type=ctx.agent_type,
                step=step_number,
                token_delta=token_delta,
                max_token_velocity=self._max_token_velocity,
            )
            raise TokenVelocityError(msg)

        # ----------------------------------------------------------------
        # Check 2 — human intervention requested
        # ----------------------------------------------------------------
        if signal == LoopSignal.HUMAN_REQUIRED:
            ctx.intervention_required = True
            self._audit.warning(
                "loop.pending_approval",
                session_id=str(session_id),
                agent_type=ctx.agent_type,
                step=step_number,
            )
            raise PendingApprovalError(
                f"Agent requested human intervention on step {step_number}"
            )

        # ----------------------------------------------------------------
        # Check 3 — NO_PROGRESS streak
        # ----------------------------------------------------------------
        no_progress_streak = self._no_progress_streak(ctx.steps)
        if no_progress_streak >= self._max_agent_steps:
            msg = (
                f"No progress after {no_progress_streak} consecutive steps "
                f"(max={self._max_agent_steps})"
            )
            ctx.loop_detected = True
            self._audit.warning(
                "loop.detected",
                reason="no_progress_streak",
                session_id=str(session_id),
                agent_type=ctx.agent_type,
                step=step_number,
                streak=no_progress_streak,
                max_agent_steps=self._max_agent_steps,
            )
            raise LoopDetectedError(msg)

        return ctx

    def get_context(self, session_id: UUID) -> ExecutionContext | None:
        """Return the execution context for *session_id*, or ``None`` if absent."""
        return self._contexts.get(session_id)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_context(self, session_id: UUID) -> ExecutionContext:
        """Return the execution context or raise ``KeyError`` if absent."""
        ctx = self._contexts.get(session_id)
        if ctx is None:
            raise KeyError(f"Execution context {session_id} not found")
        return ctx

    @staticmethod
    def _no_progress_streak(steps: list[StepRecord]) -> int:
        """Count consecutive trailing NO_PROGRESS steps.

        Iterates the step list in reverse and stops at the first ``PROGRESS``
        or ``HUMAN_REQUIRED`` step (both reset the streak).
        """
        streak = 0
        for step in reversed(steps):
            if step.signal in (LoopSignal.PROGRESS, LoopSignal.HUMAN_REQUIRED):
                break
            streak += 1
        return streak
