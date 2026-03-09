"""A2-4 tests for per-task audit timestamp ordering guarantees."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import MutableMapping
from datetime import UTC, datetime, timedelta

import pytest
from structlog.testing import capture_logs

from src.audit_vault.logger import AuditLogger, AuditOrderingError, LifecycleEvent


def _group_task_entries(
    entries: list[MutableMapping[str, object]],
) -> dict[str, list[MutableMapping[str, object]]]:
    grouped: dict[str, list[MutableMapping[str, object]]] = defaultdict(list)
    for entry in entries:
        task_id = entry.get("task_id")
        if isinstance(task_id, str):
            grouped[task_id].append(entry)
    return grouped


def _timestamps(entries: list[MutableMapping[str, object]]) -> list[str]:
    return [str(entry["timestamp"]) for entry in entries]


class TestAuditTimestampOrdering:
    """A2-4 unit coverage for task-scoped audit ordering."""

    def test_monotonic_timestamps_for_100_tasks(self) -> None:
        """Each task stream must have non-decreasing timestamps across repeated events."""
        logger = AuditLogger("test.audit-ordering.monotonic")

        with capture_logs() as logs:
            for task_index in range(100):
                task_id = f"task-{task_index:03d}"
                for stage_index in range(5):
                    logger.stage_event(
                        "audit.stage.progress",
                        outcome="allow",
                        stage=f"stage-{stage_index + 1}",
                        task_id=task_id,
                        agent_type="general",
                    )

        grouped = _group_task_entries(logs)
        assert len(grouped) == 100
        for task_id, entries in grouped.items():
            observed_timestamps = _timestamps(entries)
            assert observed_timestamps == sorted(observed_timestamps), task_id
            assert [entry["sequence_number"] for entry in entries] == list(range(5))

    def test_clock_skew_emits_warning_and_clamps_timestamp(self) -> None:
        """Backward wall-clock drift must emit a warning and keep task timestamps ordered."""
        logger = AuditLogger("test.audit-ordering.skew")
        base = datetime(2026, 3, 9, 12, 0, 0, tzinfo=UTC)
        times = iter(
            [
                base,
                base + timedelta(seconds=1),
                base + timedelta(seconds=2),
                base + timedelta(seconds=1, milliseconds=500),
                base + timedelta(seconds=3),
            ]
        )
        logger._utcnow = lambda: next(times)  # type: ignore[method-assign]

        with capture_logs() as logs:
            for idx in range(5):
                logger.lifecycle_event(
                    "workflow.progress",
                    event_type=LifecycleEvent.STARTED.value,
                    task_id="skew-task",
                    agent_type="general",
                    session_id="session-skew",
                    workflow_status="running",
                    stage=f"stage-{idx + 1}",
                )

        task_entries = [entry for entry in logs if entry.get("task_id") == "skew-task"]
        warning_entries = [
            entry for entry in task_entries if entry.get("event") == "audit.clock_skew_warning"
        ]
        business_entries = [
            entry for entry in task_entries if entry.get("event") != "audit.clock_skew_warning"
        ]

        assert len(warning_entries) == 1
        assert warning_entries[0]["skew_ms"] == 500
        assert _timestamps(business_entries) == sorted(_timestamps(business_entries))
        assert [entry["sequence_number"] for entry in task_entries] == list(range(6))

    @pytest.mark.asyncio
    async def test_concurrent_tasks_do_not_interfere(self) -> None:
        """Interleaved task streams must still be monotonic within each task."""
        logger = AuditLogger("test.audit-ordering.concurrent")

        async def emit_task(task_index: int) -> None:
            task_id = f"concurrent-{task_index:02d}"
            for stage_index in range(5):
                logger.stage_event(
                    "audit.concurrent.progress",
                    outcome="allow",
                    stage=f"stage-{stage_index + 1}",
                    task_id=task_id,
                    agent_type="general",
                )
                await asyncio.sleep(0)

        with capture_logs() as logs:
            await asyncio.gather(*(emit_task(task_index) for task_index in range(20)))

        grouped = _group_task_entries(logs)
        assert len(grouped) == 20
        for task_id, entries in grouped.items():
            observed_timestamps = _timestamps(entries)
            assert observed_timestamps == sorted(observed_timestamps), task_id
            assert [entry["sequence_number"] for entry in entries] == list(range(5))

    def test_explicit_out_of_order_timestamp_is_blocked(self) -> None:
        """Explicit earlier timestamps must raise and must not be emitted."""
        logger = AuditLogger("test.audit-ordering.explicit")
        base = datetime(2026, 3, 9, 13, 0, 0, tzinfo=UTC)

        with capture_logs() as logs:
            logger.lifecycle_event(
                "workflow.started",
                event_type=LifecycleEvent.STARTED,
                task_id="explicit-order-task",
                agent_type="general",
                session_id="session-explicit",
                workflow_status="running",
                timestamp_override=base,
            )

            with pytest.raises(AuditOrderingError):
                logger.lifecycle_event(
                    "workflow.completed",
                    event_type=LifecycleEvent.COMPLETED,
                    task_id="explicit-order-task",
                    agent_type="general",
                    session_id="session-explicit",
                    workflow_status="completed",
                    timestamp_override=base - timedelta(milliseconds=1),
                )

        task_entries = [entry for entry in logs if entry.get("task_id") == "explicit-order-task"]
        assert len(task_entries) == 1
        assert task_entries[0]["event"] == "workflow.started"
