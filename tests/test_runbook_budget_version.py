"""Regression checks for the Phase 2 budget runbook wording."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
BUDGET_RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "budget-exceeded.md"


def test_budget_runbook_mentions_phase2_hitl_terms() -> None:
    content = BUDGET_RUNBOOK.read_text(encoding="utf-8")
    for term in ["PendingApproval", "approve", "deny"]:
        assert term in content, f"Missing Phase 2 term {term!r} from budget runbook"


def test_budget_runbook_includes_hitl_approval_flow_section() -> None:
    content = BUDGET_RUNBOOK.read_text(encoding="utf-8")
    assert "## HITL Approval Flow" in content


def test_budget_runbook_does_not_reference_legacy_approvals_endpoint() -> None:
    content = BUDGET_RUNBOOK.read_text(encoding="utf-8")
    assert "/api/v1/approvals" not in content
