# ADR-001: Governance Architecture - Policy-as-Code with OPA

**Status**: Accepted  
**Date**: 2026-03-03  
**Deciders**: Aegis-OS Core Team

## Context

AI agents executing autonomous tasks need clearly defined, auditable permissions. Static API keys and hard-coded rules do not scale and cannot be changed without a code deployment.

## Decision

Use Open Policy Agent (OPA) with Rego policies as the Policy-as-Code engine for all agent access decisions. Policies live in `policies/` and are loaded into the OPA server at startup.

## Consequences

- **Positive**: Policies can be updated independently of application code.  
- **Positive**: Every access decision is logged and auditable.  
- **Negative**: Adds an OPA server dependency; mitigated by the Docker Compose setup.

---

# ADR-002: Just-In-Time Agent Tokens

**Status**: Accepted  
**Date**: 2026-03-03

## Context

Long-lived API keys have a large blast radius if compromised.

## Decision

Issue short-lived (15-minute) JWT tokens scoped to a specific agent type and requester via `src/governance/session_mgr.py`. The current production direction is sender-constrained `ES256` tokens backed by Vault-managed signing material and bound to DPoP proofs where protected downstream calls require proof-of-possession. Legacy `HS256` bearer-token validation may remain enabled only as a temporary compatibility path during migration.

## Consequences

- **Positive**: Compromised tokens expire quickly.  
- **Positive**: Token scope limits lateral movement, and DPoP binding reduces replay value for protected flows.  
- **Negative**: Agents must renew tokens; handled transparently by the Control Plane.

---

# ADR-003: Circuit Breaker for Agent Loop Detection

**Status**: Accepted  
**Date**: 2026-03-03

## Context

LLM agents can enter infinite loops, burning tokens and incurring unexpected costs.

## Decision

Implement a `LoopDetector` watchdog (`src/watchdog/loop_detector.py`) that raises a `LoopDetectedError` when an agent exceeds `MAX_AGENT_STEPS` without a `PROGRESS` signal or exceeds `MAX_TOKEN_VELOCITY`. This triggers a `Human_Intervention_Required` event in Temporal.

## Consequences

- **Positive**: Hard cap on runaway agent costs.  
- **Positive**: Humans remain in the loop for stuck workflows.  
- **Negative**: Legitimate long-running tasks must emit progress signals; documented in the agent SDK guide.

---

# ADR-004: FastAPI as the Control Plane API Framework

**Status**: Accepted  
**Date**: 2026-03-03  
**Deciders**: Aegis-OS Core Team

## Context

Aegis-OS requires an HTTP API framework that supports async-native execution (for non-blocking OPA and LLM adapter calls), automatic OpenAPI documentation, and tight Pydantic integration for strict input validation at every trust boundary.

## Decision

Use FastAPI 0.115+ as the Control Plane API framework. All request/response models are defined as Pydantic `BaseModel` subclasses. All route handlers and adapter calls are `async def`.

## Consequences

- **Positive**: Async-native; OPA evaluations and LLM calls do not block the event loop.
- **Positive**: Pydantic `BaseModel` enforces schema validation at every API boundary — unknown `agent_type` values are rejected before they reach governance logic.
- **Positive**: Auto-generated OpenAPI schema at `/docs` serves as living API documentation.
- **Negative**: FastAPI has no built-in rate limiting; requires `slowapi` or an upstream gateway (addressed in Phase 1).

---

# ADR-005: structlog + OpenTelemetry for Observability

**Status**: Accepted  
**Date**: 2026-03-03  
**Deciders**: Aegis-OS Core Team

## Context

Audit logging for an AI governance platform must be structured (machine-parseable), correlated (events for the same task must be linked), and extensible (new frameworks like SOC2 or GDPR should not require changes to the logging layer).

## Decision

Use `structlog` for structured JSON log emission and the OpenTelemetry SDK for distributed trace correlation. The `AuditLogger` class in `src/audit_vault/logger.py` wraps both. Security-relevant events use `AuditLogger.audit()`, which opens an OTel span and attaches `agent_id` and `action` as span attributes. All other events use `info()`, `warning()`, or `error()`.

## Alternatives Considered

