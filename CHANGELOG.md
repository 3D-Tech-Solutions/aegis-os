# Changelog

All notable changes to Aegis-OS are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

---

## [0.4.0] — 2026-03-09

**Phase 2 — Durable Orchestration.** Temporal-backed execution now survives
retries, worker restarts, approval pauses, and provider-failure recovery with
validated audit continuity, replay-safe budget accounting, and release-grade
documentation for HITL operations.

### Added

**Platform (P2-1, P2-2, P2-3, P2-4)**
- `src/control_plane/scheduler.py` now provides a real `AgentTaskWorkflow`
	mapped 1:1 to the five Governance Loop stages with durable Temporal activity
	execution, workflow queries/signals, and restart-safe state propagation.
- `src/control_plane/data_converter.py` adds encrypted Temporal payload
	persistence backed by Fernet so prompts, intermediate context, and workflow
	inputs are not stored in plaintext workflow history.
- `tests/test_temporal_workflow.py`, `tests/test_p2_retry.py`,
	`tests/test_p2_encryption.py`, `tests/test_p2_chaos.py`, and
	`tests/test_no_workflow_stubs.py` now enforce activity mapping, retry
	backoff, encrypted history, kill-and-resume continuity, and stub-free
	workflow activities.

**Security & Governance (S2-1, S2-2, S2-3, S2-4)**
- Human-in-the-loop `PendingApproval` workflow state added with approve, deny,
	and timeout handling wired through Temporal signals and task-scoped queries.
- `POST /api/v1/tasks/{task_id}/approve` and
	`POST /api/v1/tasks/{task_id}/deny` now enforce OPA-backed admin-only RBAC,
	structured task lookup, and scoped JIT-token validation.
- Sender-constrained JIT token rotation now re-issues JWTs and DPoP proofs on
	retry while preserving `cnf.jkt` binding and rejecting replay of prior retry
	credentials.
- `tests/test_hitl_pending_approval.py`, `tests/test_hitl_endpoints.py`,
	`tests/test_hitl_rbac_matrix.py`, and `tests/test_jit_retry_rotation.py`
	provide the Gate 2 approval, RBAC, and adversarial token coverage.

**Watchdog & Reliability (W2-1, W2-2, W2-3, W2-4)**
- Budget and loop state now round-trip through durable workflow-owned
	checkpoints, allowing exact restart recovery and replay-safe accounting.
- Pending-approval Prometheus metrics and the `aegis_hitl_stuck` alert now
	track live workflow approval state and resolve cleanly after review.
- `tests/test_budget_recovery.py`, `tests/test_loop_recovery.py`,
	`tests/test_pending_approval_metrics.py`, `tests/test_prometheus_rules.py`,
	and `tests/test_budget_replay.py` now enforce restart recovery, alerting,
	and seeded 1,000-task replay correctness.

**Audit & Compliance (A2-1, A2-2, A2-3, A2-4)**
- Temporal workflow and activity inputs now preserve caller-supplied `task_id`
	and W3C `traceparent`, with propagation failures raising a dedicated audit
	error before downstream work continues.
- Lifecycle audit events now cover `started`, `retried`,
	`pending-approval`, `approved`, `denied`, `completed`, and `failed`, with
	deterministic `sequence_number` ordering and clock-skew protection.
- `tests/test_temporal_trace_propagation.py`,
	`tests/test_audit_lifecycle_count.py`, `tests/test_audit_lifecycle_events.py`,
	`tests/test_audit_provider_outage.py`, and `tests/test_audit_ordering.py`
	provide the Phase 2 propagation, lifecycle, outage, and ordering contracts.

**Frontend & DevEx (F2-1, F2-2, F2-3, F2-4)**
- `docs/deployment-guide.md`, `docs/api-reference.md`,
	`docs/runbooks/hitl-stuck-approval.md`, and
	`docs/runbooks/budget-exceeded.md` now reflect the live task-based HITL
	model, Temporal operations flow, and recovery/runbook procedures.
- `tests/test_deployment_guide_completeness.py`,
	`tests/test_api_reference_schemas.py`, `tests/test_api_reference_completeness.py`,
	`tests/test_hitl_timeout_contract.py`, `tests/test_runbook_completeness.py`,
	`tests/test_runbook_cross_references.py`, `tests/test_runbook_commands.py`,
	and `tests/test_runbook_budget_version.py` now enforce the Phase 2 docs and
	runbook contracts in CI.

