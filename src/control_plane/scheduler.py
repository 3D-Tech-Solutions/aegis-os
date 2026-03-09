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

"""Control Plane Scheduler — manages Temporal workflows for durable agent execution.

Phase 2 (P2-1): Implements ``AgentTaskWorkflow`` as a production Temporal workflow
with five activities mapped 1:1 to the Aegis Governance Loop stages::

    PrePIIScrub → PolicyEval → JITTokenIssue → LLMInvoke → PostSanitize

Phase 2 (P2-2): ``LLM_RETRY_POLICY`` applies exponential backoff (initial 1 s,
coefficient 2.0, max 5 attempts) to ``LLMInvoke``.  When all attempts are
exhausted the workflow raises :exc:`HITLEscalationError`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID, uuid4

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Status, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

from src.adapters.base import BaseAdapter, LLMRequest
from src.audit_vault.logger import AuditLogger, LifecycleEvent
from src.governance.guardrails import Guardrails
from src.governance.policy_engine.opa_client import OpaUnavailableError, PolicyEngine, PolicyInput
from src.governance.session_mgr import SessionManager
from src.watchdog.budget_enforcer import (
    BudgetEnforcer,
    BudgetExceededError,
    BudgetHistoryEntry,
    BudgetSessionSnapshot,
)
from src.watchdog.loop_detector import (
    ExecutionCheckpoint,
    LoopDetectedError,
    LoopDetector,
    LoopSignal,
    PendingApprovalError,
    TokenVelocityError,
)

_logger = AuditLogger()


# ---------------------------------------------------------------------------
# Workflow status
# ---------------------------------------------------------------------------


class WorkflowStatus(StrEnum):
    """Lifecycle states for a managed agent workflow."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    HUMAN_INTERVENTION_REQUIRED = "human_intervention_required"


class PendingApprovalState(StrEnum):
    """Durable review states used for budget-extension HITL workflows."""

    NOT_REQUIRED = "not-required"
    AWAITING_APPROVAL = "awaiting-approval"
    APPROVED = "approved"
    DENIED = "denied"
    TIMED_OUT = "timed-out"


@dataclass(frozen=True)
class ApprovalSignalPayload:
    """Decision metadata supplied through approve/deny signals."""

    approver_id: str
    reason: str
    approved: bool


@dataclass(frozen=True)
class ApprovalStatusSnapshot:
    """Queryable workflow snapshot used by approval endpoints."""

    task_id: str
    session_id: str | None
    agent_type: str
    workflow_status: str
    approval_state: str
    pending_since_epoch_seconds: float | None = None


@dataclass
class WorkflowHandle:
    """In-memory tracking record for a scheduled workflow."""

    workflow_id: UUID = field(default_factory=uuid4)
    status: WorkflowStatus = WorkflowStatus.PENDING
    agent_type: str = "general"
    task_description: str = ""
    step_count: int = 0
    token_count: int = 0


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class RateLimitError(Exception):
    """Raised when an LLM provider returns HTTP 429 (Too Many Requests).

    The ``LLMInvoke`` activity raises this exception when the upstream provider
    signals rate-limiting.  It is recognised as retryable by ``LLM_RETRY_POLICY``
    and will be retried up to ``LLM_RETRY_POLICY.maximum_attempts`` times with
    exponential backoff.
    """


class HITLEscalationError(Exception):
    """Raised when ``LLMInvoke`` exhausts all retry attempts without success.

    The workflow catches :exc:`~temporalio.exceptions.ActivityError` from an
    exhausted activity and re-raises this exception.  Callers must surface it as
    a ``human_intervention_required`` status and emit a ``workflow.hitl_required``
    audit event rather than treating it as a terminal failure.
    """


class MissingTaskIdError(ValueError):
    """Raised when workflow activity input loses its required ``task_id``."""


# ---------------------------------------------------------------------------
# Activity I/O dataclasses
# ---------------------------------------------------------------------------


@dataclass
class WorkflowInput:
    """Input record submitted when starting an ``AgentTaskWorkflow``."""

    task_id: str
    prompt: str
    agent_type: str
    requester_id: str
    model: str = "gpt-4o-mini"
    max_tokens: int = 1024
    temperature: float = 0.7
    system_prompt: str = ""
    protect_outbound_request: bool = False
    session_id: str | None = None
    budget_session_id: str | None = None
    budget_limit_usd: str | None = None
    cost_per_token_usd: str = str(BudgetEnforcer.DEFAULT_COST_PER_TOKEN)
    loop_session_id: str | None = None
    loop_signal: LoopSignal = LoopSignal.NO_PROGRESS
    loop_token_delta: int | None = None
    loop_max_agent_steps: int | None = None
    loop_max_token_velocity: int | None = None
    projected_spend_usd: str = "0.00"
    approval_timeout_seconds: int = 86_400
    traceparent: str | None = None


@dataclass
class PrePIIScrubResult:
    """Output of the ``PrePIIScrub`` activity."""

    sanitized_prompt: str
    pii_types: list[str]


@dataclass
class PolicyEvalInput:
    """Input to the ``PolicyEval`` activity."""

    task_id: str
    sanitized_prompt: str
    agent_type: str
    requester_id: str
    model: str
    traceparent: str | None = None


@dataclass
class PolicyEvalResult:
    """Output of the ``PolicyEval`` activity.

    ``sanitized_prompt`` reflects any additional masking applied when
    ``action == "mask"``; it equals the input prompt when no masking occurred.
    """

    allowed: bool
    action: str
    fields: list[str]
    sanitized_prompt: str
    extra_pii_types: list[str]


@dataclass
class JITTokenInput:
    """Input to the ``JITTokenIssue`` activity."""

    agent_type: str
    requester_id: str
    task_id: str
    protect_outbound_request: bool = False
    session_id: str | None = None
    allowed_actions: tuple[str, ...] = ("llm:complete",)
    role: str | None = None
    rotation_key: str | None = None
    traceparent: str | None = None


@dataclass
class JITTokenResult:
    """Output of the ``JITTokenIssue`` activity."""

    token: str
    jti: str
    protected_private_key_pem: str | None = None
    protected_public_jwk: dict[str, str] | None = None


@dataclass
class LLMInvokeInput:
    """Input to the ``LLMInvoke`` activity."""

    task_id: str
    token: str
    sanitized_prompt: str
    agent_type: str
    requester_id: str
    model: str
    max_tokens: int
    temperature: float
    system_prompt: str
    protect_outbound_request: bool = False
    session_id: str | None = None
    allowed_actions: tuple[str, ...] = ("llm:complete",)
    role: str | None = None
    rotation_key: str | None = None
    protected_private_key_pem: str | None = None
    protected_public_jwk: dict[str, str] | None = None
    traceparent: str | None = None


@dataclass
class LLMInvokeResult:
    """Output of the ``LLMInvoke`` activity."""

    content: str
    tokens_used: int
    model: str
    provider: str
    retry_count: int = 0


@dataclass
class PostSanitizeInput:
    """Input to the ``PostSanitize`` activity."""

    task_id: str
    agent_type: str
    content: str
    tokens_used: int
    model: str
    provider: str
    session_id: str | None = None
    total_retries: int = 0
    traceparent: str | None = None