- **Python stdlib `logging` + JSON formatter**: Lacks built-in context binding and processor chain; more boilerplate for structured output.
- **Loguru**: No native OTel integration; community-maintained rather than CNCF-backed.
- **Direct OTel Logs API**: Insufficient maturity for structured log output at the time of decision.

## Consequences

- **Positive**: Every log entry is valid JSON, readable by any SIEM without a custom parser.
- **Positive**: OTel spans correlate audit events to distributed traces; compatible with Jaeger, Zipkin, and OTLP collectors.
- **Positive**: `structlog` processor chain is composable; adding PII scrubbing to all log output is a single processor addition.
- **Negative**: `ConsoleSpanExporter` is used in v0.1; must be replaced with an OTLP exporter before production (see `SECURITY.md`).

---

# ADR-006: Vendor-Agnostic BaseAdapter ABC for LLM Providers

**Status**: Accepted  
**Date**: 2026-03-03  
**Deciders**: Aegis-OS Core Team

## Context

Aegis-OS targets a multi-vendor LLM environment (OpenAI, Anthropic, Local Llama). Provider APIs differ in authentication, request/response schema, token counting, and error handling. The Aegis Governance Loop must apply identically regardless of which provider executes the prompt.

## Decision

Define a `BaseAdapter` abstract base class in `src/adapters/base.py` with a standardized `LLMRequest` / `LLMResponse` model pair. Every provider adapter implements `async def complete(request: LLMRequest) -> LLMResponse`. The Control Plane only operates on these standardized models; provider-specific details are encapsulated inside each adapter.

## Consequences

- **Positive**: The Guardrails, BudgetEnforcer, and AuditLogger are provider-agnostic; adding a new LLM provider requires only a new adapter class.
- **Positive**: Token count normalization (`LLMResponse.tokens_used`) ensures `BudgetEnforcer` works consistently across providers with different tokenization schemes.
- **Positive**: Enables the multi-vendor arbitrage routing planned in Phase 2/3 without changes to the governance layer.
- **Negative**: Lowest-common-denominator `LLMRequest` model may not expose provider-specific features (e.g., OpenAI function calling schema); adapters may need to extend the base model.

---

# ADR-007: pydantic-settings with AEGIS_ Prefix for Configuration

**Status**: Accepted  
**Date**: 2026-03-03  
**Deciders**: Aegis-OS Core Team

## Context

Aegis-OS must run in multiple environments (development, staging, production) without code changes. All tunable parameters — budget limits, token thresholds, service endpoints, signing keys — must be injectable from the environment in accordance with 12-factor App principles.

## Decision

Use `pydantic-settings` `BaseSettings` with `env_prefix="AEGIS_"` and `case_sensitive=False`. All configuration is centralized in `src/config.py` as a single `Settings` singleton. No module reads environment variables directly; all configuration is accessed via `from src.config import settings`.

## Consequences

- **Positive**: A single `settings` object is the contract for all tunable parameters; easy to mock in tests.
- **Positive**: `pydantic-settings` validates types and applies defaults at startup; misconfiguration fails fast with a clear error.
- **Positive**: The `AEGIS_` prefix avoids collisions with OS and framework environment variables.
- **Negative**: Secrets (signing key, Vault token) are currently loaded from environment variables; Phase 4 will replace these with dynamic Vault fetches so that secrets are never present in the process environment.

---

# ADR-008: In-Memory State for v0.1 with Explicit Temporal Migration Path

**Status**: Accepted (with expiry)  
**Date**: 2026-03-03  
**Deciders**: Aegis-OS Core Team  
**Expiry**: This decision is superseded by the Phase 2 Temporal integration.

## Context

`BudgetEnforcer` and `LoopDetector` maintain per-session state. In a production system this state must be durable (survive restarts), consistent across multiple API replicas, and recoverable after an LLM provider outage. Implementing durable state correctly from day one would require Temporal integration before any other module is testable.

## Decision

For v0.1, use in-memory Python dicts (`self._sessions`, `self._contexts`) with no persistence. Document this explicitly as a known limitation in `SECURITY.md` and `docs/threat-model.md`. Design the public API of both watchdog classes so that replacing the backing store (in Phase 2) requires no changes to callers.

