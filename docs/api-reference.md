# Aegis-OS API Reference

**Version**: 0.2.0 (current)  
**Base URL**: `http://localhost:18000` (local dev)  
**Format**: All request and response bodies are JSON.

> This document is a living reference. Phase 2 endpoints are placeholders;
> signatures will be finalised and tested before Gate 2.

---

## Table of Contents

- [Core Task Endpoints](#core-task-endpoints)
  - [POST /api/v1/tasks](#post-apiv1tasks)
  - [GET /api/v1/tasks/{task_id}](#get-apiv1taskstask_id)
- [Phase 2 — HITL Approval Endpoints (Placeholder)](#phase-2--hitl-approval-endpoints-placeholder)
  - [POST /api/v1/workflows/{workflow_id}/approve](#post-apiv1workflowsworkflow_idapprove)
  - [POST /api/v1/workflows/{workflow_id}/deny](#post-apiv1workflowsworkflow_iddeny)
- [Health](#health)

---

## Core Task Endpoints

### POST /api/v1/tasks

Submit a new agent task to the Aegis Governance Loop.

**Request body**

```json
{
  "prompt": "Summarise the Q1 audit findings.",
  "agent_type": "finance",
  "requester_id": "alice@example.com"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `prompt` | string | yes | The raw task prompt. PII is scrubbed before reaching the LLM. |
| `agent_type` | string | yes | One of the registered agent types (`finance`, `hr`, `it`, `legal`, `general`, `code_scalpel`). |
| `requester_id` | string | yes | Stable identity of the requesting user or service. |
| `budget_session_id` | UUID string | no | If omitted, a new budget session is created automatically. |

**Response `200 OK`**

```json
{
  "task_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "content": "Q1 audit findings summary...",
  "model": "gpt-4o-mini",
  "tokens_used": 312,
  "agent_type": "finance"
}
```

**Error responses**

| Status | Condition |
|---|---|
| `400` | Invalid request body or unknown `agent_type`. |
| `403` | OPA policy denied the request. |
| `402` | Budget cap exceeded for the session. |
| `500` | Internal server error (stage exception). |

---

### GET /api/v1/tasks/{task_id}

Retrieve the result and audit trail for a previously submitted task.

_Endpoint not yet implemented — placeholder for Phase 2._

---

## Phase 2 — HITL Approval Endpoints (Placeholder)

These endpoints will be implemented in Phase 2 (P2-x). The contract below is
the **discussion draft** to be reviewed by the Platform and Security teams
before any code is written.

> **Note**: All approve/deny actions are OPA-gated. The caller must present a
> valid JIT token whose `agent_type` maps to a principal with the `approve` or
> `deny` capability in `policies/agent_access.rego` (`rbac_capabilities`
> `ops_lead` role).

---

### POST /api/v1/workflows/{workflow_id}/approve

Approve a workflow that is waiting in `PendingApproval` state.

**Path parameters**

| Parameter | Description |
|---|---|
| `workflow_id` | UUID of the Temporal workflow in `PendingApproval`. |

**Request body**

```json
{
  "approver_id": "ops-lead@example.com",
  "reason": "Reviewed output; no policy violation found."
}
```

**Response `200 OK`**

```json
{
  "workflow_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "status": "approved",
  "approved_by": "ops-lead@example.com",
  "timestamp": "2026-03-05T12:00:00Z"
}
```

**Error responses**

| Status | Condition |
|---|---|
| `403` | Caller does not hold the `approve` RBAC capability. |
| `404` | Workflow not found or not in `PendingApproval` state. |
| `409` | Workflow already approved, denied, or timed out. |

---

### POST /api/v1/workflows/{workflow_id}/deny

Deny a workflow that is waiting in `PendingApproval` state, terminating it as `Failed`.

**Path parameters**

| Parameter | Description |
|---|---|
| `workflow_id` | UUID of the Temporal workflow in `PendingApproval`. |

**Request body**

```json
{
  "denier_id": "ops-lead@example.com",
  "reason": "Output contains policy violation; denying continuation."
}
```

**Response `200 OK`**

```json
{
  "workflow_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "status": "denied",
  "denied_by": "ops-lead@example.com",
  "timestamp": "2026-03-05T12:01:00Z"
}
```

**Error responses**

| Status | Condition |
|---|---|
| `403` | Caller does not hold the `deny` RBAC capability. |
| `404` | Workflow not found or not in `PendingApproval` state. |
| `409` | Workflow already approved, denied, or timed out. |

---

## Health

### GET /health

Returns `{"status": "ok"}` with HTTP 200 when the API is ready. Used by
Docker Compose `healthcheck` and load balancer probes.