@dataclass
class PostSanitizeResult:
    """Output of the ``PostSanitize`` activity."""

    sanitized_content: str
    pii_types: list[str]


@dataclass
class WorkflowOutput:
    """Output returned when an ``AgentTaskWorkflow`` completes."""

    task_id: str
    content: str
    sanitized_prompt: str
    pii_types: list[str]
    tokens_used: int
    model: str
    workflow_status: str = "completed"
    approval_state: str = PendingApprovalState.NOT_REQUIRED.value
    approver_id: str | None = None
    reason: str | None = None
    budget_spent_usd: str | None = None


@dataclass
class WorkflowAuditInput:
    """Serializable payload for workflow-level audit events."""

    event: str
    outcome: str
    stage: str
    task_id: str
    agent_type: str
    session_id: str | None = None
    workflow_status: str = WorkflowStatus.RUNNING.value
    event_type: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    traceparent: str | None = None


@dataclass
class BudgetPreCheckInput:
    """Input to the ``BudgetPreCheck`` activity."""

    task_id: str
    agent_type: str
    budget_session_id: str
    budget_limit_usd: str
    history: list[BudgetHistoryEntry] = field(default_factory=list)
    traceparent: str | None = None


@dataclass
class BudgetPreCheckResult:
    """Output of the ``BudgetPreCheck`` activity."""

    snapshot: BudgetSessionSnapshot


@dataclass
class BudgetRecordInput:
    """Input to the ``BudgetRecordSpend`` activity."""

    task_id: str
    agent_type: str
    budget_session_id: str
    budget_limit_usd: str
    tokens_used: int
    cost_per_token_usd: str
    history: list[BudgetHistoryEntry] = field(default_factory=list)
    traceparent: str | None = None


@dataclass
class BudgetRecordResult:
    """Output of the ``BudgetRecordSpend`` activity."""

    snapshot: BudgetSessionSnapshot
    history: list[BudgetHistoryEntry]


@dataclass
class LoopRecordInput:
    """Input to the ``LoopRecordStep`` activity."""

    task_id: str
    agent_type: str
    loop_session_id: str
    token_delta: int
    signal: LoopSignal = LoopSignal.NO_PROGRESS
    description: str = ""
    checkpoint: ExecutionCheckpoint | None = None
    max_agent_steps: int | None = None
    max_token_velocity: int | None = None
    traceparent: str | None = None


@dataclass
class LoopRecordResult:
    """Output of the ``LoopRecordStep`` activity."""

    checkpoint: ExecutionCheckpoint
    step_count: int
    total_tokens: int
    loop_detected: bool
    intervention_required: bool


# ---------------------------------------------------------------------------
# Retry policy (P2-2)
# ---------------------------------------------------------------------------

#: Retry policy for the ``LLMInvoke`` activity.
#:
#: * Initial interval: 1 s
#: * Backoff coefficient: 2.0 (doubling delay each attempt)
#: * Maximum attempts: 5
#: * Non-retryable: ``PolicyDeniedError``, ``TokenScopeError``,
#:   ``TokenExpiredError``, ``HITLEscalationError``
LLM_RETRY_POLICY: RetryPolicy = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_attempts=5,
    non_retryable_error_types=[
        "PolicyDeniedError",
        "TokenScopeError",
        "TokenExpiredError",
        "HITLEscalationError",
    ],
)


# ---------------------------------------------------------------------------
# Activity implementations (AegisActivities — P2-1)
# ---------------------------------------------------------------------------