## Consequences

- **Positive**: All watchdog logic is testable from day one without a running Temporal cluster.
- **Positive**: `AgentScheduler` and both watchdog classes already use the abstraction that Phase 2 will fill with real Temporal workflow state.
- **Negative**: A process restart loses all active session budgets and loop detection contexts. This is a **known, accepted risk** for development environments only.
- **Negative**: Running multiple `aegis-api` replicas in v0.1 results in split-brain budget tracking — each replica has its own in-memory state. Horizontal scaling is unsafe until Phase 2.

---

# ADR-009: PendingApproval State Machine for Human-in-the-Loop Workflows

**Status**: Accepted  
**Date**: 2026-03-05  
**Deciders**: Aegis-OS Core Team (Security & Governance, Platform)  
**Phase**: Phase 2 preparation (S-prep-1)

**Gate 2 review note**: Reviewed against the implemented Phase 2 workflow and
signed off by the Platform team lead on 2026-03-09.

## Context

When a `LoopDetector` raises `PendingApprovalError`, the orchestrator must pause workflow execution and wait for an authorised human operator to either approve or deny continued execution. This introduces a new workflow state (`PendingApproval`) that must be modelled explicitly, with defined transitions, timeouts, and RBAC gating.

## State Machine

```
                     ┌─────────┐
          task start │         │
        ─────────────▶ Running │
                     │         │
                     └────┬────┘
                          │  HUMAN_REQUIRED signal
                          ▼
                  ┌──────────────────┐
                  │  PendingApproval  │
                  │  (max 24 h)       │
                  └───┬──────────┬───┘
                      │          │
          approve      │          │   deny
           action      │          │   action
                      ▼          ▼
               ┌──────────┐  ┌────────┐
               │ Approved │  │ Denied │
               └────┬─────┘  └───┬────┘
                    │             │
                    ▼             ▼
              ┌───────────┐  ┌────────┐
              │ Completed │  │ Failed │  ◀── also triggered by 24 h timeout
              └───────────┘  └────────┘
```

**State descriptions:**

| State | Description |
|---|---|
| `Running` | Workflow is executing normally through the five pipeline stages. |
| `PendingApproval` | Execution is paused; awaiting an authorised `admin` approve or deny action. |
| `Approved` | An authorised operator approved resumed execution. |
| `Denied` | An authorised operator denied resumed execution; workflow terminates. |
| `Completed` | Workflow ran to successful completion. |
| `Failed` | Workflow terminated due to denial, timeout, or unrecoverable error. |

**Transitions:**

- `Running → PendingApproval`: `PendingApprovalError` raised by `LoopDetector.record_step()`.
- `PendingApproval → Approved`: `approve` action submitted by an `admin` RBAC principal.
- `PendingApproval → Denied`: `deny` action submitted by an `admin` RBAC principal.
- `PendingApproval → Failed` (timeout): 24 h elapsed without an approve or deny action. Prometheus `aegis_hitl_stuck` alert fires at this threshold.
- `Approved → Completed`: Resumed execution completes all remaining pipeline stages.
- `Denied / Timeout → Failed`: Task is marked failed; a `task.denied` or `task.failed` audit event is emitted.

## Decision

Model `PendingApproval` as an explicit Temporal workflow state gated by a signal channel. The `ApprovalSignal` (approve/deny) is sent externally via the `/api/v1/tasks/{task_id}/approve` and `/api/v1/tasks/{task_id}/deny` endpoints (Phase 2).  
RBAC enforcement uses the `rbac_capabilities` map in `policies/agent_access.rego`; the live matrix currently allows only `admin` principals to send the signal.

## Consequences

- **Positive**: Temporal's durable timer handles the 24 h timeout without a background thread.
- **Positive**: OPA remains the single source of truth for the approve/deny capability check.
- **Negative**: Workflows in `PendingApproval` consume a Temporal workflow slot; large volumes of stuck approvals increase cluster resource pressure.

---

# ADR-010: Write-Once Audit Backend — Signed PostgreSQL vs. AWS QLDB

---

# ADR-011: Sender-Constrained Session Tokens with ES256 + DPoP

