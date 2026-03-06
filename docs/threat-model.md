# Aegis-OS Threat Model

**Version:** 0.1.0  
**Date:** 2026-03-03  
**Methodology:** STRIDE (Spoofing, Tampering, Repudiation, Information Disclosure, Denial of Service, Elevation of Privilege)  
**Scope:** Aegis-OS Control Plane and all components in the Docker Compose deployment boundary

---

## Table of Contents

- [System Overview & Trust Boundaries](#system-overview--trust-boundaries)
- [Data Flow Diagram](#data-flow-diagram)
- [STRIDE Analysis by Component](#stride-analysis-by-component)
- [Attack Surface Summary](#attack-surface-summary)
- [Mitigations Matrix](#mitigations-matrix)
- [Residual Risks](#residual-risks)
- [Out of Scope](#out-of-scope)

---

## System Overview & Trust Boundaries

Aegis-OS sits between external callers (humans or upstream orchestrators) and LLM providers. Three explicit trust zones are defined:

```
┌──────────────────────────────────────────────────────────────────┐
│  ZONE 0 — Untrusted External                                     │
│  Callers: API clients, external agents, MCP peer servers         │
└───────────────────────────┬──────────────────────────────────────┘
                            │ HTTPS (TLS 1.3)
┌───────────────────────────▼──────────────────────────────────────┐
│  ZONE 1 — Aegis Control Plane (Trusted Perimeter)                │
│  Components: FastAPI, Guardrails, SessionManager, PolicyEngine,  │
│              BudgetEnforcer, LoopDetector, AuditLogger           │
└──────────┬────────────────┬────────────────────┬─────────────────┘
           │                │                    │
      OPA sidecar      Vault sidecar      Temporal sidecar
      (Zone 2)          (Zone 2)           (Zone 2)
┌──────────▼────────────────▼────────────────────▼─────────────────┐
│  ZONE 2 — Internal Infrastructure (Semi-Trusted)                 │
│  OPA, HashiCorp Vault, Temporal, PostgreSQL                      │
└──────────────────────────────────────────────────────────────────┘
           │
┌──────────▼──────────────────────────────────────┐
│  ZONE 3 — External LLM Providers (Untrusted)    │
│  OpenAI, Anthropic, Local Llama inference       │
└─────────────────────────────────────────────────┘
```

**Key trust assumptions:**
- Zone 0 is fully untrusted. Every input is sanitized before use.
- Zone 1 components trust each other only via in-process calls or loopback; no shared secrets in environment variables in production.
- Zone 2 accepts only connections originating from Zone 1.
- Zone 3 (LLM providers) is treated as an untrusted data source. Responses are subject to the same Guardrails as inbound prompts.

---

## Data Flow Diagram

```
Caller
  │
  │  POST /api/v1/tasks {description, agent_type, requester_id}
  ▼
[1] FastAPI Router (router.py)
  │
  │  raw prompt
  ▼
[2] Guardrails.check_prompt_injection()          ← blocks injection attacks
  │
  │  sanitized prompt
  ▼
[3] Guardrails.mask_pii()                        ← redacts PII before policy eval
  │
  │  PolicyInput {agent_type, requester_id, action, resource, metadata}
  ▼
[4] PolicyEngine.evaluate() → OPA Server         ← allow/deny decision
  │
  │  allow=True
  ▼
[5] SessionManager.issue_token()                 ← JIT JWT, 15-min expiry
  │
  │  signed JWT returned to caller
  ▼
[6] LLM Adapter (BaseAdapter.complete())         ← prompt sent to provider
  │
  │  LLM response
  ▼
[7] Guardrails.mask_pii() (post-processing)      ← sanitize model output
  │
  │  clean response + AuditLogger.audit() event
  ▼
Caller  +  AuditVault
```

---

## STRIDE Analysis by Component

### 1. FastAPI Control Plane (`src/main.py`, `src/control_plane/router.py`)

| Threat | Category | Description | Mitigation |
|---|---|---|---|
| Unauthenticated task submission | **S**poofing | Any caller can POST `/api/v1/tasks` without proving identity | Phase 4: mTLS + OAuth 2.0 client credentials at the API gateway layer |
| Task description manipulation in transit | **T**ampering | MITM modification of the `description` field before it reaches the Guardrails | Enforce TLS 1.3; validate `Content-Type` and payload schema with Pydantic |
| No non-repudiation on task origin | **R**epudiation | A caller can deny having submitted a task | `task_id` (UUID) is logged at receipt via `AuditLogger`; correlate with caller-supplied `requester_id` |
| Response leaks internal error detail | **I**nformation Disclosure | Unhandled exceptions may expose stack traces | FastAPI exception handlers must return generic 5xx bodies; structured errors only |
| Request flood | **D**enial of Service | High-volume unauthenticated POST requests exhaust worker threads | Deploy behind a rate-limiting reverse proxy (Nginx/Traefik); add `slowapi` rate limiter to FastAPI |
| `agent_type` enum bypass | **E**levation of Privilege | Sending an unlisted agent type string to escalate permissions | Pydantic `AgentType` StrEnum validates and rejects unknown values at the model layer |

---

### 2. Guardrails (`src/governance/guardrails.py`)

| Threat | Category | Description | Mitigation |
|---|---|---|---|
| Novel prompt injection bypassing regex | **T**ampering | Paraphrased or encoded injection (base64, Unicode homoglyphs) evades `_INJECTION_PATTERNS` | Layer embedding-based semantic detector (see research.md §2); maintain and version injection pattern library |
| PII exfiltration via model response | **I**nformation Disclosure | LLM infers and outputs PII not present in the prompt (e.g., reconstructed SSN from context) | Post-response `mask_pii()` pass already implemented; extend with entropy-based anomaly detection for generated PII |
| Regex ReDoS attack | **D**enial of Service | A crafted input string causes catastrophic backtracking in one of the `_PII_PATTERNS` regexes | Audit all patterns with `re2` compatibility; add per-call timeout on `mask_pii()` |
| Bypass via language switching | **E**levation of Privilege | Injection attempts written in non-English to evade English-only patterns | Add language-detection pre-check; apply injection patterns in at minimum: EN, ES, FR, ZH, AR |

---

### 3. Policy Engine / OPA (`src/governance/policy_engine/opa_client.py`, `policies/`)

| Threat | Category | Description | Mitigation |
|---|---|---|---|
| OPA server impersonation | **S**poofing | An attacker replaces the OPA sidecar with a permissive stub that returns `allow=true` for all requests | Pin OPA container image by SHA digest; use mTLS between Control Plane and OPA in production |
| Rego policy tampering | **T**ampering | An attacker modifies `policies/*.rego` files to widen permissions | Store policies in a signed, immutable Git ref; OPA bundle signing (OPA v0.68 supports bundle signing) |
| Policy decision not logged | **R**epudiation | An allow/deny decision occurs with no audit trail | All `PolicyEngine.evaluate()` calls are wrapped by the router; log `PolicyResult` fields via `AuditLogger` before acting on the decision |
| OPA unavailability | **D**enial of Service | OPA server crashes; all tasks fail | Implement explicit fail-closed behavior: if `httpx` raises a connection error to OPA, the Control Plane returns HTTP 503 — never defaults to allow |
| Input injection into Rego `input` | **E**levation of Privilege | Attacker crafts `metadata` dict keys that collide with `input` top-level reserved fields in Rego | `PolicyInput` Pydantic model constrains `metadata` to `dict[str, str]`; Rego policies access only named fields |

---

### 4. Session Manager (`src/governance/session_mgr.py`)

| Threat | Category | Description | Mitigation |
|---|---|---|---|
| Weak signing key in development | **S**poofing | Default `token_secret_key = "change-me-in-production"` is publicly known | Production deployment MUST rotate this; enforced via `SECURITY.md` checklist and startup assertion |
| JWT algorithm confusion (`alg: none`) | **S**poofing | Attacker strips algorithm header to bypass signature verification | `python-jose` `jwt.decode()` specifies `algorithms=[settings.token_algorithm]` explicitly, rejecting `none` |
| Token replay after expiry | **E**levation of Privilege | Stolen token used within its 15-minute window | Scope restriction means a replayed `finance` token cannot access `hr` resources; add token blacklist (Vault KV) for critical revocations |
| `jti` claim not checked for uniqueness | **R**epudiation | Same `jti` used twice; log correlation fails | Implement `jti` seen-set in Vault KV at validation time to detect reuse |
| Secret key in environment variable | **I**nformation Disclosure | `AEGIS_TOKEN_SECRET_KEY` exposed in `docker inspect` or process environment | Phase 4: fetch key dynamically from Vault Transit Secrets Engine; never store in env |

---

### 5. Watchdog (`src/watchdog/budget_enforcer.py`, `src/watchdog/loop_detector.py`)

| Threat | Category | Description | Mitigation |
|---|---|---|---|
| Budget session state in memory | **T**ampering | An attacker with process access can modify `_sessions` dict directly to reset spend | Phase 2: migrate session state to Temporal workflow state (durable, cryptographically sealed) |
| `BudgetSession` not tied to a verified token | **E**levation of Privilege | An agent could create a new budget session under a fabricated `session_id` | `session_id` must be derived from the verified `jti` claim of the JIT token; enforce in Phase 2 Temporal integration |
| Loop detector bypass via crafted signals | **T**ampering | Agent always emits `LoopSignal.PROGRESS` regardless of actual progress | `PROGRESS` signals must be validated against measurable output change (e.g., a hash of the agent's tool call result), not self-reported |
| Prometheus metrics manipulation | **T**ampering | Counter/gauge values can be spoofed if the `/metrics` endpoint is writable | `/metrics` endpoint is Prometheus read-only; enforce network policy to restrict scrape access to Prometheus server only |
| Cost-per-token model stale | **I**nformation Disclosure | Hard-coded `DEFAULT_COST_PER_TOKEN` underestimates real cost, allowing actual overruns | Replace with dynamic pricing from provider APIs; alert if actual cost diverges from estimate by >5% |

---

### 6. Audit Logger (`src/audit_vault/logger.py`)

| Threat | Category | Description | Mitigation |
|---|---|---|---|
| Log injection via structured fields | **T**ampering | User-supplied strings containing `\n` or JSON-breaking characters pollute structured log output | `structlog.processors.UnicodeDecoder()` is already in the chain; add explicit field value sanitization |
| Log stream interception | **I**nformation Disclosure | Stdout log stream captured by a co-located malicious container | In production, ship logs over an encrypted OTLP exporter to a dedicated collector; do not rely on stdout in multi-tenant environments |
| No log integrity guarantee | **R**epudiation | Logs written to stdout can be deleted or truncated by an operator | Phase 3: integrate write-once log store (Immudb or QLDB); hash-chain entries for tamper evidence |
| Duplicate events on Temporal replay | **T**ampering | Workflow replay re-emits audit events, polluting the immutable audit trail | Wrap all audit calls in Temporal Activities (not Workflow functions); use `jti`-scoped idempotency keys |

---

### 7. LLM Adapters (`src/adapters/`)

| Threat | Category | Description | Mitigation |
|---|---|---|---|
| API key exfiltration | **I**nformation Disclosure | LLM provider API keys stored as env vars are exposed if the container is compromised | Phase 4: fetch API keys dynamically from Vault; rotate on each Temporal workflow execution |
| Malicious content in LLM response | **T**ampering | Provider returns content designed to manipulate downstream processing (indirect injection) | All LLM responses pass through `mask_pii()` and injection check post-processing |
| Provider SSRF via prompt | **I**nformation Disclosure | Crafted prompt tricks the LLM into issuing HTTP requests to internal endpoints (e.g., `169.254.169.254`) | Egress firewall rules on the container network; `BaseAdapter` should not follow hyperlinks in responses |
| Billing fraud via token stuffing | **D**enial of Service | Abuse of the adapter to submit artificially large prompts, running up provider bills | `LLMRequest.max_tokens` is capped; `BudgetEnforcer` hard-limits spend per session |

---

## Attack Surface Summary

| Surface | Exposure | Highest Risk |
|---|---|---|
| `POST /api/v1/tasks` | Public HTTP | Prompt injection, unauthenticated access |
| `GET /metrics` | Internal | Sensitive throughput data; restrict to Prometheus only |
| OPA server port `8181` | Internal | Policy tampering if exposed externally |
| Vault dev server `8200` | Internal | Root token exposure in default config |
| Temporal gRPC `7233` | Internal | Workflow manipulation if exposed externally |
| LLM provider egress | External | API key theft, SSRF |
| `policies/*.rego` files | Filesystem | Policy tampering without signing |

---

## Mitigations Matrix

| Risk | Severity | Implemented (v0.1) | Planned Phase |
|---|---|---|---|
| Prompt injection via regex filter | High | ✅ | Enhance: Phase 1 |
| PII masking pre/post LLM | High | ✅ | Done |
| OPA fail-closed on unavailability | Critical | ⚠️ Partial (raises error) | Explicit 503: Phase 1 |
| JWT `alg: none` bypass | Critical | ✅ | Done |
| Hard-coded signing key | Critical | ⚠️ Dev default only | Vault Transit: Phase 4 |
| Token scope enforcement | High | ✅ | Done |
| Budget hard cap | High | ✅ | Done |
| Loop circuit breaker | High | ✅ | Done |
| Audit log append-only | High | ⚠️ In-memory / stdout | Write-once DB: Phase 3 |
| mTLS between services | High | ❌ | Phase 4 |
| OPA bundle signing | High | ❌ | Phase 4 |
| Vault for API key secrets | Critical | ❌ | Phase 4 |
| Rate limiting on public API | Medium | ❌ | Phase 1 |

---

## Residual Risks

The following risks are **accepted** for v0.1 development environments and **must be resolved** before any production or customer-facing deployment:

1. **No API authentication on `/api/v1/tasks`** — Any network-reachable process can submit tasks. Accepted until Phase 1 gateway integration.
2. **In-memory budget and loop state** — A process restart loses all session tracking. Accepted until Phase 2 Temporal integration.
3. **Vault in dev mode** — The Vault container uses `-dev` mode with a static root token. The Vault unseal mechanism, ACL policies, and audit logging are not configured. Do not use in production.
4. **Stdout-only audit log** — Logs are not persisted or integrity-protected. Accepted until Phase 3 Audit Vault integration.
5. **Single-node OPA without bundle signing** — Policy files are loaded directly from disk without cryptographic verification of their integrity.

---

## Phase 2 Risks

The following new attack surfaces are introduced by the Phase 2 Temporal integration and must be addressed before Gate 2.

### jti Reuse Across Temporal Retries

**Threat**: `JITTokenIssue` is a Temporal activity. When Temporal retries a failed activity, it may re-invoke `SessionManager.issue_token()` with the same input parameters. Depending on how the JIT token is constructed, this could yield a token with a reused `jti` claim.

**Impact**: Elevation of Privilege. The session integrity guarantee (one token per request, short-lived, unique `jti`) is violated. A replay attack becomes possible if an adversary captures the first token before the retry.

**Root cause**: Temporal activity retries are transparent to the application code; the `jit_token_issue` activity stub does not receive any retry-context signal to force a fresh `jti`.

**Planned mitigation for Phase 2 (P2-1)**:
- `jit_token_issue` must derive its `jti` from a combination of the stable `task_id` **and** the Temporal activity attempt number (injected via `activity.info().attempt`). This guarantees a unique `jti` on every retry without external coordination.
- `test_jit_token.py` must include a regression test asserting that two successive calls to `issue_token` with identical input produce distinct `jti` values.
- Any token with a `jti` already present in the audit log for the same `task_id` must be rejected at the validation step.

**Interim mitigation (Phase 2 prep)**: `jit_token_issue` is a `NotImplementedError` stub — no tokens are issued. The risk is theoretical until the stub is implemented.

---

## Out of Scope

- **Model-level jailbreaks** — Aegis-OS is not responsible for the internal alignment of the underlying LLM. The Guardrails layer is an application-level defense, not a replacement for model safety training.
- **Physical security** — The threat model assumes the infrastructure is hosted in a cloud environment with standard physical security controls.
- **Insider threats from Aegis operators** — A privileged operator with shell access to the Control Plane container can bypass all application-level controls. This is a deployment-level concern addressed by least-privilege OS accounts and audit logging of shell sessions.
