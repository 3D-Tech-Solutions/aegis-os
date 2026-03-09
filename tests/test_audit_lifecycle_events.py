"""A2-2 lifecycle audit coverage for workflow transitions and retry metadata."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, cast

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.adapters.base import BaseAdapter, LLMResponse
from src.audit_vault.logger import AuditLogger, LifecycleEvent
from src.control_plane.scheduler import (
    AegisActivities,
    AgentTaskWorkflow,
    ApprovalSignalPayload,
    ApprovalStatusSnapshot,
    PendingApprovalState,
    WorkflowAuditActivities,
    WorkflowInput,
    WorkflowOutput,
)
from src.governance.policy_engine.opa_client import PolicyEngine, PolicyResult

_TASK_QUEUE = "aegis-audit-lifecycle-events"


class _RecordingAuditLogger(AuditLogger):
    """Capture rendered audit entries after logger helpers expand metadata."""

    def __init__(self) -> None:
        super().__init__("test.audit.lifecycle-events")
        self.entries: list[dict[str, Any]] = []

    def info(self, event: str, **kwargs: object) -> None:
        self.entries.append({"level": "info", "event": event, **kwargs})

    def warning(self, event: str, **kwargs: object) -> None:
        self.entries.append({"level": "warning", "event": event, **kwargs})

    def error(self, event: str, **kwargs: object) -> None:
        self.entries.append({"level": "error", "event": event, **kwargs})


def _allow_policy_engine() -> PolicyEngine:
    """Return a typed allow-all policy engine stub."""

    class _AllowPolicyEngine:
        async def evaluate(self, *_args: Any, **_kwargs: Any) -> PolicyResult:
            return PolicyResult(allowed=True, action="allow", reasons=[], fields=[])

    return cast(PolicyEngine, _AllowPolicyEngine())


class _SequenceAdapter(BaseAdapter):
    """Return configured responses or raise configured exceptions in order."""

    def __init__(self, responses: list[LLMResponse | Exception]) -> None:
        self._responses = responses
        self._index = 0

    @property
    def provider_name(self) -> str:
        return "stub"

    async def complete(self, request: Any) -> LLMResponse:
        current = self._responses[min(self._index, len(self._responses) - 1)]
        self._index += 1
        if isinstance(current, Exception):
            raise current
        return current


def _workflow_input(*, projected_spend_usd: str = "0.00") -> WorkflowInput:
    task_id = str(uuid.uuid4())
    return WorkflowInput(
        task_id=task_id,
        prompt="Trace the workflow lifecycle.",
        agent_type="general",
        requester_id="lifecycle-events-user",
        session_id=f"session-{task_id}",
        projected_spend_usd=projected_spend_usd,
        approval_timeout_seconds=30,
    )


def _response(content: str = "Lifecycle response", tokens_used: int = 11) -> LLMResponse:
    return LLMResponse(
        content=content,
        tokens_used=tokens_used,
        model="gpt-4o-mini",
        provider="stub",
        finish_reason="stop",
    )


def _assert_mandatory_fields(entry: dict[str, Any], expected_event_type: LifecycleEvent) -> None:
    assert entry["event_type"] == expected_event_type.value
    assert isinstance(entry["task_id"], str) and entry["task_id"]
    assert isinstance(entry["session_id"], str)
    assert isinstance(entry["timestamp"], str) and entry["timestamp"]
    assert isinstance(entry["workflow_status"], str) and entry["workflow_status"]


async def _run_workflow(
    *,
    adapter: BaseAdapter,
    workflow_input: WorkflowInput,
    include_workflow_audit: bool = False,
    signal: ApprovalSignalPayload | None = None,
    expect_error: bool = False,
) -> tuple[_RecordingAuditLogger, WorkflowOutput | None]:
    audit = _RecordingAuditLogger()
    activities = AegisActivities(
        adapter=adapter,
        audit_logger=audit,
        policy_engine=_allow_policy_engine(),
    )
    registered_activities: list[Any] = [
        activities.pre_pii_scrub,
        activities.policy_eval,
        activities.jit_token_issue,
        activities.llm_invoke,
        activities.post_sanitize,
    ]
    if include_workflow_audit:
        registered_activities.append(WorkflowAuditActivities(audit_logger=audit).record_event)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[AgentTaskWorkflow],
            activities=registered_activities,
        ):
            handle = await env.client.start_workflow(
                AgentTaskWorkflow.run,
                workflow_input,
                id=f"lifecycle-events-{workflow_input.task_id}",
                task_queue=_TASK_QUEUE,
            )
            if signal is not None:
                await _wait_for_state(handle, PendingApprovalState.AWAITING_APPROVAL.value)
                if signal.approved:
                    await handle.signal(AgentTaskWorkflow.approve, signal)
                else:
                    await handle.signal(AgentTaskWorkflow.deny, signal)
            if expect_error:
                with pytest.raises(Exception):
                    await handle.result()
                return audit, None
            result: WorkflowOutput = await handle.result()
            return audit, result


async def _wait_for_state(handle: Any, expected_state: str) -> ApprovalStatusSnapshot:
    for _ in range(20):
        snapshot = await handle.query(AgentTaskWorkflow.approval_status)
        if snapshot.approval_state == expected_state:
            return cast(ApprovalStatusSnapshot, snapshot)
        await asyncio.sleep(0.05)
    return cast(ApprovalStatusSnapshot, await handle.query(AgentTaskWorkflow.approval_status))


def _entry(
    entries: list[dict[str, Any]],
    *,
    event: str,
    event_type: LifecycleEvent,
) -> dict[str, Any]:
    return next(
        entry
        for entry in entries
        if entry.get("event") == event and entry.get("event_type") == event_type.value
    )


@pytest.mark.integration
class TestLifecycleEvents:
    """Cover every Phase 2 lifecycle event type with mandatory metadata."""

    @pytest.mark.asyncio
    async def test_started_event_has_mandatory_fields(self) -> None:
        audit, result = await _run_workflow(
            adapter=_SequenceAdapter([_response()]),
            workflow_input=_workflow_input(),
        )

        assert result is not None and result.workflow_status == "completed"
        started = _entry(audit.entries, event="workflow.started", event_type=LifecycleEvent.STARTED)
        _assert_mandatory_fields(started, LifecycleEvent.STARTED)

    @pytest.mark.asyncio
    async def test_retried_event_has_mandatory_fields(self) -> None:
        audit, result = await _run_workflow(
            adapter=_SequenceAdapter(
                [
                    ApplicationError("Rate limited", type="RateLimitError"),
                    _response(content="Recovered after retry"),
                ]
            ),
            workflow_input=_workflow_input(),
        )

        assert result is not None and result.workflow_status == "completed"
        retried = _entry(audit.entries, event="llm.retried", event_type=LifecycleEvent.RETRIED)
        _assert_mandatory_fields(retried, LifecycleEvent.RETRIED)

    @pytest.mark.asyncio
    async def test_pending_approval_event_has_mandatory_fields(self) -> None:
        audit, result = await _run_workflow(
            adapter=_SequenceAdapter([_response(content="Approved after review")]),
            workflow_input=_workflow_input(projected_spend_usd="75.00"),
            include_workflow_audit=True,
            signal=ApprovalSignalPayload(
                approver_id="reviewer-1",
                reason="Spend justified",
                approved=True,
            ),
        )

        assert result is not None and result.workflow_status == "completed"
        pending = _entry(
            audit.entries,
            event="workflow.pending_approval",
            event_type=LifecycleEvent.PENDING_APPROVAL,
        )
        _assert_mandatory_fields(pending, LifecycleEvent.PENDING_APPROVAL)

    @pytest.mark.asyncio
    async def test_approved_event_has_mandatory_fields(self) -> None:
        audit, result = await _run_workflow(
            adapter=_SequenceAdapter([_response(content="Approved path")]),
            workflow_input=_workflow_input(projected_spend_usd="75.00"),
            include_workflow_audit=True,
            signal=ApprovalSignalPayload(
                approver_id="reviewer-2",
                reason="Approved after review",
                approved=True,
            ),
        )

        assert result is not None and result.workflow_status == "completed"
        approved = _entry(
            audit.entries,
            event="workflow.approved",
            event_type=LifecycleEvent.APPROVED,
        )
        _assert_mandatory_fields(approved, LifecycleEvent.APPROVED)

    @pytest.mark.asyncio
    async def test_denied_event_has_approver_id_and_mandatory_fields(self) -> None:
        audit, result = await _run_workflow(
            adapter=_SequenceAdapter([_response()]),
            workflow_input=_workflow_input(projected_spend_usd="75.00"),
            include_workflow_audit=True,
            signal=ApprovalSignalPayload(
                approver_id="reviewer-3",
                reason="Denied due to overspend",
                approved=False,
            ),
        )

        assert result is not None and result.workflow_status == PendingApprovalState.DENIED.value
        denied = _entry(audit.entries, event="workflow.denied", event_type=LifecycleEvent.DENIED)
        _assert_mandatory_fields(denied, LifecycleEvent.DENIED)
        assert denied["approver_id"] == "reviewer-3"

    @pytest.mark.asyncio
    async def test_completed_event_has_mandatory_fields(self) -> None:
        audit, result = await _run_workflow(
            adapter=_SequenceAdapter([_response()]),
            workflow_input=_workflow_input(),
        )

        assert result is not None and result.workflow_status == "completed"
        completed = _entry(
            audit.entries,
            event="workflow.completed",
            event_type=LifecycleEvent.COMPLETED,
        )
        _assert_mandatory_fields(completed, LifecycleEvent.COMPLETED)

    @pytest.mark.asyncio
    async def test_failed_event_has_mandatory_fields(self) -> None:
        audit, result = await _run_workflow(
            adapter=_SequenceAdapter(
                [
                    ApplicationError(
                        "Policy denied",
                        type="PolicyDeniedError",
                        non_retryable=True,
                    )
                ]
            ),
            workflow_input=_workflow_input(),
            include_workflow_audit=True,
            expect_error=True,
        )

        assert result is None
        failed = _entry(audit.entries, event="workflow.failed", event_type=LifecycleEvent.FAILED)
        _assert_mandatory_fields(failed, LifecycleEvent.FAILED)

    @pytest.mark.asyncio
    async def test_retry_events_increment_and_completed_reports_total_retries(self) -> None:
        audit, result = await _run_workflow(
            adapter=_SequenceAdapter(
                [
                    ApplicationError("Rate limited", type="RateLimitError"),
                    ApplicationError("Rate limited", type="RateLimitError"),
                    _response(content="Recovered on third attempt", tokens_used=25),
                ]
            ),
            workflow_input=_workflow_input(),
        )

        assert result is not None and result.workflow_status == "completed"
        retried_entries = [
            entry
            for entry in audit.entries
            if entry.get("event_type") == LifecycleEvent.RETRIED.value
        ]
        assert [entry["retry_count"] for entry in retried_entries] == [1, 2]
        completed = _entry(
            audit.entries,
            event="workflow.completed",
            event_type=LifecycleEvent.COMPLETED,
        )
        assert completed["total_retries"] == 2
