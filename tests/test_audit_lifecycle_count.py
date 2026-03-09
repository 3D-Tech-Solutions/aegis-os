"""A2-2 regression: happy-path lifecycle event count stays stable."""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.adapters.base import BaseAdapter, LLMResponse
from src.audit_vault.logger import (
    EXPECTED_PHASE2_LIFECYCLE_EVENTS,
    AuditLogger,
)
from src.control_plane.scheduler import (
    AegisActivities,
    AgentTaskWorkflow,
    WorkflowInput,
    WorkflowOutput,
)
from src.governance.policy_engine.opa_client import PolicyEngine, PolicyResult

_TASK_QUEUE = "aegis-audit-lifecycle-count"


class _RecordingAuditLogger(AuditLogger):
    """Capture rendered audit entries for exact lifecycle counting."""

    def __init__(self) -> None:
        super().__init__("test.audit.lifecycle-count")
        self.entries: list[dict[str, Any]] = []

    def info(self, event: str, **kwargs: object) -> None:
        self.entries.append({"level": "info", "event": event, **kwargs})

    def warning(self, event: str, **kwargs: object) -> None:
        self.entries.append({"level": "warning", "event": event, **kwargs})

    def error(self, event: str, **kwargs: object) -> None:
        self.entries.append({"level": "error", "event": event, **kwargs})


class _StubAdapter(BaseAdapter):
    """Return a single successful response for happy-path lifecycle checks."""

    @property
    def provider_name(self) -> str:
        return "stub"

    async def complete(self, request: Any) -> LLMResponse:
        return LLMResponse(
            content="Lifecycle count response",
            tokens_used=9,
            model=request.model,
            provider="stub",
            finish_reason="stop",
        )


def _allow_policy_engine() -> PolicyEngine:
    """Return a typed allow-all policy engine stub."""

    class _AllowPolicyEngine:
        async def evaluate(self, *_args: Any, **_kwargs: Any) -> PolicyResult:
            return PolicyResult(allowed=True, action="allow", reasons=[], fields=[])

    return cast(PolicyEngine, _AllowPolicyEngine())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_happy_path_emits_exact_expected_phase2_lifecycle_events() -> None:
    audit = _RecordingAuditLogger()
    activities = AegisActivities(
        adapter=_StubAdapter(),
        audit_logger=audit,
        policy_engine=_allow_policy_engine(),
    )
    workflow_input = WorkflowInput(
        task_id=str(uuid.uuid4()),
        prompt="Summarise the lifecycle count check.",
        agent_type="general",
        requester_id="lifecycle-count-user",
        session_id="session-lifecycle-count",
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[AgentTaskWorkflow],
            activities=[
                activities.pre_pii_scrub,
                activities.policy_eval,
                activities.jit_token_issue,
                activities.llm_invoke,
                activities.post_sanitize,
            ],
        ):
            result: WorkflowOutput = await env.client.execute_workflow(
                AgentTaskWorkflow.run,
                workflow_input,
                id=f"lifecycle-count-{workflow_input.task_id}",
                task_queue=_TASK_QUEUE,
            )

    assert result.workflow_status == "completed"
    lifecycle_entries = [
        entry for entry in audit.entries if entry.get("event_type") is not None
    ]
    assert [entry["event_type"] for entry in lifecycle_entries] == [
        event.value for event in EXPECTED_PHASE2_LIFECYCLE_EVENTS
    ]
    assert len(lifecycle_entries) == len(EXPECTED_PHASE2_LIFECYCLE_EVENTS)
