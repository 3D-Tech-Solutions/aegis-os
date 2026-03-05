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

"""Orchestrator – single, ordered entry point for all agent LLM requests.

Pipeline (must not be reordered or skipped):

    Stage 1 – Guardrails pre-sanitize    (prompt injection + PII masking)
    Stage 2 – OPA policy evaluation      (allow / deny)
    Stage 3 – SessionManager             (issue or validate JIT token)
    Stage 4 – LLM Adapter               (completion request)
    Stage 5 – Guardrails post-sanitize   (PII masking on LLM output)
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from decimal import Decimal
from uuid import UUID, uuid4

from jose.exceptions import ExpiredSignatureError as JoseExpiredSignatureError
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel, Field

from src.adapters.base import BaseAdapter, LLMRequest, LLMResponse
from src.audit_vault.logger import AuditLogger
from src.governance.guardrails import Guardrails
from src.governance.policy_engine.opa_client import OpaUnavailableError, PolicyEngine, PolicyInput
from src.governance.session_mgr import SessionManager, TokenExpiredError, TokenScopeError
from src.watchdog.budget_enforcer import BudgetEnforcer, BudgetExceededError
from src.watchdog.loop_detector import (
    LoopDetectedError,
    LoopDetector,
    LoopSignal,
    PendingApprovalError,
    TokenVelocityError,
)
from src.watchdog.metrics import orchestrator_errors as _orchestrator_errors

logger = logging.getLogger(__name__)
_module_tracer = trace.get_tracer(__name__, schema_url="https://opentelemetry.io/schemas/1.11.0")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class OrchestratorRequest(BaseModel):
    """Input to the orchestration pipeline."""

    task_id: UUID | None = Field(default_factory=uuid4)
    prompt: str = Field(..., min_length=1, max_length=32_768)
    agent_type: str = Field(..., min_length=1, max_length=64)
    requester_id: str = Field(..., min_length=1, max_length=256)
    # If the caller already holds a valid JIT token, supply it here;
    # otherwise the orchestrator issues a fresh one via SessionManager.
    session_token: str | None = None
    model: str = "gpt-4o-mini"
    max_tokens: int = 1024
    temperature: float = 0.7
    system_prompt: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)
    # ------------------------------------------------------------------
    # Watchdog budget-enforcement fields
    # ------------------------------------------------------------------
    # When budget_session_id is provided AND a BudgetEnforcer is wired into
    # the Orchestrator, the session is checked before the LLM call and
    # debited with the actual token cost afterward.
    budget_session_id: UUID | None = None
    cost_per_token: Decimal = Decimal("0.000002")
    # ------------------------------------------------------------------
    # Watchdog loop-detection fields
    # ------------------------------------------------------------------
    # When loop_session_id is provided AND a LoopDetector is wired into the
    # Orchestrator, record_step() is called after every successful LLM
    # response using the supplied loop_signal.
    loop_session_id: UUID | None = None
    loop_signal: LoopSignal = LoopSignal.NO_PROGRESS
    loop_token_delta: int | None = None  # defaults to llm_response.tokens_used


class OrchestratorResult(BaseModel):
    """Output of the orchestration pipeline."""

    task_id: UUID
    response: LLMResponse
    session_token: str
    sanitized_prompt: str
    pii_found_in_prompt: list[str] = Field(default_factory=list)
    pii_found_in_response: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class PolicyDeniedError(PermissionError):
    """Raised when the OPA policy engine denies the request."""


class BudgetLimitError(Exception):
    """Raised when the agent session has exhausted its allocated budget.

    The orchestrator catches ``BudgetExceededError`` from the Watchdog and
    re-raises as ``BudgetLimitError`` so the router can map it to HTTP 429
    without importing directly from ``src.watchdog.budget_enforcer``.
    """


class LoopHaltError(Exception):
    """Raised when the LoopDetector circuit breaker trips on a NO_PROGRESS streak.

    Wraps :exc:`~src.watchdog.loop_detector.LoopDetectedError` so the router
    can map it to HTTP 429 without importing from ``src.watchdog`` directly.
    """


class LoopVelocityError(Exception):
    """Raised when a single step's token count exceeds ``max_token_velocity``.

    Wraps :exc:`~src.watchdog.loop_detector.TokenVelocityError`.
    """


class LoopApprovalError(Exception):
    """Raised when the agent signals that human approval is required.

    Wraps :exc:`~src.watchdog.loop_detector.PendingApprovalError`.
    The orchestrator must **not** treat this as a terminal error — callers
    should surface it as a 202 Accepted / pending-approval response.
    """


class MissingTaskIdError(ValueError):
    """Raised when a pipeline request arrives without a ``task_id``.

    ``task_id`` is auto-populated by :class:`OrchestratorRequest`'s
    ``default_factory``; this error can only be triggered by callers that
    bypass Pydantic validation (e.g. via ``model_construct``).  The guard
    fires before Stage 4 so the LLM adapter is never called for an
    un-trackable request.
    """


# ---------------------------------------------------------------------------
# Stage error guard
# ---------------------------------------------------------------------------


@contextmanager
def _stage_error_guard(stage: str, agent_type: str) -> Generator[None, None, None]:
    """Increment the orchestrator error counter on any stage exception, then re-raise.

    Usage::

        with tracer.start_as_current_span("stage.foo") as span, \\
                _stage_error_guard("foo", request.agent_type):
            ...  # stage body

    If any exception propagates out of the ``with`` block,
    ``aegis_orchestrator_errors_total{stage=stage, agent_type=agent_type}``
    is incremented **before** the exception continues to unwind.  The
    exception is re-raised unchanged so callers see the original type.
    """
    try:
        yield
    except Exception:
        _orchestrator_errors.labels(stage=stage, agent_type=agent_type).inc()
        raise


# Exceptions that represent intentional governance decisions — the stage body
# emits a schema-conformant ``deny`` / ``redact`` audit event *before*
# re-raising these.  ``_span_stage`` must not emit a duplicate ``error``
# event on top of one that is already in the audit trail.
_CONTROLLED_EXC: tuple[type[Exception], ...] = (
    PolicyDeniedError,
    TokenExpiredError,
    TokenScopeError,
    BudgetLimitError,
    LoopHaltError,
    LoopVelocityError,
    LoopApprovalError,
)


@contextmanager
def _span_stage(
    tracer: trace.Tracer,
    span_name: str,
    stage_key: str,
    agent_type: str,
    task_id: str,
    audit: AuditLogger | None = None,
) -> Generator[trace.Span, None, None]:
    """Open a named OTel span for a pipeline stage with mandatory attributes.

    Sets ``task_id``, ``agent_type``, and ``span.status`` (``"OK"`` or
    ``"ERROR"``) on every span.  On exception, additionally sets
    ``error=True`` and ``error.message``, records the exception on the span,
    marks ``StatusCode.ERROR``, increments the Prometheus error counter, and
    re-raises the original exception unchanged.

    When *audit* is supplied and the propagating exception is **not** a
    ``_CONTROLLED_EXC`` (i.e. it was not already emitted by the stage body
    as a deny / redact outcome), a ``stage.error`` event with
    ``outcome="error"`` is emitted before the re-raise.  This satisfies the
    A1-2 "no silent stage" requirement — any unexpected runtime exception is
    guaranteed to produce at least one audit event.

    This is the canonical instrumentation helper for all pipeline stages
    (A1-1: attach a named OTel span; A1-2: emit structured audit events).
    """
    with tracer.start_as_current_span(span_name) as span:
        span.set_attribute("task_id", task_id)
        span.set_attribute("agent_type", agent_type)
        try:
            yield span
            span.set_attribute("span.status", "OK")
            span.set_status(Status(StatusCode.OK))
        except Exception as exc:
            span.set_attribute("span.status", "ERROR")
            span.set_attribute("error", True)
            span.set_attribute("error.message", str(exc))
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            _orchestrator_errors.labels(stage=stage_key, agent_type=agent_type).inc()
            if audit is not None and not isinstance(exc, _CONTROLLED_EXC):
                audit.stage_event(
                    "stage.error",
                    outcome="error",
                    stage=span_name,
                    task_id=task_id,
                    agent_type=agent_type,
                    error_message=str(exc),
                )
            raise


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class Orchestrator:
    """Sequences the five governance and execution stages in strict order.

    All five stages *must* execute (unless a preceding stage raises), and they
    must execute in the documented order.  No stage may be skipped, and no
    governance call may be made outside of this class.
    """

    def __init__(
        self,
        adapter: BaseAdapter,
        guardrails: Guardrails | None = None,
        policy_engine: PolicyEngine | None = None,
        session_mgr: SessionManager | None = None,
        audit_logger: AuditLogger | None = None,
        tracer: trace.Tracer | None = None,
        budget_enforcer: BudgetEnforcer | None = None,
        loop_detector: LoopDetector | None = None,
    ) -> None:
        self._adapter = adapter
        self._guardrails = guardrails if guardrails is not None else Guardrails()
        self._policy_engine = policy_engine if policy_engine is not None else PolicyEngine()
        self._session_mgr = session_mgr if session_mgr is not None else SessionManager()
        self._audit = audit_logger if audit_logger is not None else AuditLogger("orchestrator")
        self._tracer = tracer if tracer is not None else _module_tracer
        self._budget_enforcer = budget_enforcer
        self._loop_detector = loop_detector

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, request: OrchestratorRequest) -> OrchestratorResult:
        """Execute the five-stage pipeline and return a sanitized LLM response.

        Raises
        ------
        PromptInjectionError
            Stage 1 detected a prompt injection attempt.
        PolicyDeniedError
            Stage 2 OPA evaluation returned ``allow = false``.
        TokenExpiredError
            Stage 3: the supplied ``session_token`` has passed its ``exp``
            timestamp.  An ``token_expired`` audit event is emitted before
            the raise.
        TokenScopeError
            Stage 3: the supplied ``session_token`` is scoped to a different
            ``agent_type`` than the current request.  A
            ``token_scope_violation`` audit event is emitted before the raise.
        jose.JWTError
            Stage 3: the supplied ``session_token`` is malformed or has an
            invalid signature.
        httpx.HTTPStatusError / httpx.TimeoutException
            Stage 4 LLM adapter call failed.
        """
        if request.task_id is None:
            raise MissingTaskIdError(
                "task_id must not be None — every pipeline request requires "
                "a unique task identifier for audit correlation"
            )
        task_id: UUID = request.task_id

        with self._tracer.start_as_current_span("orchestrator.run") as _root:
            _root.set_attribute("task_id", str(task_id))
            _root.set_attribute("agent_type", request.agent_type)
            _root.set_attribute("requester_id", request.requester_id)

            # ----------------------------------------------------------------
            # Stage 1 — Guardrails: pre-sanitize prompt
            # ----------------------------------------------------------------
            with _span_stage(
                self._tracer,
                "pre-pii-scrub",
                "pre_pii_scrub",
                request.agent_type,
                str(task_id),
                audit=self._audit,
            ):
                # check_prompt_injection raises PromptInjectionError on hit;
                # mask_pii returns cleaned text and discovered PII categories.
                self._guardrails.check_prompt_injection(request.prompt)
                pre_mask = self._guardrails.mask_pii(request.prompt)
                sanitized_prompt = pre_mask.text
                pii_in_prompt = pre_mask.found_types
                self._audit.stage_event(
                    "guardrails.pre_sanitize",
                    outcome="redact" if pii_in_prompt else "allow",
                    stage="pre-pii-scrub",
                    task_id=str(task_id),
                    agent_type=request.agent_type,
                    pii_types=pii_in_prompt,
                )

            # ----------------------------------------------------------------
            # Stage 2 — OPA: policy evaluation
            # ----------------------------------------------------------------
            with _span_stage(
                self._tracer,
                "policy-eval",
                "policy_eval",
                request.agent_type,
                str(task_id),
                audit=self._audit,
            ):
                policy_input = PolicyInput(
                    agent_type=request.agent_type,
                    requester_id=request.requester_id,
                    action="llm.complete",
                    resource=f"model:{request.model}",
                    metadata=request.metadata,
                )
                try:
                    policy_result = await self._policy_engine.evaluate(
                        "agent_access", policy_input
                    )
                except OpaUnavailableError as exc:
                    self._audit.stage_event(
                        "policy.opa_unavailable",
                        outcome="error",
                        stage="policy-eval",
                        task_id=str(task_id),
                        agent_type=request.agent_type,
                        requester_id=request.requester_id,
                        error_message=str(exc),
                    )
                    raise PolicyDeniedError(
                        "OPA unavailable – request denied (fail-closed)"
                    ) from exc

                # Fail-closed: deny on allow=False OR explicit action="reject".
                # IMPORTANT: the raw prompt is deliberately excluded from the
                # audit event to prevent plaintext PII appearing in logs.
                if not policy_result.allowed or policy_result.action == "reject":
                    self._audit.stage_event(
                        "policy.denied",
                        outcome="deny",
                        stage="policy-eval",
                        task_id=str(task_id),
                        agent_type=request.agent_type,
                        requester_id=request.requester_id,
                        reasons=policy_result.reasons,
                    )
                    raise PolicyDeniedError(
                        f"OPA denied request for agent_type={request.agent_type!r}: "
                        f"{policy_result.reasons}"
                    )

                # All denial conditions passed — policy allows the request.
                self._audit.stage_event(
                    "policy.allowed",
                    outcome="allow",
                    stage="policy-eval",
                    task_id=str(task_id),
                    agent_type=request.agent_type,
                )

            # ----------------------------------------------------------------
            # Stage 2b — OPA mask instruction: re-scrub specified fields
            # ----------------------------------------------------------------
            if policy_result.action == "mask":
                with _span_stage(
                    self._tracer,
                    "policy-mask",
                    "policy_mask",
                    request.agent_type,
                    str(task_id),
                    audit=self._audit,
                ):
                    for field in policy_result.fields:
                        if field == "prompt":
                            mask_result = self._guardrails.scrub(sanitized_prompt)
                            sanitized_prompt = mask_result.text
                            # Merge any newly-discovered PII types (deduplicate).
                            for t in mask_result.found_types:
                                if t not in pii_in_prompt:
                                    pii_in_prompt.append(t)
                    self._audit.stage_event(
                        "policy.mask_applied",
                        outcome="redact",
                        stage="policy-eval",
                        task_id=str(task_id),
                        agent_type=request.agent_type,
                        fields=policy_result.fields,
                    )

            # ----------------------------------------------------------------
            # Stage 3 — SessionManager: issue or validate JIT token
            # ----------------------------------------------------------------
            with _span_stage(
                self._tracer,
                "jit-token-issue",
                "jit_token_issue",
                request.agent_type,
                str(task_id),
                audit=self._audit,
            ):
                if request.session_token:
                    # Decode – jose raises ExpiredSignatureError for stale tokens.
                    try:
                        claims = self._session_mgr.validate_token(request.session_token)
                    except JoseExpiredSignatureError as exc:
                        self._audit.stage_event(
                            "token.expired",
                            outcome="deny",
                            stage="jit-token-issue",
                            task_id=str(task_id),
                            agent_type=request.agent_type,
                            requester_id=request.requester_id,
                        )
                        raise TokenExpiredError(
                            "Session token has expired"
                        ) from exc

                    # Belt-and-suspenders: check independently of jose (covers
                    # sub-second clock skew and test-injected fake claims).
                    if self._session_mgr.is_expired(claims):
                        self._audit.stage_event(
                            "token.expired",
                            outcome="deny",
                            stage="jit-token-issue",
                            task_id=str(task_id),
                            agent_type=request.agent_type,
                            requester_id=request.requester_id,
                        )
                        raise TokenExpiredError("Session token has expired")

                    # Scope check: the token must be scoped to the requested
                    # agent_type.  Mismatches are a hard deny.
                    if claims.agent_type != request.agent_type:
                        self._audit.stage_event(
                            "token.scope_violation",
                            outcome="deny",
                            stage="jit-token-issue",
                            task_id=str(task_id),
                            agent_type=request.agent_type,
                            requester_id=request.requester_id,
                        )
                        raise TokenScopeError(
                            f"Token is scoped to agent_type={claims.agent_type!r} "
                            f"but request declares agent_type={request.agent_type!r}"
                        )
                    token = request.session_token
                    self._audit.stage_event(
                        "token.validated",
                        outcome="allow",
                        stage="jit-token-issue",
                        task_id=str(task_id),
                        agent_type=request.agent_type,
                    )
                else:
                    token = self._session_mgr.issue_token(
                        agent_type=request.agent_type,
                        requester_id=request.requester_id,
                        metadata=request.metadata,
                    )
                    # Decode the freshly-issued token to extract the jti for
                    # the immutable audit trail.
                    issued_claims = self._session_mgr.validate_token(token)
                    self._audit.stage_event(
                        "token.issued",
                        outcome="allow",
                        stage="jit-token-issue",
                        task_id=str(task_id),
                        agent_type=request.agent_type,
                        jti=issued_claims.jti,
                        requester_id=request.requester_id,
                    )

            # ----------------------------------------------------------------
            # Stage 3.5 — Watchdog: pre-LLM budget guard
            # If the session has already exhausted its cap the request is
            # denied here — the LLM adapter is never called.
            # ----------------------------------------------------------------
            if (
                self._budget_enforcer is not None
                and request.budget_session_id is not None
            ):
                with _span_stage(
                    self._tracer,
                    "watchdog.pre-llm",
                    "watchdog_pre",
                    request.agent_type,
                    str(task_id),
                    audit=self._audit,
                ):
                    try:
                        self._budget_enforcer.check_budget(request.budget_session_id)
                    except BudgetExceededError as exc:
                        self._audit.stage_event(
                            "budget.exceeded",
                            outcome="deny",
                            stage="watchdog.pre-llm",
                            task_id=str(task_id),
                            agent_type=request.agent_type,
                            budget_session_id=str(request.budget_session_id),
                            error_message=str(exc),
                        )
                        raise BudgetLimitError(str(exc)) from exc
                    self._audit.stage_event(
                        "budget.pre_check",
                        outcome="allow",
                        stage="watchdog.pre-llm",
                        task_id=str(task_id),
                        agent_type=request.agent_type,
                        budget_session_id=str(request.budget_session_id),
                    )

            # ----------------------------------------------------------------
            # Stage 4 — LLM Adapter: completion request
            # The JIT token is injected into ``metadata["aegis_token"]`` so
            # that every LLM call is traceable back to an audited session.
            # ----------------------------------------------------------------
            with _span_stage(
                self._tracer,
                "llm-invoke",
                "llm_invoke",
                request.agent_type,
                str(task_id),
                audit=self._audit,
            ):
                llm_req = LLMRequest(
                    prompt=sanitized_prompt,
                    model=request.model,
                    max_tokens=request.max_tokens,
                    temperature=request.temperature,
                    system_prompt=request.system_prompt,
                    metadata={"aegis_token": token},
                )
                llm_response: LLMResponse = await self._adapter.complete(llm_req)
                self._audit.stage_event(
                    "llm.completed",
                    outcome="allow",
                    stage="llm-invoke",
                    task_id=str(task_id),
                    agent_type=request.agent_type,
                    model=request.model,
                    tokens_used=llm_response.tokens_used,
                )

            # ----------------------------------------------------------------
            # Stage 4.5 — Watchdog: record post-LLM actual spend
            # Debits the session with the exact token cost returned by the
            # adapter.  Raises BudgetLimitError synchronously if this spend
            # pushes the session over its cap.
            # ----------------------------------------------------------------
            if (
                self._budget_enforcer is not None
                and request.budget_session_id is not None
            ):
                with _span_stage(
                    self._tracer,
                    "watchdog.record-spend",
                    "watchdog_record",
                    request.agent_type,
                    str(task_id),
                    audit=self._audit,
                ):
                    spend = Decimal(llm_response.tokens_used) * request.cost_per_token
                    try:
                        self._budget_enforcer.record_spend(request.budget_session_id, spend)
                    except BudgetExceededError as exc:
                        self._audit.stage_event(
                            "budget.exceeded_on_record",
                            outcome="deny",
                            stage="watchdog.record-spend",
                            task_id=str(task_id),
                            agent_type=request.agent_type,
                            error_message=str(exc),
                        )
                        raise BudgetLimitError(str(exc)) from exc
                    self._audit.stage_event(
                        "budget.spend_recorded",
                        outcome="allow",
                        stage="watchdog.record-spend",
                        task_id=str(task_id),
                        agent_type=request.agent_type,
                    )

            # ----------------------------------------------------------------
            # Stage 4.6 — Watchdog: LoopDetector step record
            # Records the step signal AFTER a successful LLM response.  If
            # the circuit breaker trips the appropriate orchestrator-level
            # exception is raised, ensuring the caller never needs to import
            # from src.watchdog directly.
            # ----------------------------------------------------------------
            if (
                self._loop_detector is not None
                and request.loop_session_id is not None
            ):
                with _span_stage(
                    self._tracer,
                    "watchdog.loop-detect",
                    "watchdog_loop",
                    request.agent_type,
                    str(task_id),
                    audit=self._audit,
                ):
                    token_delta = (
                        request.loop_token_delta
                        if request.loop_token_delta is not None
                        else llm_response.tokens_used
                    )
                    try:
                        self._loop_detector.record_step(
                            request.loop_session_id,
                            token_delta=token_delta,
                            signal=request.loop_signal,
                        )
                    except LoopDetectedError as exc:
                        self._audit.stage_event(
                            "loop.halted",
                            outcome="deny",
                            stage="watchdog.loop-detect",
                            task_id=str(task_id),
                            agent_type=request.agent_type,
                            error_message=str(exc),
                        )
                        raise LoopHaltError(str(exc)) from exc
                    except TokenVelocityError as exc:
                        self._audit.stage_event(
                            "loop.velocity_exceeded",
                            outcome="deny",
                            stage="watchdog.loop-detect",
                            task_id=str(task_id),
                            agent_type=request.agent_type,
                            error_message=str(exc),
                        )
                        raise LoopVelocityError(str(exc)) from exc
                    except PendingApprovalError as exc:
                        self._audit.stage_event(
                            "loop.pending_approval",
                            outcome="deny",
                            stage="watchdog.loop-detect",
                            task_id=str(task_id),
                            agent_type=request.agent_type,
                            error_message=str(exc),
                        )
                        raise LoopApprovalError(str(exc)) from exc
                    self._audit.stage_event(
                        "loop.step_recorded",
                        outcome="allow",
                        stage="watchdog.loop-detect",
                        task_id=str(task_id),
                        agent_type=request.agent_type,
                    )

            # ----------------------------------------------------------------
            # Stage 5 — Guardrails: post-sanitize LLM output
            # ----------------------------------------------------------------
            with _span_stage(
                self._tracer,
                "post-sanitize",
                "post_sanitize",
                request.agent_type,
                str(task_id),
                audit=self._audit,
            ):
                post_mask = self._guardrails.mask_pii(llm_response.content)
                final_response = llm_response.model_copy(
                    update={"content": post_mask.text}
                )
                pii_in_response = post_mask.found_types
                self._audit.stage_event(
                    "guardrails.post_sanitize",
                    outcome="redact" if pii_in_response else "allow",
                    stage="post-sanitize",
                    task_id=str(task_id),
                    agent_type=request.agent_type,
                    pii_types=pii_in_response,
                )

            logger.info(
                "orchestrator.run.complete",
                extra={
                    "agent_type": request.agent_type,
                    "model": request.model,
                    "pii_in_prompt": pii_in_prompt,
                    "pii_in_response": pii_in_response,
                },
            )

            return OrchestratorResult(
                task_id=task_id,
                response=final_response,
                session_token=token,
                sanitized_prompt=sanitized_prompt,
                pii_found_in_prompt=pii_in_prompt,
                pii_found_in_response=pii_in_response,
            )
