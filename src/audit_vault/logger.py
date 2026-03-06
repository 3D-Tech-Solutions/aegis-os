"""Audit Vault Logger - high-fidelity, append-only trace logging via structlog.

Note: This module does NOT configure the global OpenTelemetry TracerProvider at
import time.  Provider setup belongs at application startup (e.g. ``src/main.py``).
Test suites and the application runtime are therefore free to install their own
provider before any ``AuditLogger`` instance is created.
"""

from collections import defaultdict
from enum import StrEnum
from threading import Lock

import structlog
from opentelemetry import trace

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
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

    def stage_event(
        self,
        event: str,
        *,
        outcome: str,
        stage: str,
        task_id: str,
        agent_type: str,
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
        with self._seq_lock:
            sequence_number = self._seq_counters[task_id]
            self._seq_counters[task_id] += 1

        traceparent = self._current_traceparent()

        if outcome == "error":
            self.error(
                event,
                outcome=outcome,
                stage=stage,
                task_id=task_id,
                agent_type=agent_type,
                sequence_number=sequence_number,
                traceparent=traceparent,
                **kwargs,
            )
        elif outcome == "deny":
            self.warning(
                event,
                outcome=outcome,
                stage=stage,
                task_id=task_id,
                agent_type=agent_type,
                sequence_number=sequence_number,
                traceparent=traceparent,
                **kwargs,
            )
        else:
            self.info(
                event,
                outcome=outcome,
                stage=stage,
                task_id=task_id,
                agent_type=agent_type,
                sequence_number=sequence_number,
                traceparent=traceparent,
                **kwargs,
            )