**Status**: Proposed  
**Date**: 2026-03-06  
**Deciders**: Aegis-OS Core Team (Platform, Security & Governance)

## Context

`SessionManager` currently issues short-lived scoped bearer JWTs. The 15-minute
TTL, `agent_type` scope, `allowed_actions`, and revocable `jti` claims reduce
blast radius, but the token is still a bearer credential: if the token string
is exfiltrated, it can be replayed until expiry unless the `jti` is revoked.

Moving from `HS256` to an asymmetric algorithm improves key distribution but
does **not** by itself prevent replay. To materially reduce token replay risk,
the token must be sender-constrained so that the caller proves possession of a
private key on every request.

## Decision

Refactor `src/governance/session_mgr.py` from a shared-secret bearer-only
issuer into a dual-mode identity component that supports both:

- legacy short-lived bearer JWTs for backward compatibility; and
- sender-constrained access tokens signed with `ES256` and bound to a DPoP
     public key via `cnf.jkt`.

The refactored `SessionManager` now exposes the following DPoP-oriented
capabilities:

- `issue_sender_constrained_token(...)`
- `issue_dpop_proof(...)`
- `validate_dpop_proof(...)`
- `validate_sender_constrained_token(...)`
- `public_jwk_thumbprint(...)`
- `generate_dpop_key_pair()`

## Design

### Access Token Format

Sender-constrained access tokens continue to carry the existing claims used by
the orchestrator, but add token binding material:

```json
{
     "jti": "token-uuid",
     "sub": "requester-or-agent-id",
     "agent_type": "finance",
     "session_id": "session-uuid",
     "task_id": "task-uuid",
     "allowed_actions": ["llm.invoke"],
     "role": "ops_lead",
     "iat": 1741257600,
     "exp": 1741258500,
     "metadata": {"provider": "openai"},
     "cnf": {
          "jkt": "base64url(sha256(jwk-thumbprint-input))"
     }
}
```

`cnf.jkt` is the RFC 7638 thumbprint of the client public JWK that is allowed
to present the token.

### DPoP Proof Format

Each protected request carries a proof JWT in the `DPoP` header. The proof is
signed by the client private key and embeds the corresponding public JWK in the
protected header:

```json
{
     "header": {
          "typ": "dpop+jwt",
          "alg": "ES256",
          "jwk": {
               "kty": "EC",
               "crv": "P-256",
               "x": "...",
               "y": "..."
          }
     },
     "payload": {
          "jti": "proof-uuid",
          "htm": "POST",
          "htu": "https://api.example.test/v1/llm",
          "iat": 1741257600,
          "ath": "base64url(sha256(access-token))"
     }
}
```

### Verification Flow

1. Validate the access token signature with `ES256` verification key material.
2. Extract `cnf.jkt` from the token claims.
3. Extract the embedded public JWK from the DPoP proof header.
4. Compute the JWK thumbprint and compare it to `cnf.jkt`.
5. Verify the DPoP proof signature using the embedded public key.
6. Validate `htm`, `htu`, `iat`, optional `nonce`, and `ath`.
7. Reject a reused proof `jti` via a replay store.
8. Only then permit the protected action.

### Why This Differentiates Aegis

Aegis is not merely changing JWT algorithms. The differentiator is that JIT
agent identity becomes:

- short-lived,
- task-bound,
- policy-scoped,
- sender-constrained, and
- auditable across retries and workflow resumes.

That is a materially stronger security posture than bearer-only tokens,
especially for agent-to-tool and control-plane-to-provider requests.

## Migration Plan

### Phase 1 — Introduce asymmetric signing without changing caller behaviour

- Add `AEGIS_TOKEN_PRIVATE_KEY` and `AEGIS_TOKEN_PUBLIC_KEY`.
- Switch signing from `HS256` to `ES256` in non-production environments first.
- Keep `issue_token()` and `validate_token()` API stable so current callers do
     not break.
- Continue allowing bearer validation while asymmetric key distribution is
     rolled out.

### Phase 2 — Add sender-constrained token issuance

