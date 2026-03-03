"""Audit Vault Logger - high-fidelity, append-only trace logging via structlog."""

import structlog
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

# Configure OpenTelemetry tracer
_provider = TracerProvider()
_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(_provider)

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
        self._tracer = trace.get_tracer(component)

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
