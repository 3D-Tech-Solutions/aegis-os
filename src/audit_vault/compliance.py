"""Compliance - SOC2/GDPR report generators for the Audit Vault."""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class ComplianceFramework(StrEnum):
    SOC2 = "SOC2"
    GDPR = "GDPR"


class AuditEvent(BaseModel):
    """A single immutable audit event record."""

    event_id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    agent_id: str
    action: str
    resource: str
    outcome: str
    metadata: dict[str, str] = Field(default_factory=dict)


class ComplianceReport(BaseModel):
    """A compliance report generated from audit events."""

    report_id: UUID = Field(default_factory=uuid4)
    framework: ComplianceFramework
    generated_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    period_start: datetime
    period_end: datetime
    total_events: int
    events: list[AuditEvent]
    summary: str


class ComplianceReporter:
    """Generates SOC2 and GDPR compliance reports from the audit event store."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    def record_event(self, event: AuditEvent) -> None:
        """Append an audit event to the in-memory store (use a DB in production)."""
        self._events.append(event)

    def generate_report(
        self,
        framework: ComplianceFramework,
        period_start: datetime,
        period_end: datetime,
    ) -> ComplianceReport:
        """Generate a compliance report for the given framework and time period."""
        in_scope = [
            e
            for e in self._events
            if period_start <= e.timestamp <= period_end
        ]

        if framework == ComplianceFramework.SOC2:
            summary = self._soc2_summary(in_scope)
        else:
            summary = self._gdpr_summary(in_scope)

        return ComplianceReport(
            framework=framework,
            period_start=period_start,
            period_end=period_end,
            total_events=len(in_scope),
            events=in_scope,
            summary=summary,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _soc2_summary(self, events: list[AuditEvent]) -> str:
        failed = [e for e in events if e.outcome == "failure"]
        return (
            f"SOC2 Audit Summary: {len(events)} total events, "
            f"{len(failed)} failures recorded."
        )

    def _gdpr_summary(self, events: list[AuditEvent]) -> str:
        pii_access = [e for e in events if "pii" in e.resource.lower()]
        return (
            f"GDPR Audit Summary: {len(events)} total events, "
            f"{len(pii_access)} PII-related access events."
        )
