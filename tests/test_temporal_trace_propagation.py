"""A2-1 tests: task_id and traceparent survive Temporal workflow boundaries."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import threading
import uuid
from multiprocessing.managers import SyncManager
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from temporalio import activity
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.adapters.base import BaseAdapter, LLMRequest, LLMResponse
from src.audit_vault.logger import AuditLogger
from src.control_plane.scheduler import (
    AegisActivities,
    AgentTaskWorkflow,
    JITTokenInput,
    JITTokenResult,
    LLMInvokeInput,
    LLMInvokeResult,
    MissingTaskIdError,
    PolicyEvalInput,
    PolicyEvalResult,
    PostSanitizeInput,
    PostSanitizeResult,
    PrePIIScrubResult,
    WorkflowInput,
    WorkflowOutput,
)
from src.governance.policy_engine.opa_client import PolicyEngine, PolicyResult

_TASK_QUEUE = "aegis-trace-propagation"
_STAGE_ACTIVITY_NAMES = (
    "PrePIIScrub",
    "PolicyEval",
    "JITTokenIssue",
    "LLMInvoke",
    "PostSanitize",
)
_STAGE_SPAN_NAMES = {
    "pre-pii-scrub",
    "policy-eval",
    "jit-token-issue",
    "llm-invoke",
    "post-sanitize",
}
_MP_CONTEXT = multiprocessing.get_context("spawn")


class _StubAdapter(BaseAdapter):
    @property
    def provider_name(self) -> str:
        return "trace-propagation-adapter"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content="trace-propagation-ok",
            tokens_used=17,
            model=request.model,
            provider=self.provider_name,
            finish_reason="stop",
        )


class _RecordingAuditLogger(AuditLogger):
    def __init__(self) -> None:
        super().__init__("test.a2_1")
        self.events: list[dict[str, object]] = []

    def info(self, event: str, **kwargs: object) -> None:
        self.events.append({"level": "info", "event": event, **kwargs})

    def warning(self, event: str, **kwargs: object) -> None:
        self.events.append({"level": "warning", "event": event, **kwargs})

    def error(self, event: str, **kwargs: object) -> None:
        self.events.append({"level": "error", "event": event, **kwargs})


class _SharedAuditLogger(AuditLogger):
    def __init__(self, sink: Any) -> None:
        super().__init__("test.a2_1.shared")
        self._sink = sink

    def info(self, event: str, **kwargs: object) -> None:
        self._sink.append({"level": "info", "event": event, **kwargs})

    def warning(self, event: str, **kwargs: object) -> None:
        self._sink.append({"level": "warning", "event": event, **kwargs})

    def error(self, event: str, **kwargs: object) -> None:
        self._sink.append({"level": "error", "event": event, **kwargs})


def _make_policy_engine() -> PolicyEngine:
    engine = MagicMock(spec=PolicyEngine)
    engine.evaluate = AsyncMock(
        return_value=PolicyResult(allowed=True, action="allow", reasons=[], fields=[])
    )
    return engine


def _make_activities(
    *,
    tracer: Any | None = None,
    audit_logger: AuditLogger | None = None,
) -> AegisActivities:
    return AegisActivities(
        adapter=_StubAdapter(),
        policy_engine=_make_policy_engine(),
        tracer=tracer,
        audit_logger=audit_logger,
    )


def _make_workflow_input(
    *,
    traceparent: str | None = None,
    task_id: str | None = None,
) -> WorkflowInput:
    return WorkflowInput(
        task_id=task_id or str(uuid.uuid4()),
        prompt="Trace propagation workflow prompt",
        agent_type="general",
        requester_id="trace-user",
        traceparent=traceparent,
    )


def _get_temporal_target_host(env: WorkflowEnvironment) -> str:
    config = env.client.config()
    if isinstance(config, dict):
        target_host = config.get("target_host")
        if isinstance(target_host, str) and target_host:
            return target_host
    return env.client.service_client.config.target_host


def _decode_payload(payload: Any) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(payload.data.decode()))


def _workflow_started_input(history: Any) -> dict[str, Any]:
    for event in history.events:
        if event.HasField("workflow_execution_started_event_attributes"):
            payloads = event.workflow_execution_started_event_attributes.input.payloads
            assert payloads, "WorkflowExecutionStarted event is missing input payloads"
            return _decode_payload(payloads[0])
    raise AssertionError("WorkflowExecutionStarted event not found in history")


def _scheduled_activity_inputs(history: Any) -> dict[str, dict[str, Any]]:
    decoded: dict[str, dict[str, Any]] = {}
    for event in history.events:
        if not event.HasField("activity_task_scheduled_event_attributes"):
            continue
        attrs = event.activity_task_scheduled_event_attributes
        activity_name = attrs.activity_type.name
        payloads = attrs.input.payloads
        if not payloads or activity_name in decoded:
            continue
        decoded[activity_name] = _decode_payload(payloads[0])
    return decoded


def _make_traceparent(trace_id_hex: str, span_id_hex: str = "1111111111111111") -> str:
    return f"00-{trace_id_hex}-{span_id_hex}-01"


def _trace_id_from_traceparent(traceparent: str) -> str:
    return traceparent.split("-")[1]


def _schedule_worker_exit() -> None:
    threading.Timer(1.0, os._exit, args=(1,)).start()


def _run_trace_worker(
    target_host: str,
    die_after_stage: str | None,
    ready_event: Any,
    stage_done_events: dict[str, Any],
    audit_events: Any,
) -> None:
    async def _main() -> None:
        trace.set_tracer_provider(TracerProvider())
        client = await Client.connect(target_host)
        audit_logger = _SharedAuditLogger(audit_events)
        activities = _make_activities(audit_logger=audit_logger)

        @activity.defn(name="PrePIIScrub")
        async def _pre_pii_scrub(inp: WorkflowInput) -> PrePIIScrubResult:
            result = await activities.pre_pii_scrub(inp)
            stage_done_events["PrePIIScrub"].set()
            if die_after_stage == "PrePIIScrub":
                _schedule_worker_exit()
            return result

        @activity.defn(name="PolicyEval")
        async def _policy_eval(inp: PolicyEvalInput) -> PolicyEvalResult:
            result = await activities.policy_eval(inp)
            stage_done_events["PolicyEval"].set()
            if die_after_stage == "PolicyEval":
                _schedule_worker_exit()
            return result

        @activity.defn(name="JITTokenIssue")
        async def _jit_token_issue(inp: JITTokenInput) -> JITTokenResult:
            result = await activities.jit_token_issue(inp)
            stage_done_events["JITTokenIssue"].set()
            if die_after_stage == "JITTokenIssue":
                _schedule_worker_exit()
            return result

        @activity.defn(name="LLMInvoke")
        async def _llm_invoke(inp: LLMInvokeInput) -> LLMInvokeResult:
            result = await activities.llm_invoke(inp)
            stage_done_events["LLMInvoke"].set()
            if die_after_stage == "LLMInvoke":
                _schedule_worker_exit()
            return result

        @activity.defn(name="PostSanitize")
        async def _post_sanitize(inp: PostSanitizeInput) -> PostSanitizeResult:
            result = await activities.post_sanitize(inp)
            stage_done_events["PostSanitize"].set()
            if die_after_stage == "PostSanitize":
                _schedule_worker_exit()
            return result

        ready_event.set()
        async with Worker(
            client,
            task_queue=_TASK_QUEUE,
            workflows=[AgentTaskWorkflow],
            activities=[
                _pre_pii_scrub,
                _policy_eval,
                _jit_token_issue,
                _llm_invoke,
                _post_sanitize,
            ],
        ):
            await asyncio.sleep(60)

    asyncio.run(_main())


def _start_trace_worker(
    *,
    target_host: str,
    die_after_stage: str | None,
    manager: SyncManager,
) -> tuple[Any, Any, Any, Any]:
    ready_event = manager.Event()
    stage_done_events = {name: manager.Event() for name in _STAGE_ACTIVITY_NAMES}
    audit_events = manager.list()
    proc = _MP_CONTEXT.Process(
        target=_run_trace_worker,
        args=(target_host, die_after_stage, ready_event, stage_done_events, audit_events),
        daemon=True,
    )
    proc.start()
    return proc, ready_event, stage_done_events, audit_events


@pytest.mark.integration
@pytest.mark.asyncio
async def test_task_id_in_workflow_input() -> None:
    workflow_id = f"trace-taskid-start-{uuid.uuid4()}"
    fixed_task_id = str(uuid.uuid4())
    activities = _make_activities()
    workflow_input = _make_workflow_input(task_id=fixed_task_id)

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
                id=workflow_id,
                task_queue=_TASK_QUEUE,
            )
            history = await env.client.get_workflow_handle(workflow_id).fetch_history()

    assert result.task_id == fixed_task_id
    decoded_input = _workflow_started_input(history)
    assert decoded_input["task_id"] == fixed_task_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_task_id_in_every_activity_input() -> None:
    workflow_id = f"trace-taskid-activities-{uuid.uuid4()}"
    fixed_task_id = str(uuid.uuid4())
    activities = _make_activities()
    workflow_input = _make_workflow_input(task_id=fixed_task_id)

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
            await env.client.execute_workflow(
                AgentTaskWorkflow.run,
                workflow_input,
                id=workflow_id,
                task_queue=_TASK_QUEUE,
            )
            history = await env.client.get_workflow_handle(workflow_id).fetch_history()

    decoded_inputs = _scheduled_activity_inputs(history)
    assert set(decoded_inputs) >= set(_STAGE_ACTIVITY_NAMES)
    for activity_name in _STAGE_ACTIVITY_NAMES:
        assert decoded_inputs[activity_name]["task_id"] == fixed_task_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_traceparent_roundtrip() -> None:
    trace_id_hex = "1234567890abcdef1234567890abcdef"
    traceparent = _make_traceparent(trace_id_hex)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test.a2_1.traceparent")
    activities = _make_activities(tracer=tracer)
    workflow_input = _make_workflow_input(traceparent=traceparent)

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
            await env.client.execute_workflow(
                AgentTaskWorkflow.run,
                workflow_input,
                id=f"trace-roundtrip-{uuid.uuid4()}",
                task_queue=_TASK_QUEUE,
            )

    stage_spans = [span for span in exporter.get_finished_spans() if span.name in _STAGE_SPAN_NAMES]
    assert len(stage_spans) == 5
    expected_trace_id = int(trace_id_hex, 16)
    for span in stage_spans:
        assert span.context is not None
        assert span.context.trace_id == expected_trace_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_trace_unbroken_across_restart() -> None:
    trace_id_hex = "abcdefabcdefabcdefabcdefabcdefab"
    traceparent = _make_traceparent(trace_id_hex)
    workflow_input = _make_workflow_input(traceparent=traceparent)

    with _MP_CONTEXT.Manager() as manager:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            target_host = _get_temporal_target_host(env)
            proc1, ready1, events1, audit_events = _start_trace_worker(
                target_host=target_host,
                die_after_stage="PolicyEval",
                manager=manager,
            )
            assert ready1.wait(timeout=15), "Worker 1 did not become ready in time"

            handle = await env.client.start_workflow(
                AgentTaskWorkflow.run,
                workflow_input,
                id=f"trace-restart-{uuid.uuid4()}",
                task_queue=_TASK_QUEUE,
            )

            assert events1["PolicyEval"].wait(timeout=30)
            proc1.join(timeout=2)
            if proc1.is_alive():
                proc1.kill()
                proc1.join(timeout=2)

            proc2, ready2, _events2, _audit_events2 = _start_trace_worker(
                target_host=target_host,
                die_after_stage=None,
                manager=manager,
            )
            assert ready2.wait(timeout=15), "Worker 2 did not become ready in time"
            result = await asyncio.wait_for(handle.result(), timeout=30)
            captured_audit_events = list(audit_events)
            proc2.kill()
            proc2.join(timeout=2)

    assert result.workflow_status == "completed"
    stage_events = [
        event
        for event in captured_audit_events
        if event.get("event")
        in {
            "guardrails.pre_sanitize",
            "policy.allowed",
            "token.issued",
            "llm.completed",
            "guardrails.post_sanitize",
        }
        and event.get("task_id") == workflow_input.task_id
    ]
    assert len(stage_events) == 5
    observed_trace_ids = {
        _trace_id_from_traceparent(str(event["traceparent"])) for event in stage_events
    }
    assert observed_trace_ids == {trace_id_hex}


@pytest.mark.asyncio
async def test_missing_task_id_raises_propagation_error() -> None:
    audit_logger = _RecordingAuditLogger()
    activities = _make_activities(audit_logger=audit_logger)
    bad_input = PolicyEvalInput(
        task_id="placeholder-task-id",
        sanitized_prompt="already sanitized",
        agent_type="general",
        requester_id="trace-user",
        model="gpt-4o-mini",
    )
    bad_input.task_id = cast(Any, None)

    with pytest.raises(MissingTaskIdError):
        await activities.policy_eval(bad_input)

    propagation_errors = [
        event for event in audit_logger.events if event.get("event") == "audit.propagation_error"
    ]
    assert propagation_errors, "Expected audit.propagation_error event for missing task_id"
