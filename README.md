# Aegis-OS: The Open Governance Runtime for Enterprise AI Agents

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

> **v0.2.0** — Now with native **Code Scalpel MCP** integration

**Secure, auditable, governed code agents out of the box.** Your Claude / Cursor agents get surgical edits + the full Aegis Governance Loop — PII scrub, JIT tokens, budget breakers, and an immutable audit trail — with zero configuration.

Aegis-OS is an **open governance runtime** that manages, secures, and scales autonomous AI agent workforces. As enterprises shift from experiment-phase chatbots to production-scale agentic workflows, the primary bottlenecks are no longer model performance — they are **security**, **observability**, and **cost control**.

Aegis-OS treats AI agents as managed processes with strict, auditable boundaries. Every interaction passes through the **Aegis Governance Loop** — a closed-loop pipeline of PII scrubbing, policy enforcement, JIT identity, real-time economic controls, and immutable audit capture — before and after any prompt reaches a model.

---

## Table of Contents

- [Who is this for?](#who-is-this-for)
- [Core Value Pillars](#core-value-pillars)
- [Why Aegis-OS: The Case for a Unified Stack](#why-aegis-os-the-case-for-a-unified-stack)
- [Technical Architecture](#technical-architecture)
- [Module Reference](#module-reference)
- [Infrastructure Stack](#infrastructure-stack)
- [API Reference](#api-reference)
- [Configuration](#configuration)
- [Quick Start](#quick-start)
- [Development](#development)
- [Roadmap](#roadmap)

---

## Who is this for?

Aegis-OS serves two distinct audiences. Jump to the section most relevant to you:

| I am a… | My concern is… | Start here |
|---|---|---|
| **Platform / infra engineer** building the agent runtime layer | Adapter interfaces, policy schemas, OPA integration, provider-agnostic routing | [Core Value Pillars](#core-value-pillars) → [Technical Architecture](#technical-architecture) |
| **CISO / compliance officer** evaluating an agent governance control layer | PII controls, audit trails, SOC2/GDPR reporting, budget enforcement, JIT identity | [Why Aegis-OS](#why-aegis-os-the-case-for-a-unified-stack) → [API Reference](#api-reference) |

---

## Core Value Pillars

### 1. Autonomous Security — Aegis-IAM

Traditional long-lived API keys are a critical liability. Aegis-OS issues **Just-In-Time (JIT) Scoped Session Tokens** for every agent task. Each token:

- Expires after **15 minutes** (configurable via `AEGIS_TOKEN_EXPIRY_SECONDS`)
- Is scoped to a specific **agent type** (`finance`, `hr`, `it`, `legal`, `general`)
- Is signed with HS256 and carries a unique `jti` claim for revocation tracing
- Drastically reduces the blast radius if a token is ever compromised

Tokens are issued by `SessionManager` and validated on every inbound request.

### 2. The Aegis Governance Loop

Every agent interaction passes through a closed-loop governance pipeline before and after touching an LLM. This is the core differentiator of Aegis-OS — a named, versioned interface that other runtimes and orchestration frameworks can integrate against:

```
[Raw Prompt] → PII Scrub → Injection Check → OPA Policy → LLM Adapter → Post-Sanitize → [Clean Output]
```

**Pre-Processing (`Guardrails`)**

Detects and redacts five classes of PII before any data leaves the control plane:

| Pattern | Example | Replacement |
|---|---|---|
| Email address | `user@corp.com` | `[REDACTED-EMAIL]` |
| US Social Security Number | `123-45-6789` | `[REDACTED-SSN]` |
| Credit card number | `4111 1111 1111 1111` | `[REDACTED-CREDIT_CARD]` |
| US phone number | `(800) 555-0100` | `[REDACTED-PHONE_US]` |
| IPv4 address | `192.168.1.1` | `[REDACTED-IP_ADDRESS]` |

Also performs **prompt injection detection**, blocking patterns such as `ignore all previous instructions`, `act as`, `jailbreak`, and `DAN mode` before they reach any model.

**Policy Enforcement (`PolicyEngine` + OPA)**

Real-time `allow/deny` decisions are delegated to an **Open Policy Agent** server. Policies are written in Rego and hot-reloaded without redeploying application code. The engine evaluates structured `PolicyInput` documents containing agent type, requester identity, action, and target resource — returning a `PolicyResult` with a boolean decision and human-readable reasons.

**Post-Processing**

A secondary guardrail pass is applied to LLM outputs to catch any PII the model may have inferred or generated, preventing sensitive data leakage in responses.

### 3. Agent Watchdog — Economic Safety

Eliminates runaway token costs and infinite execution loops via two complementary mechanisms:

**`BudgetEnforcer`**
- Creates a `BudgetSession` per agent run with a configurable USD cap (default **$10.00**)
- Tracks cumulative token spend using a per-token cost model
- Raises `BudgetExceededError` and emits a `budget.exceeded` audit event the moment a session goes over limit
- Exposes live Prometheus metrics: `aegis_tokens_consumed_total` (counter by agent type) and `aegis_budget_remaining_usd` (gauge by session)

**`LoopDetector`**
- Records each agent step with a `LoopSignal` (`PROGRESS` | `NO_PROGRESS` | `HUMAN_REQUIRED`)
- Triggers a circuit breaker if step count exceeds `max_agent_steps` (default **10**) without a `PROGRESS` signal
- Also triggers on **token velocity** spikes: if a single step consumes more than `max_token_velocity` tokens (default **10,000**), execution is halted and human intervention is flagged

### 4. Audit Vault — Immutable Transparency

Every agent action, policy decision, and system event is captured in a structured, append-only audit trail:

- **`AuditLogger`**: Emits JSON-structured log entries via `structlog` to stdout. Each entry carries ISO-format timestamps, log level, and arbitrary key-value context.
- **OpenTelemetry**: Security-relevant `audit()` calls open a correlated OTel span, attaching `agent_id` and `action` attributes for distributed trace correlation.
- **`ComplianceReporter`**: Generates on-demand **SOC2** and **GDPR** compliance reports from the audit event store, summarizing total events, failure counts, and PII access events for any time window.

---

## Why Aegis-OS: The Case for a Unified Stack

### The Core Problem: Agent Failures Are Systemic, Not Isolated

The failure modes of enterprise AI agents are **interdependent**. Competitors solve isolated slices — identity, observability, cost control, or governance — but none address the **systemic risk** that emerges when autonomous agents operate at scale.

A real incident chain looks like this:

1. A prompt slips through guardrails
2. A policy isn't enforced
3. A token has too much privilege
4. The agent loops and burns budget
5. No audit trail exists to reconstruct what happened

This is exactly the pattern that enterprise governance research identifies as the primary cause of governance program failure: organizations cannot operationalize controls **across the full lifecycle** of agent behavior ([subramanya.ai](https://subramanya.ai/2025/11/20/the-governance-stack-operationalizing-ai-agent-governance-at-enterprise-scale/)). A McKinsey survey found that 40% of technology executives believe their governance programs are insufficient for the scale and complexity of their agentic workforce.

Aegis-OS is designed so that **every layer reinforces the others**. Remove any one layer and the rest lose effectiveness.

---

### Competitive Landscape

| Capability | Aegis-OS | AControlLayer | Microsoft (CAF) | Portkey | Vast Data AI OS |
|---|:---:|:---:|:---:|:---:|:---:|
| Pre-LLM PII scrubbing | ✅ | ❌ | ❌ | ❌ | ❌ |
| Prompt-injection detection | ✅ | ❌ | ❌ | ❌ | ❌ |
| OPA policy enforcement (hot-reload) | ✅ | ❌ | Partial | ❌ | ❌ |
| JIT scoped token issuance | ✅ | ✅ | ✅ | ❌ | ✅ |
| Real-time budget circuit breaker | ✅ | ❌ | ❌ | ❌ | ❌ |
| Loop / token-velocity detection | ✅ | ❌ | ❌ | ❌ | ❌ |
| OTel-correlated audit spans | ✅ | ❌ | Partial | ❌ | ❌ |
| SOC2 / GDPR report generation | ✅ | ❌ | ❌ | ❌ | ❌ |
| Model-agnostic adapter layer | ✅ | ❌ | ❌ | ✅ | ❌ |
| Temporal durable orchestration | ✅ | ❌ | ❌ | ❌ | ❌ |

---

### Why Competitors Fall Short

#### 🔐 1. Identity without governance is incomplete

Platforms like AControlLayer emphasize identity, permissions, and audit trails, but they rely on the user's existing runtime for execution and do not provide a full governance pipeline ([acontrollayer.com](https://acontrollayer.com/)). They answer *who* acted — not *whether they were allowed to*, *how much it cost*, or *what data they touched*.

Microsoft's Cloud Adoption Framework stresses that without proper governance, AI agents introduce risks around sensitive data exposure, compliance violations, and security vulnerabilities ([Microsoft Learn](https://learn.microsoft.com/en-us/azure/cloud-adoption-framework/ai-agents/governance-security-across-organization)). But their control plane focuses on identity and observability — not watchdog economics, not PII scrubbing, not multi-adapter routing.

Aegis-OS creates the **Aegis Governance Loop** — a closed-loop pipeline that ties identity to policy, budget, and audit simultaneously:

```
[Raw Prompt] → PII Scrub → Injection Check → OPA Policy → LLM → Post-Sanitize → [Clean Output]
```

Identity becomes a **living, dynamic boundary**, not a static permission set.

#### 💸 2. Spend dashboards are not circuit breakers

Portkey and similar platforms provide token spend dashboards, but they react after the fact. They do not enforce **real-time circuit breakers**.

Aegis-OS halts execution the *moment* a session exceeds its USD cap. The `BudgetEnforcer` raises a `BudgetExceededError` synchronously and emits an audit event. The `LoopDetector` stops infinite loops and token-velocity spikes mid-flight. This prevents the catastrophic overnight runaway billing scenarios that enterprises fear — not by alerting after the damage, but by stopping execution before it compounds.

#### 🧠 3. Infrastructure-coupled control planes are vendor lock-in

Vast Data's AI OS introduces a global control plane and zero-trust agent framework, but it is tightly coupled to their storage and infrastructure stack ([SiliconANGLE](https://siliconangle.com/2026/02/25/vast-data-expands-ai-operating-system-global-control-plane-zero-trust-agent-framework-deeper-nvidia-integration/)). It is not model-agnostic and not portable.

Aegis-OS is:
- **Provider-agnostic** — OpenAI, Anthropic, or local Llama behind the same interface
- **Infrastructure-neutral** — runs anywhere Docker runs
- **Policy-driven** — OPA Rego policies that hot-reload without redeployment
- **Orchestration-ready** — Temporal workflows survive process restarts and provider outages

#### 📜 4. Compliance tooling built for regulators, not just developers

Agentic Control Plane (ACP) demonstrates the value of audit logs and RBAC enforcement, but does not provide OTel-correlated spans, automated SOC2/GDPR report generation, or pre- and post-LLM PII scrubbing in a single pipeline.

Aegis-OS's Audit Vault captures every policy decision, token spend, loop signal, and PII scrubbing event in a structured, append-only log with distributed trace correlation — and surfaces it as a `ComplianceReport` on demand.

---

### Strategic Positioning

Aegis-OS is the **Kubernetes of AI agent governance** — a unified control plane that beats a patchwork of bolted-on tools the same way Kubernetes beat ad-hoc container management scripts.

The **Aegis Governance Loop is the open standard**. The enterprise runtime is the product.

| Property | What it means in practice |
|---|---|
| **Holistic** | Solves the entire governance lifecycle in a single runtime |
| **Open** | Model-agnostic, adapter-based, infrastructure-neutral |
| **Deterministic** | Every decision is logged, enforced, and auditable |
| **Enterprise-grade** | Built around compliance, identity, and cost control from day one |
| **Future-proof** | Temporal orchestration and multi-agent workflows are first-class concerns |

Competitors are building *features*. Aegis-OS is building an **open standard** — and the enterprise runtime on top of it.

---

## Technical Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                     Aegis-OS Control Plane (FastAPI)                 │
│                                                                      │
│  POST /api/v1/tasks                                                  │
│       │                                                              │
│       ▼                                                              │
│  ┌─────────────┐   ┌───────────────┐   ┌──────────────────────┐    │
│  │  Guardrails  │──▶│  PolicyEngine │──▶│   SessionManager     │    │
│  │ (PII + Inj.) │   │  (OPA/Rego)  │   │  (JIT JWT Tokens)    │    │
│  └─────────────┘   └───────────────┘   └──────────────────────┘    │
│                                                   │                  │
│       ┌───────────────────────────────────────────┘                  │
│       ▼                                                              │
│  ┌─────────────────────────────────────┐                            │
│  │          LLM Adapter Layer          │                            │
│  │  OpenAI │ Anthropic │ Local Llama   │                            │
│  └─────────────────────────────────────┘                            │
│       │                                                              │
│       ▼                                                              │
│  ┌──────────────┐   ┌────────────────┐                              │
│  │BudgetEnforcer│   │  LoopDetector  │  ◀── Watchdog Layer          │
│  └──────────────┘   └────────────────┘                              │
│                                                                      │
│  ─────────────────────────────────────────────────────────────────  │
│  AuditLogger (structlog + OTel)   │   /metrics (Prometheus)         │
└──────────────────────────────────────────────────────────────────────┘
```

**Core stack:**

| Concern | Technology |
|---|---|
| API Framework | FastAPI 0.115+ |
| Orchestration | Temporal.io 1.24 |
| Policy-as-Code | Open Policy Agent 0.68 (Rego) |
| Identity / Secrets | HashiCorp Vault 1.17 |
| Structured Logging | structlog 24.4 |
| Distributed Tracing | OpenTelemetry (OTLP) |
| Metrics | Prometheus + Grafana |
| Token Signing | python-jose (HS256) |
| Settings | pydantic-settings (env-prefixed) |
| Runtime | Python 3.11+ |

---

## Module Reference

```
src/
├── main.py                        # FastAPI app entry point, /health, /metrics
├── config.py                      # Settings (AEGIS_* env vars via pydantic-settings)
├── adapters/
│   ├── base.py                    # LLMRequest / LLMResponse models + BaseAdapter ABC
│   ├── openai_adapter.py          # OpenAI completion adapter
│   ├── anthropic_adapter.py       # Anthropic completion adapter
│   └── local_llama.py             # Local Llama completion adapter
├── audit_vault/
│   ├── logger.py                  # AuditLogger (structlog + OTel spans)
│   └── compliance.py              # ComplianceReporter — SOC2 / GDPR report generation
├── control_plane/
│   ├── router.py                  # FastAPI router: POST /tasks, GET /tasks/{id}
│   └── scheduler.py               # Temporal workflow stubs (Phase 2)
├── governance/
│   ├── guardrails.py              # PII masking + prompt injection detection
│   ├── session_mgr.py             # JIT token issuance and validation
│   └── policy_engine/
│       └── opa_client.py          # PolicyEngine — async OPA evaluation via httpx
└── watchdog/
    ├── budget_enforcer.py         # BudgetEnforcer — USD cap + Prometheus metrics
    └── loop_detector.py           # LoopDetector — step/velocity circuit breaker
```

---

## Infrastructure Stack

All services are defined in `docker-compose.yml` and start with a single command.

| Service | Image | Port(s) | Purpose |
|---|---|---|---|
| `aegis-api` | Local build | `18000` | Aegis-OS Control Plane API |
| `vault` | `hashicorp/vault:1.17` | `8210` | Dynamic secrets & agent identity |
| `temporal` | `temporalio/auto-setup:1.24` | `7233`, `8088` | Durable workflow orchestration |
| `temporal-ui` | `temporalio/ui:2.47.2` | `18080` | Temporal web UI |
| `postgresql` | `postgres:16` | internal | Temporal backend persistence |
| `opa` | `openpolicyagent/opa:0.68.0` | `8181` | Policy evaluation server |
| `prometheus` | `prom/prometheus:v2.54.0` | `19090` | Metrics scraping |
| `grafana` | `grafana/grafana:11.2.0` | `13000` | Metrics dashboards |
| `code-scalpel` | `ghcr.io/astral-sh/uv:python3.12-bookworm-slim` | `18090` | Code Scalpel MCP server (SSE: `/sse`) |

OPA automatically loads all `*.rego` files from the `policies/` directory on startup.

---

## API Reference

### `GET /health`

Returns the service liveness status.

```json
{ "status": "ok", "service": "aegis-os" }
```

### `GET /metrics`

Prometheus metrics endpoint. Key metrics exposed:

| Metric | Type | Labels | Description |
|---|---|---|---|
| `aegis_tokens_consumed_total` | Counter | `agent_type` | Total tokens consumed |
| `aegis_budget_remaining_usd` | Gauge | `session_id` | Remaining USD budget for a session |

### `POST /api/v1/tasks`

Route an agent task through the Aegis Governance Loop and receive a scoped JIT session token.

**Request body:**

```json
{
  "task_id": "optional-uuid-v4",
  "description": "Summarize Q3 financials for EMEA region",
  "agent_type": "finance",
  "requester_id": "user:alice@corp.com",
  "metadata": { "department": "finance", "cost_center": "CC-042" }
}
```

`agent_type` must be one of: `finance` | `hr` | `it` | `legal` | `general`

**Response:**

```json
{
  "task_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "agent_type": "finance",
  "session_token": "<signed-jwt>",
  "message": "Task routed to finance agent"
}
```

The returned `session_token` is a HS256-signed JWT with a 15-minute expiry, scoped to the requested agent type.

### `GET /api/v1/tasks/{task_id}`

Retrieve the current status of a routed task by its UUID.

---

## Configuration

All settings are loaded from environment variables prefixed with `AEGIS_` and can be overridden in `docker-compose.yml` or a `.env` file.

| Variable | Default | Description |
|---|---|---|
| `AEGIS_ENV` | `development` | Runtime environment |
| `AEGIS_VAULT_ADDR` | `http://localhost:8200` | HashiCorp Vault address |
| `AEGIS_VAULT_TOKEN` | `aegis-dev-root-token` | Vault root token (dev only) |
| `AEGIS_TEMPORAL_HOST` | `localhost:7233` | Temporal gRPC host |
| `AEGIS_OPA_URL` | `http://localhost:8181` | OPA server base URL |
| `AEGIS_TOKEN_EXPIRY_SECONDS` | `900` | JIT token lifetime (15 min) |
| `AEGIS_TOKEN_SECRET_KEY` | *(change in prod)* | HS256 signing key |
| `AEGIS_MAX_AGENT_STEPS` | `10` | Circuit breaker step threshold |
| `AEGIS_MAX_TOKEN_VELOCITY` | `10000` | Max tokens per single step |
| `AEGIS_BUDGET_LIMIT_USD` | `10.0` | Default per-session USD cap |

> **Security note:** `AEGIS_TOKEN_SECRET_KEY` and `AEGIS_VAULT_TOKEN` must be replaced with strong secrets before any production deployment. Use HashiCorp Vault or your platform's secrets manager.

---

## Quick Start

**Prerequisites:** Docker and Docker Compose.

**1. Clone the repository:**

```bash
git clone https://github.com/3D-Tech-Solutions/aegis-os.git
cd aegis-os
```

**2. Start the full control plane stack:**

```bash
docker-compose up -d
```

This spins up the Aegis API, OPA policy server, Temporal workflow engine, HashiCorp Vault, and the Prometheus/Grafana observability stack.

**3. Verify the API is healthy:**

```bash
curl http://localhost:18000/health
# → {"status":"ok","service":"aegis-os"}
```

**4. Route a task and receive a JIT session token:**

```bash
curl -s -X POST http://localhost:18000/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "description": "Draft an expense report summary for Q3",
    "agent_type": "finance",
    "requester_id": "user:alice@corp.com"
  }' | jq .
```

**5. View dashboards:**

| Interface | URL |
|---|---|
| Prometheus | http://localhost:19090 |
| Grafana | http://localhost:13000 |
| Temporal UI | http://localhost:18080 |
| Vault UI | http://localhost:8210 |
| OPA API | http://localhost:8181/v1/data |
| Code Scalpel MCP | http://localhost:18090/sse |

---

## Development

**Install dependencies (Python 3.11+ required):**

```bash
pip install -e ".[dev]"
```

**Run the test suite:**

```bash
pytest
```

Tests are located in `tests/` and cover the budget enforcer, compliance reporter, guardrails, loop detector, and session manager. Coverage is reported against the `src/` package.

**Lint and type-check:**

```bash
ruff check src/ tests/
mypy src/
```

**Run the API locally (without Docker):**

```bash
uvicorn src.main:app --reload --port 8000
```

---

## Roadmap

See [docs/roadmap.md](docs/roadmap.md) for the full execution plan including
team-specific task breakdowns and detailed Go/No-Go gate criteria.

| Version | Phase | Gate | Timeline |
|---|---|---|---|
| `v0.1.0` | Current prototype | — | — |
| `v0.2.0` | Aegis Governance Loop Integration | Gate 1 | Weeks 1–4 |
| `v0.4.0` | Durable Orchestration (Temporal.io) | Gate 2 | Weeks 5–8 |
| `v0.6.0` | Glass Box Control Plane — React UI + Grafana | Gate 3 | Weeks 9–12 |
| `v0.8.0` | Zero-Trust Hardening + MCP Agent Mesh | Gate 4 | Weeks 13–16 |
| `v1.0.0` | Release Hardening + External Security Review | Gate 5 | Weeks 17–20 |

**v1.0 Release Criteria** — each line is verified by a named Go/No-Go gate:

- Zero PII leakage across all adversarial input classes (Gates 1, 5)
- No session exceeds USD budget by more than 1%; no false-positive halts (Gates 1, 4, 5)
- Every task linked to a complete, tamper-evident, OTel-correlated audit trace (Gates 1, 3, 5)
- System survives LLM provider outage without losing active task state (Gates 2, 5)
- SOC2/GDPR report auto-generated for any 24-hour window; passes auditor checklist (Gates 3, 5)
- Zero hardcoded credentials; all secrets sourced from Vault with live rotation (Gates 4, 5)
- Aegis Governance Loop OpenAPI spec, audit event schema, and Rego library published (Gates 4, 5)

---

Maintained by [3D Tech Solutions](https://github.com/3D-Tech-Solutions)

---

## License

Licensed under the **Apache License, Version 2.0**. See [LICENSE](LICENSE) for the full text.

The **core runtime** (Governance Loop, adapters, watchdog, audit vault, OPA policy library) is open source under Apache 2.0 — free to use, modify, fork, and deploy in commercial and proprietary environments. Enterprise features (hosted control plane, advanced multi-agent UI, premium support contracts) are available commercially from [3D Tech Solutions](https://github.com/3D-Tech-Solutions).

Contributions are welcome. By submitting a pull request you agree that your contribution is licensed under Apache 2.0 and you grant the implicit patent licence described in the licence terms. See [CONTRIBUTING.md](CONTRIBUTING.md) for details.