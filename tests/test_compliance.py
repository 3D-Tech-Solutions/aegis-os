"""Tests for the Audit Vault Compliance reporter."""

from datetime import UTC, datetime, timedelta

import pytest

from src.audit_vault.compliance import (
    AuditEvent,
    ComplianceFramework,
    ComplianceReport,
    ComplianceReporter,
)


@pytest.fixture()
def reporter() -> ComplianceReporter:
    return ComplianceReporter()


@pytest.fixture()
def now() -> datetime:
    return datetime.now(tz=UTC)


def make_event(agent_id: str, action: str, resource: str, outcome: str = "success") -> AuditEvent:
    return AuditEvent(
        agent_id=agent_id,
        action=action,
        resource=resource,
        outcome=outcome,
    )


def test_report_with_no_events(reporter: ComplianceReporter, now: datetime) -> None:
    report = reporter.generate_report(
        ComplianceFramework.SOC2,
        period_start=now - timedelta(hours=1),
        period_end=now + timedelta(hours=1),
    )
    assert report.total_events == 0
    assert report.events == []
    assert report.framework == ComplianceFramework.SOC2


def test_soc2_report_counts_events(reporter: ComplianceReporter, now: datetime) -> None:
    reporter.record_event(make_event("agent-1", "read", "finance_db"))
    reporter.record_event(make_event("agent-2", "write", "reports", outcome="failure"))

    report = reporter.generate_report(
        ComplianceFramework.SOC2,
        period_start=now - timedelta(hours=1),
        period_end=now + timedelta(hours=1),
    )
    assert report.total_events == 2
    assert "2 total events" in report.summary
    assert "1 failures" in report.summary


def test_gdpr_report_counts_pii_events(reporter: ComplianceReporter, now: datetime) -> None:
    reporter.record_event(make_event("agent-1", "read", "pii_table"))
    reporter.record_event(make_event("agent-2", "read", "general_data"))

    report = reporter.generate_report(
        ComplianceFramework.GDPR,
        period_start=now - timedelta(hours=1),
        period_end=now + timedelta(hours=1),
    )
    assert report.total_events == 2
    assert "1 PII-related" in report.summary


def test_report_excludes_events_outside_period(
    reporter: ComplianceReporter, now: datetime
) -> None:
    reporter.record_event(make_event("agent-old", "read", "old_table"))
    report = reporter.generate_report(
        ComplianceFramework.SOC2,
        period_start=now + timedelta(hours=1),
        period_end=now + timedelta(hours=2),
    )
    assert report.total_events == 0


def test_report_returns_compliance_report_model(
    reporter: ComplianceReporter, now: datetime
) -> None:
    report = reporter.generate_report(
        ComplianceFramework.GDPR,
        period_start=now - timedelta(hours=1),
        period_end=now + timedelta(hours=1),
    )
    assert isinstance(report, ComplianceReport)