class AegisActivities:
    """Temporal activity implementations for the Aegis Governance Loop.

    Each public method is decorated with ``@activity.defn(name=<stage_name>)``
    and maps 1:1 to one of the five Aegis Governance Loop pipeline stages.
    Instances are constructed with all required dependencies injected —
    activities never instantiate their own dependencies.

    The ``AgentTaskWorkflow`` workflow class exposes class-level references to
    each activity method so that ``hasattr(AgentTaskWorkflow, 'pre_pii_scrub')``
    returns ``True`` and ``inspect.getsource()`` resolves to the real
    implementation rather than a stub.
    """

    def __init__(
        self,
        adapter: BaseAdapter,
        guardrails: Guardrails | None = None,
        policy_engine: PolicyEngine | None = None,
        session_mgr: SessionManager | None = None,
        budget_enforcer: BudgetEnforcer | None = None,
        loop_detector: LoopDetector | None = None,
        audit_logger: AuditLogger | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        """Initialise the activities class with injectable dependencies.

        Args:
            adapter: LLM adapter used by the ``LLMInvoke`` activity.
            guardrails: PII scrubbing / injection detection; defaults to a
                fresh ``Guardrails()`` instance when *None*.
            policy_engine: OPA policy evaluation client; defaults to a fresh
                ``PolicyEngine()`` instance when *None*.
            session_mgr: JIT token issuer/validator; defaults to a fresh
                ``SessionManager()`` instance when *None*.
            budget_enforcer: Budget accounting helper used by replay-safe
                Temporal watchdog activities. Defaults to a fresh
                ``BudgetEnforcer()`` when *None*.
            loop_detector: Loop detection helper used by replay-safe Temporal
                watchdog activities. Defaults to a fresh ``LoopDetector()``
                when *None*.
            audit_logger: Structured audit log sink; defaults to a fresh
                ``AuditLogger("activities")`` instance when *None*.
            tracer: OTel tracer used for per-stage activity spans. Defaults to
                ``trace.get_tracer("activities")`` when *None*.
        """
        self._adapter = adapter
        self._guardrails = guardrails if guardrails is not None else Guardrails()
        self._policy_engine = policy_engine if policy_engine is not None else PolicyEngine()
        self._session_mgr = session_mgr if session_mgr is not None else SessionManager()
        self._budget_enforcer = (
            budget_enforcer if budget_enforcer is not None else BudgetEnforcer(audit_logger)
        )
        self._loop_detector = loop_detector if loop_detector is not None else LoopDetector()
        self._audit = audit_logger if audit_logger is not None else AuditLogger("activities")
        self._tracer = tracer if tracer is not None else trace.get_tracer("activities")

    def _restore_budget_session(
        self,
        *,
        budget_session_id: str,
        agent_type: str,
        budget_limit_usd: str,
        history: list[BudgetHistoryEntry],
    ) -> tuple[UUID, Any]:
        """Rebuild the budget session from workflow-owned history for this activity call."""
        session_id = UUID(budget_session_id)
        restored = self._budget_enforcer.restore_from_history(
            session_id=session_id,
            agent_type=agent_type,
            budget_limit_usd=Decimal(budget_limit_usd),
            history=history,
        )
        return session_id, restored

    def _build_loop_detector(
        self,
        *,
        max_agent_steps: int | None,
        max_token_velocity: int | None,
    ) -> LoopDetector:
        """Return a detector configured for the current workflow-owned checkpoint."""
        return LoopDetector(
            max_agent_steps=max_agent_steps,
            max_token_velocity=max_token_velocity,
            audit_logger=self._audit,
        )

    @staticmethod
    def _trace_context_from_traceparent(traceparent: str | None) -> Context | None:
        """Return an extracted OTel parent context for a propagated traceparent."""
        if not traceparent:
            return None
        return TraceContextTextMapPropagator().extract({"traceparent": traceparent})

    def _require_task_id(self, task_id: str | None, *, stage: str, agent_type: str) -> str:
        """Fail loudly when Temporal activity input loses the required task id."""
        if task_id:
            return task_id
        self._audit.error(
            "audit.propagation_error",
            stage=stage,
            task_id="missing-task-id",
            agent_type=agent_type,
            error_message="Temporal activity input is missing task_id",
        )
        raise MissingTaskIdError(
            f"Temporal activity input for stage {stage!r} is missing task_id"
        )

    @staticmethod
    def _attach_span_context(span: trace.Span, *, task_id: str, agent_type: str) -> None:
        """Attach common stage attributes to a workflow activity span."""
        span.set_attribute("task_id", task_id)
        span.set_attribute("agent_type", agent_type)

    @staticmethod
    def _mark_span_success(span: trace.Span) -> None:
        """Mark a stage span as successful using the same contract as orchestrator spans."""
        span.set_attribute("span.status", "OK")
        span.set_status(Status(StatusCode.OK))

    @staticmethod
    def _mark_span_error(span: trace.Span, exc: BaseException) -> None:
        """Attach standard error attributes to a failing stage span."""
        span.set_attribute("span.status", "ERROR")
        span.set_attribute("error", True)
        span.set_attribute("error.message", str(exc))
        span.record_exception(exc)
        span.set_status(Status(StatusCode.ERROR, str(exc)))

    @activity.defn(name="PrePIIScrub")
    async def pre_pii_scrub(self, inp: WorkflowInput) -> PrePIIScrubResult:
        """Strip PII from the inbound prompt and check for injection attempts.

        Normalises the prompt through the adversarial pre-processing pipeline
        (zero-width strip → NFKC → URL-decode → ``@`` compaction) then applies
        all five PII pattern classes.

        Args:
            inp: Workflow input containing the raw prompt and task metadata.

        Returns:
            :class:`PrePIIScrubResult` with the sanitised prompt text and list
            of PII type labels discovered.

        Raises:
            PromptInjectionError: If a prompt injection attempt is detected.
        """
        task_id = self._require_task_id(
            getattr(inp, "task_id", None),
            stage="pre-pii-scrub",
            agent_type=inp.agent_type,
        )
        with self._tracer.start_as_current_span(
            "pre-pii-scrub",
            context=self._trace_context_from_traceparent(inp.traceparent),
        ) as span:
            self._attach_span_context(span, task_id=task_id, agent_type=inp.agent_type)
            try:
                self._audit.lifecycle_event(
                    "workflow.started",
                    event_type=LifecycleEvent.STARTED,
                    task_id=task_id,
                    agent_type=inp.agent_type,
                    session_id=inp.session_id,
                    workflow_status=WorkflowStatus.RUNNING.value,
                )
                self._guardrails.check_prompt_injection(inp.prompt)
                mask_result = self._guardrails.mask_pii(inp.prompt)
                self._audit.stage_event(
                    "guardrails.pre_sanitize",
                    outcome="redact" if mask_result.found_types else "allow",
                    stage="pre-pii-scrub",
                    task_id=task_id,
                    agent_type=inp.agent_type,
                    pii_types=mask_result.found_types,
                )
                self._mark_span_success(span)
                return PrePIIScrubResult(
                    sanitized_prompt=mask_result.text,
                    pii_types=mask_result.found_types,
                )
            except Exception as exc:
                self._mark_span_error(span, exc)
                raise

    @activity.defn(name="PolicyEval")
    async def policy_eval(self, inp: PolicyEvalInput) -> PolicyEvalResult:
        """Evaluate the OPA policy for the current request.

        When OPA returns ``action="mask"``, the activity applies
        :meth:`~Guardrails.scrub` to the prompt and returns the re-sanitised
        text in ``PolicyEvalResult.sanitized_prompt``.

        Args:
            inp: Policy evaluation input with agent type and sanitised prompt.

        Returns:
            :class:`PolicyEvalResult` with allow/deny decision and the
            (possibly re-masked) sanitised prompt.

        Raises:
            ApplicationError: With ``non_retryable=True`` when OPA is
                unavailable (fail-closed) or returns ``allow=False``.
        """
        task_id = self._require_task_id(
            getattr(inp, "task_id", None),
            stage="policy-eval",
            agent_type=inp.agent_type,
        )
        with self._tracer.start_as_current_span(
            "policy-eval",
            context=self._trace_context_from_traceparent(inp.traceparent),
        ) as span:
            self._attach_span_context(span, task_id=task_id, agent_type=inp.agent_type)
            try:
                policy_input = PolicyInput(
                    agent_type=inp.agent_type,
                    requester_id=inp.requester_id,
                    action="llm.complete",
                    resource=f"model:{inp.model}",
                )
                try:
                    result = await self._policy_engine.evaluate("agent_access", policy_input)
                except OpaUnavailableError as exc:
                    self._audit.stage_event(
                        "policy.opa_unavailable",
                        outcome="error",
                        stage="policy-eval",
                        task_id=task_id,
                        agent_type=inp.agent_type,
                        error_message=str(exc),
                    )
                    raise ApplicationError(
                        "OPA unavailable – request denied (fail-closed)",
                        type="PolicyDeniedError",
                        non_retryable=True,
                    ) from exc

                if not result.allowed or result.action == "reject":
                    self._audit.stage_event(
                        "policy.denied",
                        outcome="deny",
                        stage="policy-eval",
                        task_id=task_id,
                        agent_type=inp.agent_type,
                        reasons=result.reasons,
                    )
                    raise ApplicationError(
                        f"OPA denied request for agent_type={inp.agent_type!r}: {result.reasons}",
                        type="PolicyDeniedError",
                        non_retryable=True,
                    )

                self._audit.stage_event(
                    "policy.allowed",
                    outcome="allow",
                    stage="policy-eval",
                    task_id=task_id,
                    agent_type=inp.agent_type,
                )

                sanitized_prompt = inp.sanitized_prompt
                extra_pii_types: list[str] = []
                if result.action == "mask":
                    for fld in result.fields:
                        if fld == "prompt":
                            mask_result = self._guardrails.scrub(sanitized_prompt)
                            sanitized_prompt = mask_result.text
                            extra_pii_types.extend(mask_result.found_types)
                    self._audit.stage_event(
                        "policy.mask_applied",
                        outcome="redact",
                        stage="policy-eval",
                        task_id=task_id,
                        agent_type=inp.agent_type,
                        fields=result.fields,
                    )

                self._mark_span_success(span)
                return PolicyEvalResult(
                    allowed=result.allowed,
                    action=result.action,
                    fields=result.fields,
                    sanitized_prompt=sanitized_prompt,
                    extra_pii_types=extra_pii_types,
                )
            except Exception as exc:
                self._mark_span_error(span, exc)
                raise

    @activity.defn(name="BudgetPreCheck")
    async def budget_pre_check(self, inp: BudgetPreCheckInput) -> BudgetPreCheckResult:
        """Restore budget state from workflow history and fail closed if exhausted."""
        task_id = self._require_task_id(
            getattr(inp, "task_id", None),
            stage="watchdog.pre-llm",
            agent_type=inp.agent_type,
        )
        with self._tracer.start_as_current_span(
            "watchdog.pre-llm",
            context=self._trace_context_from_traceparent(inp.traceparent),
        ) as span:
            self._attach_span_context(span, task_id=task_id, agent_type=inp.agent_type)
            try:
                session_id, restored = self._restore_budget_session(
                    budget_session_id=inp.budget_session_id,
                    agent_type=inp.agent_type,
                    budget_limit_usd=inp.budget_limit_usd,
                    history=inp.history,
                )
                self._budget_enforcer.check_budget(session_id)
                self._audit.stage_event(
                    "budget.pre_check",
                    outcome="allow",
                    stage="watchdog.pre-llm",
                    task_id=task_id,
                    agent_type=inp.agent_type,
                    budget_session_id=inp.budget_session_id,
                    spent_usd=str(restored.cost_usd),
                )
                self._mark_span_success(span)
                return BudgetPreCheckResult(snapshot=restored.serialize())
            except BudgetExceededError as exc:
                self._audit.stage_event(
                    "budget.exceeded",
                    outcome="deny",
                    stage="watchdog.pre-llm",
                    task_id=task_id,
                    agent_type=inp.agent_type,
                    budget_session_id=inp.budget_session_id,
                    error_message=str(exc),
                )
                self._mark_span_error(span, exc)
                raise ApplicationError(
                    str(exc),
                    type="BudgetExceededError",
                    non_retryable=True,
                ) from exc
            except Exception as exc:
                self._mark_span_error(span, exc)
                raise

    @activity.defn(name="BudgetRecordSpend")
    async def budget_record_spend(self, inp: BudgetRecordInput) -> BudgetRecordResult:
        """Replay budget history, record spend once, and return the updated ledger."""
        task_id = self._require_task_id(
            getattr(inp, "task_id", None),
            stage="watchdog.record-spend",
            agent_type=inp.agent_type,
        )
        with self._tracer.start_as_current_span(
            "watchdog.record-spend",
            context=self._trace_context_from_traceparent(inp.traceparent),
        ) as span:
            self._attach_span_context(span, task_id=task_id, agent_type=inp.agent_type)
            try:
                info = activity.info()
                operation_id = (
                    f"{info.workflow_id}:{info.activity_id}"
                    if info.workflow_id is not None
                    else info.activity_id
                )
                session_id, restored = self._restore_budget_session(
                    budget_session_id=inp.budget_session_id,
                    agent_type=inp.agent_type,
                    budget_limit_usd=inp.budget_limit_usd,
                    history=inp.history,
                )
                existing_operations = set(restored.applied_operation_ids)
                session = self._budget_enforcer.record_tokens(
                    session_id,
                    tokens=inp.tokens_used,
                    cost_per_token=Decimal(inp.cost_per_token_usd),
                    operation_id=operation_id,
                )
                history = list(inp.history)
                if operation_id not in existing_operations:
                    amount_usd = Decimal(inp.cost_per_token_usd) * Decimal(inp.tokens_used)
                    history.append(
                        BudgetHistoryEntry(
                            operation_id=operation_id,
                            amount_usd=str(amount_usd),
                            tokens_used=inp.tokens_used,
                        )
                    )
                self._audit.stage_event(
                    "budget.spend_recorded",
                    outcome="allow",
                    stage="watchdog.record-spend",
                    task_id=task_id,
                    agent_type=inp.agent_type,
                    budget_session_id=inp.budget_session_id,
                    spent_usd=str(session.cost_usd),
                )
                self._mark_span_success(span)
                return BudgetRecordResult(snapshot=session.serialize(), history=history)
            except BudgetExceededError as exc:
                self._audit.stage_event(
                    "budget.exceeded_on_record",
                    outcome="deny",
                    stage="watchdog.record-spend",
                    task_id=task_id,
                    agent_type=inp.agent_type,
                    budget_session_id=inp.budget_session_id,
                    error_message=str(exc),
                )
                self._mark_span_error(span, exc)
                raise ApplicationError(
                    str(exc),
                    type="BudgetExceededError",
                    non_retryable=True,
                ) from exc
            except Exception as exc:
                self._mark_span_error(span, exc)
                raise

    @activity.defn(name="LoopRecordStep")
    async def loop_record_step(self, inp: LoopRecordInput) -> LoopRecordResult:
        """Restore loop state from a checkpoint, record one step, and return a new checkpoint."""
        task_id = self._require_task_id(
            getattr(inp, "task_id", None),
            stage="watchdog.loop-detect",
            agent_type=inp.agent_type,
        )
        with self._tracer.start_as_current_span(
            "watchdog.loop-detect",
            context=self._trace_context_from_traceparent(inp.traceparent),
        ) as span:
            self._attach_span_context(span, task_id=task_id, agent_type=inp.agent_type)
            loop_session_id = UUID(inp.loop_session_id)
            detector = self._build_loop_detector(
                max_agent_steps=inp.max_agent_steps,
                max_token_velocity=inp.max_token_velocity,
            )
            try:
                if inp.checkpoint is None:
                    detector.create_context(loop_session_id, agent_type=inp.agent_type)
                else:
                    detector.restore(inp.checkpoint)
                ctx = detector.record_step(
                    loop_session_id,
                    token_delta=inp.token_delta,
                    signal=inp.signal,
                    description=inp.description,
                )
                self._audit.stage_event(
                    "loop.step_recorded",
                    outcome="allow",
                    stage="watchdog.loop-detect",
                    task_id=task_id,
                    agent_type=inp.agent_type,
                    step=str(len(ctx.steps)),
                    signal=inp.signal.value,
                )
                self._mark_span_success(span)
                return LoopRecordResult(
                    checkpoint=detector.checkpoint(loop_session_id),
                    step_count=len(ctx.steps),
                    total_tokens=ctx.total_tokens,
                    loop_detected=ctx.loop_detected,
                    intervention_required=ctx.intervention_required,
                )
            except LoopDetectedError as exc:
                loop_ctx = detector.get_context(loop_session_id)
                step_count = len(loop_ctx.steps) if loop_ctx is not None else 0
                total_tokens = loop_ctx.total_tokens if loop_ctx is not None else 0
                self._audit.stage_event(
                    "loop.halted",
                    outcome="deny",
                    stage="watchdog.loop-detect",
                    task_id=task_id,
                    agent_type=inp.agent_type,
                    error_message=str(exc),
                )
                self._mark_span_error(span, exc)
                raise ApplicationError(
                    f"{exc} [step_count={step_count} total_tokens={total_tokens}]",
                    type="LoopDetectedError",
                    non_retryable=True,
                ) from exc
            except TokenVelocityError as exc:
                loop_ctx = detector.get_context(loop_session_id)
                step_count = len(loop_ctx.steps) if loop_ctx is not None else 0
                total_tokens = loop_ctx.total_tokens if loop_ctx is not None else 0
                self._audit.stage_event(
                    "loop.velocity_exceeded",
                    outcome="deny",
                    stage="watchdog.loop-detect",
                    task_id=task_id,
                    agent_type=inp.agent_type,
                    error_message=str(exc),
                )
                self._mark_span_error(span, exc)
                raise ApplicationError(
                    f"{exc} [step_count={step_count} total_tokens={total_tokens}]",
                    type="TokenVelocityError",
                    non_retryable=True,
                ) from exc
            except PendingApprovalError as exc:
                loop_ctx = detector.get_context(loop_session_id)
                step_count = len(loop_ctx.steps) if loop_ctx is not None else 0
                total_tokens = loop_ctx.total_tokens if loop_ctx is not None else 0
                self._audit.stage_event(
                    "loop.pending_approval",
                    outcome="deny",
                    stage="watchdog.loop-detect",
                    task_id=task_id,
                    agent_type=inp.agent_type,
                    error_message=str(exc),
                )
                self._mark_span_error(span, exc)
                raise ApplicationError(
                    f"{exc} [step_count={step_count} total_tokens={total_tokens}]",
                    type="PendingApprovalError",
                    non_retryable=True,
                ) from exc
            except Exception as exc:
                self._mark_span_error(span, exc)
                raise

    @activity.defn(name="JITTokenIssue")
    async def jit_token_issue(self, inp: JITTokenInput) -> JITTokenResult:
        """Issue a scoped short-lived JIT session token for the current request.

        Args:
            inp: JIT token input with agent type and requester identifier.

        Returns:
            :class:`JITTokenResult` with the signed token string and its ``jti``
            claim for audit correlation.
        """
        task_id = self._require_task_id(
            getattr(inp, "task_id", None),
            stage="jit-token-issue",
            agent_type=inp.agent_type,
        )
        with self._tracer.start_as_current_span(
            "jit-token-issue",
            context=self._trace_context_from_traceparent(inp.traceparent),
        ) as span:
            self._attach_span_context(span, task_id=task_id, agent_type=inp.agent_type)
            try:
                protected_private_key_pem: str | None = None
                protected_public_jwk: dict[str, str] | None = None
                if inp.protect_outbound_request:
                    protected_private_key_pem, protected_public_jwk = (
                        self._session_mgr.generate_dpop_key_pair()
                    )
                token = self._session_mgr.issue_token(
                    agent_type=inp.agent_type,
                    requester_id=inp.requester_id,
                    session_id=inp.session_id,
                    allowed_actions=inp.allowed_actions,
                    role=inp.role,
                    rotation_key=inp.rotation_key,
                )
                claims = self._session_mgr.validate_token(token)
                self._audit.stage_event(
                    "token.issued",
                    outcome="allow",
                    stage="jit-token-issue",
                    task_id=task_id,
                    agent_type=inp.agent_type,
                    jti=claims.jti,
                )
                self._mark_span_success(span)
                return JITTokenResult(
                    token=token,
                    jti=claims.jti,
                    protected_private_key_pem=protected_private_key_pem,
                    protected_public_jwk=protected_public_jwk,
                )
            except Exception as exc:
                self._mark_span_error(span, exc)
                raise

    @activity.defn(name="LLMInvoke")
    async def llm_invoke(self, inp: LLMInvokeInput) -> LLMInvokeResult:
        """Forward the sanitised prompt to the LLM adapter.

        Emits a ``llm.retried`` audit event when the activity is executing on
        attempt two or later (``activity.info().attempt > 1``).

        Args:
            inp: LLM invocation input with sanitised prompt and JIT token.

        Returns:
            :class:`LLMInvokeResult` with the raw LLM response content and
            token-usage metadata.

        Raises:
            RateLimitError: When the LLM provider signals rate-limiting; the
                ``LLM_RETRY_POLICY`` will retry up to 5 times before escalating.
            asyncio.TimeoutError: When the adapter call exceeds its deadline.
        """
        task_id = self._require_task_id(
            getattr(inp, "task_id", None),
            stage="llm-invoke",
            agent_type=inp.agent_type,
        )
        with self._tracer.start_as_current_span(
            "llm-invoke",
            context=self._trace_context_from_traceparent(inp.traceparent),
        ) as span:
            self._attach_span_context(span, task_id=task_id, agent_type=inp.agent_type)
            try:
                info = activity.info()
                token = inp.token
                if info.attempt > 1:
                    token = self._session_mgr.issue_token(
                        agent_type=inp.agent_type,
                        requester_id=inp.requester_id,
                        session_id=inp.session_id,
                        allowed_actions=inp.allowed_actions,
                        role=inp.role,
                        rotation_key=inp.rotation_key,
                    )
                    claims = self._session_mgr.validate_token(token)
                    self._audit.stage_event(
                        "token.reissued",
                        outcome="allow",
                        stage="jit-token-issue",
                        task_id=task_id,
                        agent_type=inp.agent_type,
                        attempt=info.attempt,
                        jti=claims.jti,
                    )
                    self._audit.stage_event(
                        "llm.retried",
                        outcome="retried",
                        stage="llm-invoke",
                        task_id=task_id,
                        agent_type=inp.agent_type,
                        attempt=info.attempt,
                    )
                    self._audit.lifecycle_event(
                        "llm.retried",
                        event_type=LifecycleEvent.RETRIED,
                        task_id=task_id,
                        agent_type=inp.agent_type,
                        session_id=inp.session_id,
                        workflow_status=WorkflowStatus.RUNNING.value,
                        stage="llm-invoke",
                        retry_count=info.attempt - 1,
                    )

                adapter_token = token
                dpop_proof: str | None = None
                if inp.protect_outbound_request:
                    if inp.protected_private_key_pem is None or inp.protected_public_jwk is None:
                        raise ValueError(
                            "Protected outbound requests require DPoP key material from "
                            "JITTokenIssue"
                        )
                    preview_request = LLMRequest(
                        prompt=inp.sanitized_prompt,
                        model=inp.model,
                        max_tokens=inp.max_tokens,
                        temperature=inp.temperature,
                        system_prompt=inp.system_prompt,
                        metadata={"aegis_token": token},
                    )
                    binding = self._adapter.outbound_request_binding(preview_request)
                    if binding is None:
                        raise ValueError(
                            f"Adapter {self._adapter.provider_name!r} does not expose an outbound "
                            "request binding for protected flows"
                        )
                    http_method, http_url = binding
                    adapter_token = self._session_mgr.issue_sender_constrained_token(
                        agent_type=inp.agent_type,
                        requester_id=inp.requester_id,
                        public_jwk=inp.protected_public_jwk,
                        task_id=task_id,
                        session_id=inp.session_id,
                        allowed_actions=("llm.invoke",),
                        role=inp.role,
                        rotation_key=f"scheduler-adapter:{task_id}",
                    )
                    dpop_proof = self._session_mgr.issue_dpop_proof(
                        inp.protected_private_key_pem,
                        inp.protected_public_jwk,
                        http_method=http_method,
                        http_url=http_url,
                        access_token=adapter_token,
                    )
                    adapter_claims = self._session_mgr.validate_token(adapter_token)
                    self._audit.stage_event(
                        "token.sender_constrained_issued",
                        outcome="allow",
                        stage="jit-token-issue",
                        task_id=task_id,
                        agent_type=inp.agent_type,
                        jti=adapter_claims.jti,
                    )

                llm_request = LLMRequest(
                    prompt=inp.sanitized_prompt,
                    model=inp.model,
                    max_tokens=inp.max_tokens,
                    temperature=inp.temperature,
                    system_prompt=inp.system_prompt,
                    metadata={
                        "aegis_token": adapter_token,
                        **(
                            {
                                "aegis_dpop_proof": dpop_proof,
                                "aegis_protected": "true",
                            }
                            if dpop_proof is not None
                            else {}
                        ),
                    },
                )
                response = await self._adapter.complete(llm_request)
                self._audit.stage_event(
                    "llm.completed",
                    outcome="allow",
                    stage="llm-invoke",
                    task_id=task_id,
                    agent_type=inp.agent_type,
                    model=response.model,
                    tokens_used=response.tokens_used,
                )
                self._mark_span_success(span)
                return LLMInvokeResult(
                    content=response.content,
                    tokens_used=response.tokens_used,
                    model=response.model,
                    provider=response.provider,
                    retry_count=info.attempt - 1,
                )
            except Exception as exc:
                self._mark_span_error(span, exc)
                raise

    @activity.defn(name="PostSanitize")
    async def post_sanitize(self, inp: PostSanitizeInput) -> PostSanitizeResult:
        """Strip PII from the LLM response before returning it to the caller.

        Args:
            inp: Post-sanitise input containing the raw LLM response content
                and task metadata for audit correlation.

        Returns:
            :class:`PostSanitizeResult` with the sanitised content and any
            PII type labels discovered in the response.
        """
        task_id = self._require_task_id(
            getattr(inp, "task_id", None),
            stage="post-sanitize",
            agent_type=inp.agent_type,
        )
        with self._tracer.start_as_current_span(
            "post-sanitize",
            context=self._trace_context_from_traceparent(inp.traceparent),
        ) as span:
            self._attach_span_context(span, task_id=task_id, agent_type=inp.agent_type)
            try:
                mask_result = self._guardrails.mask_pii(inp.content)
                self._audit.stage_event(
                    "guardrails.post_sanitize",
                    outcome="redact" if mask_result.found_types else "allow",
                    stage="post-sanitize",
                    task_id=task_id,
                    agent_type=inp.agent_type,
                    pii_types=mask_result.found_types,
                )
                self._audit.lifecycle_event(
                    "workflow.completed",
                    event_type=LifecycleEvent.COMPLETED,
                    task_id=task_id,
                    agent_type=inp.agent_type,
                    session_id=inp.session_id,
                    workflow_status=WorkflowStatus.COMPLETED.value,
                    total_retries=inp.total_retries,
                )
                self._mark_span_success(span)
                return PostSanitizeResult(
                    sanitized_content=mask_result.text,
                    pii_types=mask_result.found_types,
                )
            except Exception as exc:
                self._mark_span_error(span, exc)
                raise


class WorkflowAuditActivities:
    """Replay-safe workflow audit activity wrapper for HITL transitions."""

    def __init__(self, audit_logger: AuditLogger | None = None) -> None:
        self._audit = audit_logger if audit_logger is not None else AuditLogger("workflow-audit")

    @activity.defn(name="WorkflowAudit")
    async def record_event(self, inp: WorkflowAuditInput) -> None:
        """Emit a workflow-level audit event from activity context."""
        if inp.event_type is not None:
            self._audit.lifecycle_event(
                inp.event,
                event_type=inp.event_type,
                task_id=inp.task_id,
                agent_type=inp.agent_type,
                session_id=inp.session_id,
                workflow_status=inp.workflow_status,
                stage=inp.stage,
                **inp.details,
            )
            return
        if inp.outcome == "deny":
            self._audit.warning(
                inp.event,
                task_id=inp.task_id,
                agent_type=inp.agent_type,
                **inp.details,
            )
            return
        if inp.outcome == "error":
            self._audit.error(
                inp.event,
                task_id=inp.task_id,
                agent_type=inp.agent_type,
                **inp.details,
            )
            return
        self._audit.info(
            inp.event,
            task_id=inp.task_id,
            agent_type=inp.agent_type,
            **inp.details,
        )


# ---------------------------------------------------------------------------
# Temporal workflow (P2-1)
# ---------------------------------------------------------------------------


@workflow.defn(name="AgentTaskWorkflow", sandboxed=False)
class AgentTaskWorkflow:
    """Production Temporal workflow implementing the Aegis Governance Loop.

    Executes five activities in strict order::

        PrePIIScrub → PolicyEval → JITTokenIssue → LLMInvoke → PostSanitize

    The ``LLMInvoke`` activity is governed by ``LLM_RETRY_POLICY`` (exponential
    backoff, max 5 attempts).  When all retries are exhausted the workflow catches
    :exc:`~temporalio.exceptions.ActivityError` and re-raises
    :exc:`HITLEscalationError`.

    Class-level activity references (``pre_pii_scrub``, ``policy_eval``,
    ``jit_token_issue``, ``llm_invoke``, ``post_sanitize``) point to the
    corresponding :class:`AegisActivities` unbound methods so that
    ``hasattr(AgentTaskWorkflow, 'pre_pii_scrub')`` is always ``True`` and
    ``inspect.getsource()`` resolves to the real implementation body.
    """

    ACTIVITY_NAMES: ClassVar[tuple[str, ...]] = (
        "PrePIIScrub",
        "PolicyEval",
        "JITTokenIssue",
        "LLMInvoke",
        "PostSanitize",
    )

    # Expose AegisActivities methods as class-level attributes so external
    # inspectors (hasattr, inspect.getsource) always find the real implementations.
    pre_pii_scrub: ClassVar = AegisActivities.pre_pii_scrub
    policy_eval: ClassVar = AegisActivities.policy_eval
    jit_token_issue: ClassVar = AegisActivities.jit_token_issue
    llm_invoke: ClassVar = AegisActivities.llm_invoke
    post_sanitize: ClassVar = AegisActivities.post_sanitize

    def __init__(self) -> None:
        """Initialise deterministic query/signal state for the workflow instance."""
        self._task_id: str = ""
        self._session_id: str | None = None
        self._agent_type: str = "general"
        self._workflow_status: str = WorkflowStatus.PENDING.value
        self._approval_state: PendingApprovalState = PendingApprovalState.NOT_REQUIRED
        self._approval_signal: ApprovalSignalPayload | None = None
        self._pending_since_epoch_seconds: float | None = None

    @workflow.signal(name="approve")
    def approve(self, decision: ApprovalSignalPayload) -> None:
        """Record an approval decision while the workflow is awaiting review."""
        if self._approval_state == PendingApprovalState.AWAITING_APPROVAL:
            self._approval_signal = decision

    @workflow.signal(name="deny")
    def deny(self, decision: ApprovalSignalPayload) -> None:
        """Record a denial decision while the workflow is awaiting review."""
        if self._approval_state == PendingApprovalState.AWAITING_APPROVAL:
            self._approval_signal = decision

    @workflow.query(name="approval-status")
    def approval_status(self) -> ApprovalStatusSnapshot:
        """Return the current task/session/state tuple for control-plane queries."""
        return ApprovalStatusSnapshot(
            task_id=self._task_id,
            session_id=self._session_id,
            agent_type=self._agent_type,
            workflow_status=self._workflow_status,
            approval_state=self._approval_state.value,
            pending_since_epoch_seconds=self._pending_since_epoch_seconds,
        )

    @workflow.run
    async def run(self, inp: WorkflowInput) -> WorkflowOutput:
        """Execute the five-stage Aegis Governance Loop as a durable Temporal workflow.

        Activity execution order:
        ``PrePIIScrub`` → ``PolicyEval`` → ``JITTokenIssue`` → ``LLMInvoke``
        → ``PostSanitize``.

        Args:
            inp: Workflow input containing task context, raw prompt, and LLM
                configuration.

        Returns:
            :class:`WorkflowOutput` with the sanitised LLM response and metadata.

        Raises:
            HITLEscalationError: When ``LLMInvoke`` exhausts all 5 retry
                attempts.  Callers must handle this as a human-in-the-loop
                escalation, not as a terminal failure.
        """
        task_id = inp.task_id
        self._task_id = inp.task_id
        self._session_id = inp.session_id
        self._agent_type = inp.agent_type
        self._workflow_status = WorkflowStatus.RUNNING.value
        self._pending_since_epoch_seconds = None
        budget_history: list[BudgetHistoryEntry] = []
        budget_spent_usd: str | None = None

        # Stage 1 — PrePIIScrub
        scrub_result: PrePIIScrubResult = await workflow.execute_activity(
            "PrePIIScrub",
            inp,
            result_type=PrePIIScrubResult,
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        # Stage 2 — PolicyEval
        policy_input = PolicyEvalInput(
            task_id=task_id,
            sanitized_prompt=scrub_result.sanitized_prompt,
            agent_type=inp.agent_type,
            requester_id=inp.requester_id,
            model=inp.model,
            traceparent=inp.traceparent,
        )
        policy_result: PolicyEvalResult = await workflow.execute_activity(
            "PolicyEval",
            policy_input,
            result_type=PolicyEvalResult,
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(
                maximum_attempts=1,
                non_retryable_error_types=["PolicyDeniedError"],
            ),
        )

        # Stage 3 — JITTokenIssue
        jit_input = JITTokenInput(
            agent_type=inp.agent_type,
            requester_id=inp.requester_id,
            task_id=task_id,
            protect_outbound_request=inp.protect_outbound_request,
            session_id=inp.session_id,
            allowed_actions=("llm:complete",),
            rotation_key=f"llm:{task_id}",
            traceparent=inp.traceparent,
        )
        token_result: JITTokenResult = await workflow.execute_activity(
            "JITTokenIssue",
            jit_input,
            result_type=JITTokenResult,
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        if Decimal(inp.projected_spend_usd) > Decimal("50.00"):
            decision = await self._await_pending_approval(inp)
            if not decision.approved:
                return WorkflowOutput(
                    task_id=task_id,
                    content="",
                    sanitized_prompt=policy_result.sanitized_prompt,
                    pii_types=list(scrub_result.pii_types),
                    tokens_used=0,
                    model=inp.model,
                    workflow_status=self._workflow_status,
                    approval_state=self._approval_state.value,
                    approver_id=decision.approver_id,
                    reason=decision.reason,
                    budget_spent_usd=budget_spent_usd,
                )

        if inp.budget_session_id is not None and inp.budget_limit_usd is not None:
            budget_precheck_result: BudgetPreCheckResult = await workflow.execute_activity(
                "BudgetPreCheck",
                BudgetPreCheckInput(
                    task_id=task_id,
                    agent_type=inp.agent_type,
                    budget_session_id=inp.budget_session_id,
                    budget_limit_usd=inp.budget_limit_usd,
                    history=budget_history,
                    traceparent=inp.traceparent,
                ),
                result_type=BudgetPreCheckResult,
                schedule_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(
                    maximum_attempts=1,
                    non_retryable_error_types=["BudgetExceededError"],
                ),
            )
            budget_spent_usd = budget_precheck_result.snapshot["cost_usd"]

        # Stage 4 — LLMInvoke (exponential backoff; HITL on exhaustion)
        llm_input = LLMInvokeInput(
            task_id=task_id,
            token=token_result.token,
            sanitized_prompt=policy_result.sanitized_prompt,
            agent_type=inp.agent_type,
            requester_id=inp.requester_id,
            model=inp.model,
            max_tokens=inp.max_tokens,
            temperature=inp.temperature,
            system_prompt=inp.system_prompt,
            protect_outbound_request=inp.protect_outbound_request,
            session_id=inp.session_id,
            allowed_actions=("llm:complete",),
            rotation_key=f"llm:{task_id}",
            protected_private_key_pem=token_result.protected_private_key_pem,
            protected_public_jwk=token_result.protected_public_jwk,
            traceparent=inp.traceparent,
        )
        try:
            llm_result: LLMInvokeResult = await workflow.execute_activity(
                "LLMInvoke",
                llm_input,
                result_type=LLMInvokeResult,
                schedule_to_close_timeout=timedelta(minutes=10),
                retry_policy=LLM_RETRY_POLICY,
            )
        except ActivityError as exc:
            cause = exc.cause
            if isinstance(cause, ApplicationError) and cause.type in {
                "PolicyDeniedError",
                "TokenScopeError",
                "TokenExpiredError",
            }:
                self._workflow_status = WorkflowStatus.FAILED.value
                await workflow.execute_activity(
                    "WorkflowAudit",
                    WorkflowAuditInput(
                        event="workflow.failed",
                        outcome="error",
                        stage="llm-invoke",
                        task_id=inp.task_id,
                        agent_type=inp.agent_type,
                        session_id=inp.session_id,
                        workflow_status=self._workflow_status,
                        event_type=LifecycleEvent.FAILED.value,
                        details={
                            "error_type": cause.type,
                            "reason": cause.message,
                        },
                    ),
                    schedule_to_close_timeout=timedelta(minutes=1),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                raise cause from exc

            self._workflow_status = WorkflowStatus.HUMAN_INTERVENTION_REQUIRED.value
            await workflow.execute_activity(
                "WorkflowAudit",
                WorkflowAuditInput(
                    event="workflow.hitl_required",
                    outcome="error",
                    stage="llm-invoke",
                    task_id=inp.task_id,
                    agent_type=inp.agent_type,
                    session_id=inp.session_id,
                    workflow_status=self._workflow_status,
                    details={
                        "maximum_attempts": str(LLM_RETRY_POLICY.maximum_attempts),
                        "retry_state": str(exc.retry_state),
                    },
                ),
                schedule_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            # All retries exhausted — escalate to human-in-the-loop.
            raise HITLEscalationError(
                f"LLMInvoke exhausted {LLM_RETRY_POLICY.maximum_attempts} attempts "
                f"for task_id={task_id}: {exc}"
            ) from exc

        if inp.budget_session_id is not None and inp.budget_limit_usd is not None:
            budget_record_result: BudgetRecordResult = await workflow.execute_activity(
                "BudgetRecordSpend",
                BudgetRecordInput(
                    task_id=task_id,
                    agent_type=inp.agent_type,
                    budget_session_id=inp.budget_session_id,
                    budget_limit_usd=inp.budget_limit_usd,
                    tokens_used=llm_result.tokens_used,
                    cost_per_token_usd=inp.cost_per_token_usd,
                    history=budget_history,
                    traceparent=inp.traceparent,
                ),
                result_type=BudgetRecordResult,
                schedule_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    non_retryable_error_types=["BudgetExceededError"],
                ),
            )
            budget_history = list(budget_record_result.history)
            budget_spent_usd = budget_record_result.snapshot["cost_usd"]

        # Stage 5 — PostSanitize
        post_input = PostSanitizeInput(
            task_id=task_id,
            agent_type=inp.agent_type,
            content=llm_result.content,
            tokens_used=llm_result.tokens_used,
            model=llm_result.model,
            provider=llm_result.provider,
            session_id=inp.session_id,
            total_retries=llm_result.retry_count,
            traceparent=inp.traceparent,
        )
        post_result: PostSanitizeResult = await workflow.execute_activity(
            "PostSanitize",
            post_input,
            result_type=PostSanitizeResult,
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        # Merge all discovered PII types.
        all_pii_types = list(scrub_result.pii_types)
        for t in policy_result.extra_pii_types:
            if t not in all_pii_types:
                all_pii_types.append(t)
        for t in post_result.pii_types:
            if t not in all_pii_types:
                all_pii_types.append(t)

        self._workflow_status = WorkflowStatus.COMPLETED.value
        approval_state = (
            self._approval_state.value
            if self._approval_state != PendingApprovalState.NOT_REQUIRED
            else PendingApprovalState.NOT_REQUIRED.value
        )

        return WorkflowOutput(
            task_id=task_id,
            content=post_result.sanitized_content,
            sanitized_prompt=policy_result.sanitized_prompt,
            pii_types=all_pii_types,
            tokens_used=llm_result.tokens_used,
            model=llm_result.model,
            workflow_status="completed",
            approval_state=approval_state,
            budget_spent_usd=budget_spent_usd,
        )

    async def _await_pending_approval(self, inp: WorkflowInput) -> ApprovalSignalPayload:
        """Pause execution until the review gate is approved, denied, or timed out."""
        self._approval_state = PendingApprovalState.AWAITING_APPROVAL
        self._workflow_status = WorkflowStatus.HUMAN_INTERVENTION_REQUIRED.value
        self._pending_since_epoch_seconds = workflow.now().timestamp()
        await workflow.execute_activity(
            "WorkflowAudit",
            WorkflowAuditInput(
                event="workflow.pending_approval",
                outcome="allow",
                stage="pending-approval",
                task_id=inp.task_id,
                agent_type=inp.agent_type,
                session_id=inp.session_id,
                workflow_status=self._workflow_status,
                event_type=LifecycleEvent.PENDING_APPROVAL.value,
                details={
                    "projected_spend_usd": inp.projected_spend_usd,
                },
            ),
            schedule_to_close_timeout=timedelta(minutes=1),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        try:
            await workflow.wait_condition(
                lambda: self._approval_signal is not None,
                timeout=timedelta(seconds=inp.approval_timeout_seconds),
            )
        except TimeoutError:
            self._approval_state = PendingApprovalState.TIMED_OUT
            self._workflow_status = PendingApprovalState.TIMED_OUT.value
            self._pending_since_epoch_seconds = None
            await workflow.execute_activity(
                "WorkflowAudit",
                WorkflowAuditInput(
                    event="workflow.timed_out",
                    outcome="deny",
                    stage="pending-approval",
                    task_id=inp.task_id,
                    agent_type=inp.agent_type,
                    session_id=inp.session_id,
                    workflow_status=self._workflow_status,
                    event_type=LifecycleEvent.DENIED.value,
                    details={"approver_id": "timeout"},
                ),
                schedule_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            return ApprovalSignalPayload(
                approver_id="timeout",
                reason="Approval timed out",
                approved=False,
            )

        assert self._approval_signal is not None
        signal = self._approval_signal
        self._approval_state = (
            PendingApprovalState.APPROVED if signal.approved else PendingApprovalState.DENIED
        )
        self._workflow_status = (
            WorkflowStatus.RUNNING.value if signal.approved else PendingApprovalState.DENIED.value
        )
        self._pending_since_epoch_seconds = None
        await workflow.execute_activity(
            "WorkflowAudit",
            WorkflowAuditInput(
                event="workflow.approved" if signal.approved else "workflow.denied",
                outcome="allow" if signal.approved else "deny",
                stage="pending-approval",
                task_id=inp.task_id,
                agent_type=inp.agent_type,
                session_id=inp.session_id,
                workflow_status=self._workflow_status,
                event_type=(
                    LifecycleEvent.APPROVED.value
                    if signal.approved
                    else LifecycleEvent.DENIED.value
                ),
                details={
                    "approver_id": signal.approver_id,
                    "reason": signal.reason,
                },
            ),
            schedule_to_close_timeout=timedelta(minutes=1),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        return signal


# ---------------------------------------------------------------------------
# AgentScheduler (P1 compatibility — schedules workflows via Temporal client)
# ---------------------------------------------------------------------------


class AgentScheduler:
    """Schedules and manages agent workflows.

    This class wraps the Temporal client for durable workflow scheduling.
    The in-memory fallback supports local development without a Temporal server.
    """

    def __init__(self) -> None:
        """Initialise the scheduler with an empty in-memory workflow registry."""
        self._workflows: dict[UUID, WorkflowHandle] = {}

    def schedule(self, agent_type: str, task_description: str) -> WorkflowHandle:
        """Schedule an agent workflow and return a tracking handle.

        Args:
            agent_type: The class of agent to run (e.g. ``"general"``).
            task_description: Human-readable description of the task.

        Returns:
            A :class:`WorkflowHandle` in ``PENDING`` status.
        """
        handle = WorkflowHandle(
            agent_type=agent_type,
            task_description=task_description,
            status=WorkflowStatus.PENDING,
        )
        self._workflows[handle.workflow_id] = handle
        _logger.info(
            "workflow.scheduled",
            workflow_id=str(handle.workflow_id),
            agent_type=agent_type,
        )
        return handle

    def get(self, workflow_id: UUID) -> WorkflowHandle | None:
        """Retrieve a workflow handle by ID.

        Args:
            workflow_id: UUID of the workflow to look up.

        Returns:
            The :class:`WorkflowHandle` if found, otherwise ``None``.
        """
        return self._workflows.get(workflow_id)

    def update_status(self, workflow_id: UUID, status: WorkflowStatus) -> None:
        """Update the status of a workflow.

        Args:
            workflow_id: UUID of the workflow to update.
            status: New :class:`WorkflowStatus` value.

        Raises:
            KeyError: If no workflow with the given ID exists.
        """
        handle = self._workflows.get(workflow_id)
        if handle is None:
            raise KeyError(f"Workflow {workflow_id} not found")
        handle.status = status
        _logger.info(
            "workflow.status_updated",
            workflow_id=str(workflow_id),
            status=status.value,
        )

    async def run_workflow(self, handle: WorkflowHandle) -> WorkflowHandle:
        """Transition the workflow handle through RUNNING → COMPLETED.

        In production this submits an ``AgentTaskWorkflow`` execution to the
        Temporal server via the client.  This method provides a synchronous
        in-memory path for environments without a Temporal server.

        Args:
            handle: The :class:`WorkflowHandle` to run.

        Returns:
            The same handle, updated to ``COMPLETED`` status.
        """
        handle.status = WorkflowStatus.RUNNING
        _logger.info("workflow.started", workflow_id=str(handle.workflow_id))
        await asyncio.sleep(0)  # yield to event loop
        handle.status = WorkflowStatus.COMPLETED
        _logger.info("workflow.completed", workflow_id=str(handle.workflow_id))
        return handle