### Changed
- `Dockerfile` now copies `LICENSE`, allowing `docker compose build aegis-api
	aegis-worker` to succeed when Hatchling resolves the project license file.
- `docker-compose.yml` no longer declares the obsolete Compose `version`
	field.
- `.pre-commit-config.yaml` now includes Phase 2 release guards for workflow
	stub detection and the encrypted Temporal data-converter plaintext guard.
- `docs/architecture_decisions.md` records the Platform-team-lead Gate 2
	sign-off for the `PendingApproval` state machine.

---

## [0.2.0] — 2026-03-05

**Phase 1 — Aegis Governance Loop Integration.** All five governance stages
are connected into a single tested pipeline. Gate 1 passed: 558 tests green,
zero failures, zero `xfail` markers. `mypy` and `ruff` report zero errors.

### Added

**Platform (P1-1, P1-2, P1-3)**
- `src/control_plane/orchestrator.py` — `Orchestrator.run()` sequences five governance stages in strict order: Guardrails pre-sanitize → OPA policy eval → SessionManager JIT token → LLM Adapter → Guardrails post-sanitize. Any stage failure short-circuits; no subsequent stage is called.
- `OrchestratorRequest` / `OrchestratorResult` Pydantic models; `task_id: UUID` auto-generated via `default_factory=uuid4` and propagated through every span, audit event, and budget session.
- `MissingTaskIdError` guard fires before Stage 4 — the LLM adapter is never called for an un-trackable request.
- `POST /api/v1/tasks` router endpoint delegates entirely to `orchestrator.run()`; no inline governance logic in the handler. `TaskResponse` extended with `tokens_used`, `model`, and `pii_found`.
- Error mapping: `BudgetLimitError → 429`, `PermissionError → 403`, `ValueError → 400`, `Exception → 500` (detail never leaks raw exception messages).
- `tests/test_orchestrator.py` (12 tests), `tests/test_orchestrator_no_bypass.py` (10 tests), `tests/test_router_p1_2.py` (26 tests), `tests/test_task_id_p1_3.py` (15 tests).

**Security & Governance (S1-1, S1-2, S1-3, S1-4)**
- `OpaUnavailableError` — raised on any 5xx, `ConnectError`, or `TimeoutException`; orchestrator fails closed with `PolicyDeniedError` and emits `policy.opa_unavailable` audit event.
- `PolicyResult` extended with `action` (`allow`/`mask`/`reject`) and `fields`; Stage 2b applies `Guardrails.scrub()` when OPA returns `action="mask"`.
- `policies/agent_access.rego` updated with explicit `llm.complete` allow rules for all five agent types; sensitive types (`finance`, `hr`, `legal`) receive `action="mask"`.
- Adversarial PII normalisation pipeline: invisible-char strip → NFKC → URL-decode → `@`-whitespace compaction. All five PII pattern classes handle whitespace/newline separators.
- `TokenScopeError` / `TokenExpiredError` — scope and expiry violations emit structured audit events before raising; `metadata["aegis_token"]` injected into every outbound `LLMRequest`.
- `tests/test_opa_wiring.py` (17 unit + 7 integration), `tests/test_pii_advanced.py` (135), `tests/pii_regression.json` (62 adversarial cases, zero leakage), `tests/test_pii_s14.py` (88), `tests/test_jit_token.py` (19). Guardrails coverage: 100% line, 100% branch.

**Watchdog & Reliability (W1-1, W1-2, W1-3, W1-4)**
- `BudgetEnforcer` — all monetary arithmetic migrated to `decimal.Decimal`; `record_spend()` raises `BudgetExceededError` synchronously in the same call frame; `check_budget()` pre-LLM guard prevents any adapter call after exhaustion. Stage 3.5 (`watchdog.pre-llm`) and Stage 4.5 (`watchdog.record-spend`) wired into the orchestrator.
- `LoopDetector` — `TokenVelocityError` (single-step velocity breach) and `PendingApprovalError` (`HUMAN_REQUIRED` signal) added as distinct exception types. Check order hardened: velocity first, then `HUMAN_REQUIRED`, then NO_PROGRESS streak. Stage 4.6 (`watchdog.loop-detect`) wired into the orchestrator.
- `src/watchdog/metrics.py` — single authoritative Prometheus registry; `_span_stage()` unified context manager increments `aegis_orchestrator_errors_total` on every stage exception. All nine pipeline stages use `_span_stage()`.
- `tests/test_budget_enforcer.py` (18), `tests/test_loop_detector.py` (34), `tests/test_metrics_w1_3.py` (18), `tests/test_metrics_completeness.py` (16), `tests/test_budget_stress.py` (5 — 500 tasks, < 0.5 s total).

