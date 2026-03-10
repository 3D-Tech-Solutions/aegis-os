"""Live A2-3 outage-latency validation using the Compose-managed toxiproxy service."""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, cast

import httpx
import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.adapters.local_llama import LocalLlamaAdapter
from src.audit_vault.logger import AuditLogger
from src.control_plane.scheduler import (
    AegisActivities,
    AgentTaskWorkflow,
    WorkflowInput,
    WorkflowOutput,
)
from src.governance.policy_engine.opa_client import PolicyEngine, PolicyResult

REPO_ROOT = Path(__file__).parent.parent
_TASK_QUEUE = "aegis-audit-provider-outage-latency"
_TOXIPROXY_ADMIN_URL = "http://localhost:18474"
_TOXIPROXY_PROXY_NAME = "local-llama-outage-latency"
_TOXIPROXY_LISTEN_PORT = 18666
_RECOVERY_LATENCY_SECONDS = 30.0


class _RecordingAuditLogger(AuditLogger):
    """Capture emitted audit entries with deterministic monotonic timestamps."""

    def __init__(self) -> None:
        super().__init__("test.audit.provider-outage-latency")
        self.entries: list[dict[str, Any]] = []
        self._clock = datetime(2026, 3, 9, 16, 0, 0, tzinfo=UTC)
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


def _allow_policy_engine() -> PolicyEngine:
    """Return a typed allow-all policy engine stub."""

    class _AllowPolicyEngine:
        async def evaluate(self, *_args: Any, **_kwargs: Any) -> PolicyResult:
            return PolicyResult(allowed=True, action="allow", reasons=[], fields=[])

    return cast(PolicyEngine, _AllowPolicyEngine())


class _OpenAICompatHandler(http.server.BaseHTTPRequestHandler):
    """Serve a minimal OpenAI-compatible chat completions endpoint."""

    state: dict[str, int] = {"request_count": 0}

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        type(self).state["request_count"] += 1
        prompt = payload["messages"][-1]["content"]

        response_body = {
            "choices": [
                {
                    "message": {"content": f"Recovered live response for {prompt}"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"total_tokens": 17},
        }
        encoded = json.dumps(response_body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


@contextlib.contextmanager
def _serve_openai_compatible_model() -> Iterator[tuple[str, dict[str, int]]]:
    """Run a local HTTP server reachable from the toxiproxy container."""
    shared_state = {"request_count": 0}

    class _DynamicOpenAICompatHandler(_OpenAICompatHandler):
        state = shared_state

    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), _DynamicOpenAICompatHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"host.docker.internal:{server.server_port}", shared_state
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _run_compose(*args: str) -> None:
    """Run a docker-compose command in the repository root."""
    subprocess.run(
        ["docker-compose", *args],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def _delete_proxy_if_present(client: httpx.Client) -> None:
    """Delete the configured toxiproxy proxy, ignoring missing-proxy responses."""
    response = client.delete(f"{_TOXIPROXY_ADMIN_URL}/proxies/{_TOXIPROXY_PROXY_NAME}")
    if response.status_code not in {200, 204, 404}:
        response.raise_for_status()


@contextlib.contextmanager
def _managed_toxiproxy_proxy(*, upstream: str, enabled: bool) -> Iterator[None]:
    """Create and later remove the fixed toxiproxy proxy used by the live test."""
    _run_compose("up", "-d", "toxiproxy")
    with httpx.Client(timeout=5.0) as client:
        _delete_proxy_if_present(client)
        response = client.post(
            f"{_TOXIPROXY_ADMIN_URL}/proxies",
            json={
                "name": _TOXIPROXY_PROXY_NAME,
                "listen": f"0.0.0.0:{_TOXIPROXY_LISTEN_PORT}",
                "upstream": upstream,
                "enabled": enabled,
            },
        )
        response.raise_for_status()
        try:
            yield
        finally:
            _delete_proxy_if_present(client)


def _set_proxy_enabled(enabled: bool) -> None:
    """Toggle the fixed toxiproxy proxy on or off."""
    with httpx.Client(timeout=5.0) as client:
        response = client.post(
            f"{_TOXIPROXY_ADMIN_URL}/proxies/{_TOXIPROXY_PROXY_NAME}",
            json={"enabled": enabled},
        )
        response.raise_for_status()


def _workflow_input() -> WorkflowInput:
    """Return a single workflow input for the live outage-latency test."""
    task_id = str(uuid.uuid4())
    return WorkflowInput(
        task_id=task_id,
        prompt="Provider outage latency prompt",
        agent_type="general",
        requester_id="provider-outage-user",
        session_id=f"session-{task_id}",
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_outage_latency_recovers_within_30_seconds_after_toxiproxy_restoration() -> None:
    """A2-3: live network outage recovers within the 30-second post-restore window."""
    audit = _RecordingAuditLogger()
    workflow_input = _workflow_input()

    with _serve_openai_compatible_model() as (upstream, model_state):
        with _managed_toxiproxy_proxy(upstream=upstream, enabled=False):
            adapter = LocalLlamaAdapter(
                base_url=f"http://localhost:{_TOXIPROXY_LISTEN_PORT}/v1",
                audit_logger=audit,
            )
            activities = AegisActivities(
                adapter=adapter,
                audit_logger=audit,
                policy_engine=_allow_policy_engine(),
            )

            restored_at: dict[str, float] = {}

            async def restore_proxy() -> None:
                await asyncio.sleep(0.5)
                _set_proxy_enabled(True)
                restored_at["time"] = time.monotonic()

            async with await WorkflowEnvironment.start_local() as env:
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
                    restore_task = asyncio.create_task(restore_proxy())
                    handle = await env.client.start_workflow(
                        AgentTaskWorkflow.run,
                        workflow_input,
                        id=f"provider-outage-latency-{workflow_input.task_id}",
                        task_queue=_TASK_QUEUE,
                    )
                    result = await handle.result()
                    await restore_task
                    completed_at = time.monotonic()

    assert isinstance(result, WorkflowOutput)
    assert result.workflow_status == "completed"
    assert restored_at
    assert completed_at - restored_at["time"] < _RECOVERY_LATENCY_SECONDS
    assert model_state["request_count"] == 1

    task_entries = [
        entry for entry in audit.entries if entry.get("task_id") == workflow_input.task_id
    ]
    sequence_numbers = [cast(int, entry["sequence_number"]) for entry in task_entries]
    assert sequence_numbers == list(range(len(task_entries)))

    timestamps = [cast(str, entry["timestamp"]) for entry in task_entries]
    assert timestamps == sorted(timestamps)

    lifecycle_events = [
        cast(str, entry["event"])
        for entry in task_entries
        if "event_type" in entry
    ]
    assert lifecycle_events == ["workflow.started", "llm.retried", "workflow.completed"]
