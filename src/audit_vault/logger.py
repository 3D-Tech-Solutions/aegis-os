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

"""Audit Vault Logger - high-fidelity, append-only trace logging via structlog.

Note: This module does NOT configure the global OpenTelemetry TracerProvider at
import time.  Provider setup belongs at application startup (e.g. ``src/main.py``).
Test suites and the application runtime are therefore free to install their own
provider before any ``AuditLogger`` instance is created.
"""

from collections import defaultdict
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

        if outcome == "error":
            self.error(
                event,
                outcome=outcome,
                stage=stage,
                task_id=task_id,
                agent_type=agent_type,
                sequence_number=sequence_number,
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
                **kwargs,
            )