**Audit & Compliance (A1-1, A1-2, A1-3)**
- `_span_stage()` context manager — opens a named OTel span with `task_id`, `agent_type`, `span.status`; records exception on error; closes all spans correctly even on mid-stage exceptions. Spans: `pre-pii-scrub`, `policy-eval`, `jit-token-issue`, `llm-invoke`, `post-sanitize`.
- `AuditLogger.stage_event()` — emits a structured JSON event for every pipeline stage outcome with mandatory `outcome`, `stage`, `task_id`, `agent_type` fields routed to the correct log level (allow/redact→info, deny→warning, error→error).
- Per-task `sequence_number` counter (monotonically increasing, per `task_id`, thread-safe) included on every `stage_event()` emission — enables gap and duplicate detection.
- `tests/test_otel_spans_a1_1.py` (10), `tests/test_audit_logger_a1_2.py` (15), `tests/test_audit_logger_a1_3.py` (22).

**Frontend & DevEx (F1-1, F1-2, F1-3)**
- `docs/audit-event-schema.json` — versioned JSON Schema draft-07 (v0.1.0); open standard output of the Aegis Governance Loop. Covers all 20 known field types with `additionalProperties: false`.
- `.pre-commit-config.yaml` — hooks for `ruff`, `mypy`, branding scan, and schema conformance; conformance hook fires on changes to `logger.py` or `audit-event-schema.json`.
- Replaced all "Governance Sandwich" references with "Aegis Governance Loop" across `README.md`, `docs/architecture_decisions.md`, and `docs/research.md`.
- `docs/agent-sdk-guide.md` enhanced with Aegis Governance Loop intro, Audit Event Schema Reference table (14 fields), and four standalone-runnable Python code blocks.
- `tests/test_schema_validity.py` (41), `tests/test_docs_branding.py` (17), `tests/test_sdk_guide_quickstart.py` (10), `tests/test_docs_links.py` (14).

### Changed
- `src/audit_vault/logger.py` — removed global OTel `TracerProvider` setup at import time; provider setup moved to `src/main.py`. Added `stage_event()`, per-task sequence counter, and `threading.Lock` for safe concurrent use.
- `src/control_plane/orchestrator.py` — full five-stage pipeline with `_span_stage()`, budget and loop watchdog stages, `_CONTROLLED_EXC` guard preventing duplicate audit events, and `MissingTaskIdError` guard.
- `src/governance/policy_engine/opa_client.py` — `PolicyResult` extended with `action`/`fields`; `OpaUnavailableError` added for fail-closed behaviour.
- `src/governance/session_mgr.py` — `TokenExpiredError` and `TokenScopeError` exception classes added.
- `src/watchdog/budget_enforcer.py` — all arithmetic converted to `Decimal`; `record_spend()` and `check_budget()` added; `BudgetEnforcer.__init__` accepts injectable `AuditLogger`.
- `src/watchdog/loop_detector.py` — `TokenVelocityError`, `PendingApprovalError` added; `max_agent_steps`, `max_token_velocity`, and `audit_logger` injectable parameters added.
- `pyproject.toml` — added `jsonschema[format-nongpl]>=4.23.0`, `pre-commit>=3.8.0`, `testcontainers>=4.8.0` to `[project.optional-dependencies] dev`.

---

## [0.1.0] — 2026-03-03

Initial prototype release establishing the core governance modules and control plane skeleton.

### Added

**Control Plane**
- `FastAPI` application entry point (`src/main.py`) with `/health` and `/metrics` endpoints
- Prometheus metrics endpoint via `prometheus_client.make_asgi_app()`
- `AgentScheduler` stub in `src/control_plane/scheduler.py` with in-memory `WorkflowHandle` tracking and `WorkflowStatus` state machine (pending → running → completed / failed / human_intervention_required)
- `POST /api/v1/tasks` router with `TaskRequest`/`TaskResponse` Pydantic models and `AgentType` enum (`finance`, `hr`, `it`, `legal`, `general`)
- `GET /api/v1/tasks/{task_id}` stub endpoint

