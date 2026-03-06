# Runbook: HITL Approval Workflow Stuck

**Alert**: `HITLApprovalStuck`  
**Severity**: Warning  
**Metric**: `aegis_hitl_stuck_seconds > 86400`  
**Phase**: Phase 2 (placeholder — fill in before Gate 2)

---

## Symptoms

- Prometheus alert `HITLApprovalStuck` fires.
- `aegis_hitl_stuck_seconds{workflow_id="..."}` exceeds 86 400 (24 hours).
- Temporal UI (http://localhost:18080) shows a workflow in `PendingApproval` state with no recent signal activity.
- Downstream tasks that depend on the approval are blocked.

---

## Diagnosis

_Fill in before Gate 2._

Steps to identify the stuck workflow and determine whether it is waiting on a missing approver, a misconfigured signal channel, or a code defect.

---

## Escalation

_Fill in before Gate 2._

Who to page, what information to include, and the SLA for escalation to `ops_lead` vs. engineering on-call.

---

## Resolution

_Fill in before Gate 2._

How to approve or deny a stuck workflow from the CLI, from the Temporal UI, and via the `/api/v1/workflows/{id}/approve` and `/api/v1/workflows/{id}/deny` REST endpoints.

---

## Post-Incident

_Fill in before Gate 2._

Steps to confirm the workflow resumed or terminated cleanly, audit event verification, and Prometheus metric reset.
