"""Audit Vault Logger - high-fidelity, append-only trace logging via structlog.

Note: This module does NOT configure the global OpenTelemetry TracerProvider at
import time.  Provider setup belongs at application startup (e.g. ``src/main.py``).
Test suites and the application runtime are therefore free to install their own
provider before any ``AuditLogger`` instance is created.
"""

from collections import defaultdict
from collections.abc import MutableMapping
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock
from typing import Any

import structlog
from opentelemetry import trace


def _format_utc_timestamp(value: datetime) -> str:
    """Return an ISO-8601 UTC timestamp string with microsecond precision."""
    normalized = value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _add_timestamp_if_missing(
    _logger: Any,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Add a UTC timestamp only when the caller did not provide one explicitly."""
    event_dict.setdefault("timestamp", _format_utc_timestamp(datetime.now(UTC)))
    return event_dict

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        _add_timestamp_if_missing,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)


class LifecycleEvent(StrEnum):
    """Audit event name constants for Phase 2 task lifecycle transitions (A-prep-2).

    These constants are defined here so every team can reference a single
    vocabulary before the Phase 2 implementation ships.  They map directly
    to the ``lifecycle_status`` values added to ``docs/audit-event-schema.json``.
    """

    STARTED = "task.started"
    RETRIED = "task.retried"
    PENDING_APPROVAL = "task.pending_approval"
    APPROVED = "task.approved"
    DENIED = "task.denied"
    COMPLETED = "task.completed"
    FAILED = "task.failed"


class AuditOrderingError(RuntimeError):
    """Raised when a caller attempts to emit a task event out of timestamp order."""


EXPECTED_PHASE2_LIFECYCLE_EVENTS: tuple[LifecycleEvent, ...] = (
    LifecycleEvent.STARTED,
    LifecycleEvent.COMPLETED,
)


class AuditLogger:
    """Structured, append-only logger for all agent actions and system events.

    Every log entry is emitted as JSON to stdout (captured by the log aggregator)
    and optionally correlated with an OpenTelemetry trace span.
    """

    def __init__(self, component: str = "aegis-os") -> None:
        self._log = structlog.get_logger(component)
        # Obtain the tracer from whichever provider is active at construction time.
        # Callers are responsible for configuring the global provider before
        # instantiating AuditLogger (e.g. in application startup or test fixtures).
        self._tracer = trace.get_tracer(component)
        # Per-task sequence number counters — keyed on task_id string.
        # Each new task_id sees an independent counter starting at 0.
        # Protected by a lock so the class is correct under both asyncio
        # interleaving and threading (e.g. multi-threaded test runners).
        self._seq_counters: defaultdict[str, int] = defaultdict(int)
        self._last_timestamps: dict[str, datetime] = {}
        self._seq_lock: Lock = Lock()

    def info(self, event: str, **kwargs: object) -> None:
        """Log an informational audit event."""
        self._log.info(event, **kwargs)

    def warning(self, event: str, **kwargs: object) -> None:
        """Log a warning audit event."""
        self._log.warning(event, **kwargs)

    def error(self, event: str, **kwargs: object) -> None:
        """Log an error audit event."""
        self._log.error(event, **kwargs)

    @staticmethod
    def _current_traceparent() -> str:
        """Return the W3C ``traceparent`` header for the currently active OTel span.

        Format: ``00-{trace_id:032x}-{span_id:016x}-{flags:02x}``

        If no valid span is active (e.g. outside a traced request or in unit
        tests without a configured provider), returns the all-zero no-trace
        traceparent ``00-000...000-000...000-00``.
        """
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if not ctx.is_valid:
            return "00-00000000000000000000000000000000-0000000000000000-00"
        sampled = "01" if (int(ctx.trace_flags) & 0x01) else "00"
        return f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-{sampled}"

    def audit(self, event: str, agent_id: str, action: str, **kwargs: object) -> None:
        """Log a security-relevant audit event with agent identity and action."""
        with self._tracer.start_as_current_span(event) as span:
            span.set_attribute("agent_id", agent_id)
            span.set_attribute("action", action)
            self._log.info(event, agent_id=agent_id, action=action, **kwargs)

    def _utcnow(self) -> datetime:
        """Return the current UTC timestamp.

        Tests patch this method to simulate clock skew deterministically.
        """
        return datetime.now(UTC)

    @staticmethod
    def _normalize_timestamp(value: datetime) -> datetime:
        """Return a timezone-aware UTC datetime for ordering comparisons."""
        return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)

    def _reserve_task_event_metadata(
        self,
        task_id: str,
        *,
        timestamp_override: datetime | None = None,
    ) -> tuple[int, str, dict[str, object] | None]:
        """Reserve sequence/timestamp metadata for a task-scoped audit event.

        When the wall clock moves backward relative to the previously emitted
        event for the same task, the logger clamps the emitted timestamp to the
        last known timestamp and returns metadata for an ``audit.clock_skew_warning``
        event. Explicit timestamp overrides are treated as authoritative and will
        raise :class:`AuditOrderingError` instead of being silently rewritten.
        """
        candidate = self._normalize_timestamp(timestamp_override or self._utcnow())

        with self._seq_lock:
            previous = self._last_timestamps.get(task_id)
            if timestamp_override is not None and previous is not None and candidate < previous:
                raise AuditOrderingError(
                    "Cannot emit audit event with timestamp earlier than the previous "
                    f"event for task_id={task_id!r}"
                )

            warning_payload: dict[str, object] | None = None
            effective = candidate
            if timestamp_override is None and previous is not None and candidate < previous:
                warning_sequence_number = self._seq_counters[task_id]
                self._seq_counters[task_id] += 1
                effective = previous
                warning_payload = {
                    "sequence_number": warning_sequence_number,
                    "timestamp": _format_utc_timestamp(effective),
                    "previous_timestamp": _format_utc_timestamp(previous),
                    "attempted_timestamp": _format_utc_timestamp(candidate),
                    "adjusted_timestamp": _format_utc_timestamp(effective),
                    "skew_ms": int((previous - candidate).total_seconds() * 1000),
                }

            sequence_number = self._seq_counters[task_id]
            self._seq_counters[task_id] += 1
            self._last_timestamps[task_id] = effective
            return sequence_number, _format_utc_timestamp(effective), warning_payload

    def _emit_clock_skew_warning(
        self,
        *,
        task_id: str,
        agent_type: str,
        stage: str,
        traceparent: str,
        warning_payload: dict[str, object],
    ) -> None:
        """Emit a task-scoped warning when the wall clock moves backward."""
        self.warning(
            "audit.clock_skew_warning",
            task_id=task_id,
            agent_type=agent_type,
            stage=stage,
            traceparent=traceparent,
            **warning_payload,
        )

    def stage_event(
        self,
        event: str,
        *,
        outcome: str,
        stage: str,
        task_id: str,
        agent_type: str,
        timestamp_override: datetime | None = None,
        **kwargs: object,
    ) -> None:
        """Emit a structured audit event for a pipeline stage outcome (A1-2).

        Every call guarantees the ``outcome``, ``stage``, ``task_id``, and
        ``agent_type`` fields appear in the emitted entry, making it validatable
        against ``docs/audit-event-schema.json``.

        Args:
            event: Human-readable event name (e.g. ``guardrails.pre_sanitize``).
            outcome: One of ``allow``, ``deny``, ``redact``, or ``error``.
            stage: OTel span name of the pipeline stage (e.g. ``pre-pii-scrub``).
            task_id: Task UUID string for audit trail correlation.
            agent_type: Agent type that initiated the pipeline run.
            **kwargs: Additional context fields (e.g. ``pii_types``, ``model``).

        Outcome routing:
            * ``allow`` / ``redact``  → ``info`` level.
            * ``deny``                → ``warning`` level.
            * ``error``               → ``error`` level.

        A monotonically increasing ``sequence_number`` is assigned per
        ``task_id`` and included in every emitted entry.  The first event
        for a given ``task_id`` receives ``sequence_number=0``; each
        subsequent event increments by 1.  This field enables gap and
        duplicate detection in audit trail verification (A1-3).
        """
        traceparent = self._current_traceparent()
        sequence_number, timestamp, warning_payload = self._reserve_task_event_metadata(
            task_id,
            timestamp_override=timestamp_override,
        )
        if warning_payload is not None:
            self._emit_clock_skew_warning(
                task_id=task_id,
                agent_type=agent_type,
                stage=stage,
                traceparent=traceparent,
                warning_payload=warning_payload,
            )

        payload = {
            "outcome": outcome,
            "stage": stage,
            "task_id": task_id,
            "agent_type": agent_type,
            "sequence_number": sequence_number,
            "traceparent": traceparent,
            "timestamp": timestamp,
            **kwargs,
        }

        if outcome == "error":
            self.error(event, **payload)
        elif outcome == "deny":
            self.warning(event, **payload)
        else:
            self.info(event, **payload)

    def lifecycle_event(
        self,
        event: str,
        *,
        event_type: LifecycleEvent | str,
        task_id: str,
        agent_type: str,
        session_id: str | None,
        workflow_status: str,
        stage: str = "workflow-lifecycle",
        timestamp_override: datetime | None = None,
        **kwargs: object,
    ) -> None:
        """Emit a structured lifecycle event with mandatory Phase 2 metadata."""
        traceparent = self._current_traceparent()
        sequence_number, timestamp, warning_payload = self._reserve_task_event_metadata(
            task_id,
            timestamp_override=timestamp_override,
        )
        if warning_payload is not None:
            self._emit_clock_skew_warning(
                task_id=task_id,
                agent_type=agent_type,
                stage=stage,
                traceparent=traceparent,
                warning_payload=warning_payload,
            )

        event_type_value = (
            event_type.value if isinstance(event_type, LifecycleEvent) else event_type
        )
        payload = {
            "event_type": event_type_value,
            "stage": stage,
            "task_id": task_id,
            "agent_type": agent_type,
            "session_id": session_id or "",
            "workflow_status": workflow_status,
            "sequence_number": sequence_number,
            "traceparent": traceparent,
            "timestamp": timestamp,
            **kwargs,
        }

        if event_type_value == LifecycleEvent.FAILED.value:
            self.error(event, **payload)
        elif event_type_value == LifecycleEvent.DENIED.value:
            self.warning(event, **payload)
        else:
            self.info(event, **payload)