- Teach agents, SDKs, or workload sidecars to generate an EC P-256 key pair.
- Issue tokens via `issue_sender_constrained_token(...)` with `cnf.jkt`.
- Include `task_id`, `session_id`, and `allowed_actions` in every issued token.
- Emit audit events for token issuance that include `jti`, `task_id`, and the
     `cnf.jkt` thumbprint.

### Phase 3 — Require DPoP proofs on protected calls

- Attach a `DPoP` proof to every call that uses an Aegis-issued session token.
- Verify proof signature, `htu`, `htm`, `iat`, `ath`, and replay state.
- Reject unbound proofs and mismatched public keys.
- Add audit events for `dpop.proof.validated`, `dpop.proof.replayed`, and
     `dpop.proof.rejected`.

### Phase 4 — Replace in-memory replay protection with a durable store

- Move proof-`jti` replay tracking from process memory to Redis or another
     low-latency shared store.
- Preserve proof replay resistance across API replica failover and process
     restarts.
- Store replay entries with TTL equal to the DPoP acceptance window.

### Phase 5 — Disable bearer-only mode

- Reject bearer tokens that do not carry `cnf.jkt` for sensitive actions.
- Restrict bearer-only validation to explicit backward-compatibility paths, if
     any remain.
- Update SDKs, docs, and deployment runbooks so sender-constrained access is
     the default operating mode.

## Consequences

- **Positive**: Token theft becomes significantly less useful because the
     attacker also needs the bound private key.
- **Positive**: Asymmetric verification removes the need to distribute the
     signing secret to every verifier.
- **Positive**: `task_id` + `cnf.jkt` provides stronger audit correlation for
     workflow retries, handoffs, and revocations.
- **Negative**: DPoP adds key lifecycle management and replay-state storage.
- **Negative**: In-memory replay protection is suitable for development but not
     for a multi-replica production deployment.

**Status**: Accepted (recommendation)  
**Date**: 2026-03-05  
**Deciders**: Aegis-OS Core Team (Audit & Compliance)  
**Phase**: Phase 2 preparation (A-prep-3); selection finalised at Gate 3

## Context

Aegis-OS requires a tamper-evident, append-only audit trail for all governance events. Phase 1 uses stdout-only logging (accepted risk). Phase 3 must replace this with a write-once backend that can demonstrate immutability for SOC 2 Type II and GDPR audit requirements.

Two candidate options were evaluated:

| Criterion | AWS QLDB | Signed PostgreSQL |
|---|---|---|
| **Immutability** | Native ledger; cryptographic proof built-in | Append-only table + HMAC chain; proof requires custom tooling |
| **Portability** | AWS-only (vendor lock-in) | Runs on any Postgres-compatible host (on-prem, all clouds) |
| **Open-core alignment** | Incompatible — forces closed AWS dependency | Compatible — open-core users can run the same backend |
| **Ops complexity** | Fully managed, no ops overhead | Requires WAL archiving and chain-verification tooling |
| **Cost** | Per-I/O pricing; can be expensive at high event volume | Compute / storage cost; scales linearly with volume |
| **Export / interop** | QLDB journal export to S3; limited external tooling | Standard SQL; any BI or SIEM tool can query directly |

## Decision

**Adopt Signed PostgreSQL as the primary write-once backend.** Append-only semantics are enforced with a PostgreSQL trigger that prevents `UPDATE` and `DELETE` on the `audit_events` table. Each row carries an `HMAC-SHA256` chain hash computed over `(previous_hash || event_json)` so that any tampering causes hash verification to fail.

An optional `QLDB` adapter will be provided as a Phase 4 commercial-layer feature for AWS-hosted enterprise deployments. This maintains open-core separation: the core runtime always uses Signed PostgreSQL; the QLDB adapter is an enterprise add-on.

## Consequences

- **Positive**: Core runtime has zero cloud-provider dependencies — consistent with the open-core Apache 2.0 commitment.
- **Positive**: SIEM integration is simpler via standard SQL rather than QLDB journal exports.
- **Negative**: Hash chain verification requires a separate CLI tool (`aegis-audit-verify`); planned for Phase 3.
- **Negative**: PostgreSQL append-only enforcement via triggers is convention, not hardware-enforced immutability — a privileged DBA can disable the trigger. Mitigated by database-level audit logging of DDL changes.