**Governance**
- `Guardrails` class with five compiled PII regex patterns: `email`, `ssn`, `credit_card`, `phone_us`, `ip_address`
- `Guardrails.mask_pii()` returning `MaskResult` with redacted text and detected type list
- `Guardrails.check_prompt_injection()` with six injection pattern detectors (`PromptInjectionError` on match)
- `build_agent_input()` factory function producing typed `AgentInput` model
- `SessionManager` issuing HS256-signed JWTs with `jti`, `sub`, `agent_type`, `iat`, `exp`, and `metadata` claims
- `SessionManager.validate_token()`, `is_expired()`, `time_remaining()`, `issued_at_utc()` helpers
- `PolicyEngine` async OPA client using `httpx` against `/v1/data/aegis/<policy_name>`
- `PolicyInput` / `PolicyResult` Pydantic models

**Policy-as-Code**
- `policies/agent_access.rego`: default-deny, per-agent-type resource allowlists for read/write, PII masking requirement enforcement for `finance`, `hr`, `legal`
- `policies/budget.rego`: budget extension approval rules — manager approval for ≤$50, executive approval required for >$500

**Watchdog**
- `BudgetEnforcer` with `create_session()`, `record_tokens()`, per-token cost model, `BudgetExceededError`
- Prometheus metrics: `aegis_tokens_consumed_total` (counter, label: `agent_type`), `aegis_budget_remaining_usd` (gauge, label: `session_id`)
- `LoopDetector` with `create_context()`, `record_step()`, `LoopSignal` enum, `LoopDetectedError`
- Circuit breakers: max step count without `PROGRESS` signal (`AEGIS_MAX_AGENT_STEPS`, default 10), max token velocity per step (`AEGIS_MAX_TOKEN_VELOCITY`, default 10,000)

**Audit Vault**
- `AuditLogger` using `structlog` with JSON renderer, ISO timestamps, log level, and stack info processors
- OpenTelemetry `TracerProvider` with `BatchSpanProcessor` and `ConsoleSpanExporter`; `audit()` method opens correlated OTel span
- `ComplianceReporter` with `record_event()` and `generate_report()` for `SOC2` and `GDPR` `ComplianceFramework` values
- `AuditEvent` and `ComplianceReport` immutable Pydantic models

**Adapters**
- `BaseAdapter` ABC with `LLMRequest` / `LLMResponse` standardized models
- Adapter stubs: `openai_adapter.py`, `anthropic_adapter.py`, `local_llama.py`

**Configuration**
- `pydantic-settings` `Settings` class with `AEGIS_` prefix env var loading
- Configurable: `vault_addr`, `vault_token`, `temporal_host`, `opa_url`, `token_expiry_seconds`, `token_secret_key`, `token_algorithm`, `max_agent_steps`, `max_token_velocity`, `budget_limit_usd`

**Infrastructure**
- `docker-compose.yml` with eight services: `aegis-api`, `vault` (HashiCorp 1.17), `temporal` (auto-setup 1.24), `temporal-ui` (2.28), `postgresql` (16), `opa` (0.68.0), `prometheus` (2.54.0), `grafana` (11.2.0)
- `Dockerfile` for the Aegis API
- `docs/prometheus.yml` scrape configuration

**Tests**
- `tests/test_budget_enforcer.py`
- `tests/test_compliance.py`
- `tests/test_guardrails.py`
- `tests/test_loop_detector.py`
- `tests/test_session_mgr.py`

**Documentation**
- `README.md` — full project documentation with architecture diagram, module reference, API reference, configuration table, and quick start guide
- `docs/architecture_decisions.md` — ADR-001 (OPA), ADR-002 (JIT tokens), ADR-003 (loop circuit breaker)
- `docs/roadmap.md` — v1.0 four-phase execution plan
- `docs/research.md` — eight strategic research domains
- `docs/threat-model.md` — STRIDE analysis across all components
- `SECURITY.md` — vulnerability reporting process and pre-production hardening checklist
- `docs/policy-guide.md` — Rego authoring and testing guide
- `docs/agent-sdk-guide.md` — integration guide for agent developers
- `docs/deployment-guide.md` — Kubernetes and production deployment guide
- `docs/compliance-guide.md` — SOC2 and GDPR report generation guide
- `docs/runbooks/` — operational runbooks for budget exceeded, loop detected, OPA server down, token renewal failure

---

[Unreleased]: https://github.com/3D-Tech-Solutions/aegis-os/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/3D-Tech-Solutions/aegis-os/compare/v0.2.0...v0.4.0
[0.2.0]: https://github.com/3D-Tech-Solutions/aegis-os/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/3D-Tech-Solutions/aegis-os/releases/tag/v0.1.0
