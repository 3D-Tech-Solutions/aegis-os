"""A2-3 tests for audit completeness during provider outage recovery."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any, cast

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.adapters.base import BaseAdapter, LLMResponse
from src.audit_vault.logger import AuditLogger
from src.control_plane.scheduler import (
    AegisActivities,
    AgentTaskWorkflow,
    WorkflowInput,
    WorkflowOutput,
)
from src.governance.policy_engine.opa_client import PolicyEngine, PolicyResult

_TASK_QUEUE = "aegis-audit-provider-outage"
_TASK_COUNT = 50
_RECOVERY_LATENCY_SECONDS = 30.0


class _MonotonicRecordingAuditLogger(AuditLogger):
    """Capture audit entries with deterministic, strictly increasing timestamps."""

    def __init__(self) -> None:
        super().__init__("test.audit.provider-outage")
        self.entries: list[dict[str, Any]] = []
        self._clock = datetime(2026, 3, 9, 15, 0, 0, tzinfo=UTC)
        self._clock_lock = Lock()

    def _utcnow(self) -> datetime:
        with self._clock_lock:
            current = self._clock
            self._clock += timedelta(microseconds=1)
            return current

    def info(self, event: str, **kwargs: object) -> None:
        self.entries.append({"level": "info", "event": event, **kwargs})

    def warning(self, event: str, **kwargs: object) -> None:
        self.entries.append({"level": "warning", "event": event, **kwargs})

    def error(self, event: str, **kwargs: object) -> None:
        self.entries.append({"level": "error", "event": event, **kwargs})


class _OutageAfterOneSuccessAdapter(BaseAdapter):
    """Simulate a provider outage after the first task completes an LLM call.

    The first unique prompt succeeds immediately. Every other task fails exactly
    once with a retryable provider outage error. Once all remaining tasks have
    observed the outage, the simulated provider is restored and subsequent retry
    attempts succeed.
    """

    def __init__(self, task_count: int) -> None:
        self._task_count = task_count
        self._lock = asyncio.Lock()
        self._restored = asyncio.Event()
        self._first_success_prompt: str | None = None
        self._failed_prompts: set[str] = set()
        self.calls_by_prompt: dict[str, int] = defaultdict(int)
        self.restored_at: float | None = None

    @property
    def provider_name(self) -> str:
        return "provider-outage-stub"

    @property
    def first_success_prompt(self) -> str | None:
        return self._first_success_prompt

    @property
    def failed_prompt_count(self) -> int:
        return len(self._failed_prompts)

    async def complete(self, request: Any) -> LLMResponse:
        prompt = cast(str, request.prompt)

        async with self._lock:
            self.calls_by_prompt[prompt] += 1

            if self._first_success_prompt is None:
                self._first_success_prompt = prompt
                return self._success_response(prompt)

            if prompt not in self._failed_prompts:
                self._failed_prompts.add(prompt)
                if len(self._failed_prompts) == self._task_count - 1:
                    self.restored_at = time.monotonic()
                    self._restored.set()
                raise ApplicationError(
                    "Simulated provider outage",
                    type="ProviderOutageError",
                )

        await self._restored.wait()
        return self._success_response(prompt)

    def _success_response(self, prompt: str) -> LLMResponse:
        return LLMResponse(
            content=f"Recovered response for {prompt}",
            tokens_used=21,
            model="gpt-4o-mini",
            provider=self.provider_name,
            finish_reason="stop",
        )


def _allow_policy_engine() -> PolicyEngine:
    """Return a typed allow-all policy engine stub."""

    class _AllowPolicyEngine:
        async def evaluate(self, *_args: Any, **_kwargs: Any) -> PolicyResult:
            return PolicyResult(allowed=True, action="allow", reasons=[], fields=[])

    return cast(PolicyEngine, _AllowPolicyEngine())


def _workflow_input(task_index: int) -> WorkflowInput:
    task_id = str(uuid.uuid4())
    return WorkflowInput(
        task_id=task_id,
        prompt=f"Provider outage prompt {task_index:02d}",
        agent_type="general",
        requester_id="provider-outage-user",
        session_id=f"session-{task_id}",
    )


def _group_by_task(entries: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        task_id = entry.get("task_id")
        if isinstance(task_id, str):
            grouped[task_id].append(entry)
    return grouped


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provider_outage_recovery_has_complete_gapless_ordered_audit_trails() -> None:
    """A2-3: 50 concurrent tasks recover from outage with complete ordered audit trails."""
    audit = _MonotonicRecordingAuditLogger()
    adapter = _OutageAfterOneSuccessAdapter(_TASK_COUNT)
    activities = AegisActivities(
        adapter=adapter,
        audit_logger=audit,
        policy_engine=_allow_policy_engine(),
    )
    workflow_inputs = [_workflow_input(task_index) for task_index in range(_TASK_COUNT)]

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
            handles = [
                await env.client.start_workflow(
                    AgentTaskWorkflow.run,
                    workflow_input,
                    id=f"provider-outage-{workflow_input.task_id}",
                    task_queue=_TASK_QUEUE,
                )
                for workflow_input in workflow_inputs
            ]
            results: list[WorkflowOutput] = []
            for handle in handles:
                results.append(cast(WorkflowOutput, await handle.result()))

    assert len(results) == _TASK_COUNT
    assert all(result.workflow_status == "completed" for result in results)
    assert adapter.first_success_prompt is not None
    assert adapter.failed_prompt_count == _TASK_COUNT - 1
    assert adapter.restored_at is not None
    assert time.monotonic() - adapter.restored_at < _RECOVERY_LATENCY_SECONDS

    results_by_prompt = {
        workflow_input.prompt: result
        for workflow_input, result in zip(workflow_inputs, results)
    }
    results_by_task_id = {result.task_id: result for result in results}
    failed_task_ids = {
        result.task_id
        for prompt, result in results_by_prompt.items()
        if prompt != adapter.first_success_prompt
    }

    grouped_entries = _group_by_task(audit.entries)
    assert len(grouped_entries) == _TASK_COUNT

    retried_task_ids: set[str] = set()
    for task_id, entries in grouped_entries.items():
        sequence_numbers = [cast(int, entry["sequence_number"]) for entry in entries]
        assert sequence_numbers == list(range(len(entries))), task_id

        timestamps = [cast(str, entry["timestamp"]) for entry in entries]
        assert timestamps == sorted(timestamps), task_id

        lifecycle_events = [cast(str, entry["event"]) for entry in entries if "event_type" in entry]
        if task_id in failed_task_ids:
            assert lifecycle_events == ["workflow.started", "llm.retried", "workflow.completed"]
            retried_task_ids.add(task_id)
        else:
            assert lifecycle_events == ["workflow.started", "workflow.completed"]

        completed_event = next(entry for entry in entries if entry["event"] == "workflow.completed")
        assert completed_event["workflow_status"] == "completed"
        assert task_id in results_by_task_id

    assert retried_task_ids == failed_task_ids
