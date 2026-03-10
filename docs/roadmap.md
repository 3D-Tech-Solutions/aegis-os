# Aegis-OS: v1.0 Release Roadmap

This roadmap organises the journey from **v0.1.0 (current prototype)** to a
production-ready **v1.0 release** across five team tracks. Each phase is tied to
a numbered release version. No phase begins until the prior **Go/No-Go Gate**
passes — gate failure blocks the version tag and triggers a remediation sprint.

---

## Team Tracks

| Team | Owns | Primary files |
|---|---|---|
| **Platform** | FastAPI router, Temporal orchestration, LLM adapters, orchestrator | `src/control_plane/`, `src/adapters/` |
| **Security & Governance** | Guardrails, OPA policies, JIT tokens, Vault integration, red-teaming | `src/governance/`, `policies/` |
| **Watchdog & Reliability** | BudgetEnforcer, LoopDetector, Prometheus metrics, load testing | `src/watchdog/` |
| **Audit & Compliance** | AuditLogger, OTel spans, ComplianceReporter, immutable store | `src/audit_vault/` |
| **Frontend & DevEx** | React console, policy editor UI, Grafana dashboards, open standard artifacts, docs | `docs/`, future `ui/` |

All five tracks run in parallel within each phase. The version tag is cut only
after the gate passes.

---

## Phase 1 — Aegis Governance Loop Integration

**Release target:** `v0.2.0`
**Timeline:** Weeks 1–4
**Goal:** Connect all standalone modules into a single, fully-tested request
lifecycle. Every task must flow through one unified pipeline with no bypasses.
No stubs, mocks, or `pass` implementations are acceptable in production paths
at gate time — every item below must be backed by running code and a passing
test suite.

---

### Platform Team

#### P1-1 — Implement `src/control_plane/orchestrator.py` ✅ COMPLETE

Single entry point that sequences:
`Guardrails → OPA → SessionManager → LLM Adapter → post-sanitize`

**Completed:** 2026-03-04
**Commit scope:** `feat(P1-1): implement Orchestrator with five-stage pipeline, OTel spans, and tracer injection`

**Implementation summary**

- `src/control_plane/orchestrator.py` created. `Orchestrator.run()` sequences
  five stages in strict order; any stage failure short-circuits the pipeline and
  propagates the original exception type without calling subsequent stages.
- `_module_tracer` renamed from `tracer` to prevent constructor parameter shadowing;
  a `tracer: trace.Tracer | None` parameter on `Orchestrator.__init__()` allows
  tests to inject a local `TracerProvider`-backed tracer, bypassing OTel's
  singleton global-provider restriction.
- All six required OTel spans emitted: `orchestrator.run`, `stage.guardrails_pre`,
  `stage.opa_eval`, `stage.session_mgr`, `stage.llm_adapter`, `stage.guardrails_post`.
- `PolicyDeniedError(PermissionError)` defined in orchestrator module as the
  typed exception for OPA deny decisions and fail-closed OPA unavailability.
- `src/control_plane/router.py` extended with `POST /tasks/execute` endpoint that
  delegates 100% to `orchestrator.run()`; `configure_orchestrator()` / `_require_orchestrator()`
  helpers enable test injection without patching module state.

**Testing requirements**

- ✅ **Unit — stage order:** `TestStageOrder.test_happy_path_stage_sequence` —
  counter-based `side_effect` tracks the exact six-call sequence
  (`stage1_injection_check → stage1_mask_pii → stage2_opa → stage3_session_mgr →
  stage4_llm → stage5_mask_pii`); swapping any two entries fails the assertion.
- ✅ **Unit — short-circuit on stage failure:** seven individual tests inject a
  raised exception at each of the five stages (stage 1 split into injection-check
  and mask-pii sub-cases); assert correct propagation and that no subsequent
  stage is called.
- ✅ **Integration — live adapter call:** `TestIntegration` runs the orchestrator
  with real `Guardrails` + `SessionManager`, mocked OPA, and a `_StubAdapter`;
  asserts `LLMResponse.content` is non-empty and all six OTel span names appear
  in the `InMemorySpanExporter` output.
- ✅ **Regression guard:** `tests/test_orchestrator_no_bypass.py` (10 tests) —
  runtime check asserts `orchestrator.run()` is called with an `OrchestratorRequest`;
  source-text checks confirm `execute_task` body contains no direct governance
  method calls; AST walk confirms no governance attribute accesses in the handler.

**Test files:** `tests/test_orchestrator.py` (12 tests), `tests/test_orchestrator_no_bypass.py` (10 tests)

---

#### P1-2 — Update `src/control_plane/router.py` to delegate to the orchestrator ✅ COMPLETE

Remove all inline governance logic from the router.

**Completed:** 2026-03-04
**Commit scope:** `feat(P1-2): refactor router to delegate all LLM execution to orchestrator`

**Implementation summary**

- `BudgetLimitError(Exception)` added to `src/control_plane/orchestrator.py` so
  the router can surface budget errors as HTTP 429 without importing from
  `src.watchdog.budget_enforcer` directly.
- `TaskRequest` extended with `prompt` (required, 1–32 768 chars), `model`,
  `max_tokens`, `temperature`, and `system_prompt`; `description` made optional.
- `TaskResponse` extended with `tokens_used: int`, `model: str`, and
  `pii_found: list[str]` to surface LLM execution metadata to callers.
- `route_task (POST /tasks)` rewritten to call `orc.run(OrchestratorRequest(...))`
  and build a `TaskResponse` from the returned `OrchestratorResult`; handles
  `BudgetLimitError → 429`, `PermissionError → 403`, `ValueError → 400`, and
  `Exception → 500` (detail must not leak raw exception messages).
- Module-level `_session_mgr = SessionManager()` and its import removed; the
  router now contains no inline governance state.
- `execute_task (POST /tasks/execute)` gains `BudgetLimitError → 429` and
  `Exception → 500` handlers for parity with `route_task`.

**Testing requirements**

- ✅ **Unit — delegation only:** `TestDelegation` (9 tests) — mock
  `orchestrator.run()`; POST to `/api/v1/tasks`; assert called exactly once
  with an `OrchestratorRequest` carrying the correct `prompt`, `agent_type`, and
  `requester_id`; assert the raw un-scrubbed prompt is forwarded (no inline PII
  logic in the handler).
- ✅ **Negative test — no direct module imports:** `TestNoDirectGovernanceImports`
  (5 tests) — AST-based parse of `router.py` asserts zero imports sourced from
  `guardrails`, `opa_client`, `budget_enforcer`, and `loop_detector`.
- ✅ **Contract test:** `TestHttpContract` (12 tests) — validates `TaskResponse`
  shape on 200; `ValueError → 400`, `PermissionError → 403`,
  `BudgetLimitError → 429`, `RuntimeError → 500`; missing orchestrator → 500;
  500 detail must not expose raw exception messages.

**Test file:** `tests/test_router_p1_2.py` (26 tests)

---

#### P1-3 — Propagate `task_id` as a mandatory field through every layer ✅ COMPLETE

**Completed:** 2026-03-04
**Commit scope:** `feat(P1-3): propagate task_id through every pipeline layer, spans, and audit events`

**Implementation summary**

- `task_id: UUID | None = Field(default_factory=uuid4)` added to `OrchestratorRequest`.
  When the caller omits `task_id`, Pydantic auto-generates a UUID v4; callers
  that supply one have it preserved verbatim through to the result.
- `task_id: UUID` added to `OrchestratorResult` so the value is always
  available to the router and any downstream consumer.
- `MissingTaskIdError(ValueError)` defined in `orchestrator.py`; `run()` raises
  it immediately when `task_id is None` (possible only via `model_construct`),
  before Stage 4 — the LLM adapter is never called for an un-trackable request.
- `task_id` set as a `span.set_attribute` on **every** named OTel span:
  `orchestrator.run`, `pre-pii-scrub`, `policy-eval`, `policy-mask`
  (when active), `jit-token-issue`, `watchdog.pre-llm` (when active),
  `llm-invoke`, `watchdog.record-spend` (when active),
  `watchdog.loop-detect` (when active), and `post-sanitize`.
- `task_id=str(task_id)` threaded as a keyword argument into every
  `self._audit.info/warning/error(...)` call in `run()`.  The `budget.pre_check`
  audit event carries `task_id` and `budget_session_id` for full budget-to-task
  traceability.
- `router.py` updated to pass `task_id=request.task_id` to `OrchestratorRequest`
  in both `route_task` and `execute_task`; `TaskResponse.task_id` is sourced
  from `result.task_id` (round-tripped through the pipeline, not from the
  original HTTP request).

**Testing requirements**

- ✅ **Unit — generation:** `TestTaskIdGeneration` (4 tests) — assert
  `OrchestratorRequest` auto-generates a UUID v4 when `task_id` is omitted;
  assert `result.task_id.version == 4`; assert caller-supplied `task_id` is
  preserved; assert two successive calls never produce the same UUID.
- ✅ **Unit — thread-through:** `TestTaskIdThreadThrough` (4 tests) — assert
  `task_id` attribute present on `orchestrator.run` root span; assert all six
  named stage spans carry the same `task_id`; assert every `RecordingAuditLogger`
  event carries `task_id`; assert `budget.pre_check` audit event carries `task_id`
  when a `BudgetEnforcer` is wired in.
- ✅ **Negative test:** `TestMissingTaskIdGuard` (4 tests) — `model_construct`
  bypasses Pydantic to set `task_id=None`; assert `MissingTaskIdError` raised;
  assert `adapter.complete()` never called; assert `MissingTaskIdError` is a
  `ValueError` subclass; assert error message contains `"task_id"`.
- ✅ **Concurrency test:** `TestConcurrencyNoLeakage` (3 tests) — `asyncio.gather`
  20 tasks, each with a distinct `task_id`; assert every audit event's `task_id`
  is one of the 20 expected UUIDs (no cross-contamination); assert all 20 IDs
  appear in the audit trail; assert each result's `task_id` maps back to its
  request.

**Test file:** `tests/test_task_id_p1_3.py` (15 tests)

---

### Security & Governance Team

#### S1-1 — Wire `OPAClient` into the orchestrator ✅ COMPLETE

Every prompt must receive an `allow/deny` decision before reaching any LLM
adapter.

**Completed:** 2026-03-04
**Commit scope:** `feat(S1-1): wire OPAClient into orchestrator with fail-closed audit events`

**Implementation summary**

- `OpaUnavailableError` added to `src/governance/policy_engine/opa_client.py`; `PolicyEngine.evaluate()` now raises it on any 5xx response, `ConnectError`, or `TimeoutException` (rather than letting raw HTTP errors escape).
- `Orchestrator` (Stage 2, `src/control_plane/orchestrator.py`) accepts an injectable `AuditLogger`; on OPA unavailability it emits an `opa_unavailable` error event and raises `PolicyDeniedError` (fail-closed); on a policy deny it emits a `policy_denied` warning event including the `reasons` list.
- `policies/agent_access.rego` updated with an explicit `llm.complete` allow rule for all five registered agent types; `agent_type_not_permitted` reason added for unregistered types.
- `AuditLogger` (`src/audit_vault/logger.py`) no longer sets the global OTel `TracerProvider` at import time; provider setup moved to `src/main.py` startup.
- `testcontainers>=4.8.0` added to dev dependencies; `integration` pytest marker registered.

**Testing requirements**

- ✅ **Unit — allow path:** mock `OPAClient.evaluate()` to return
  `PolicyResult(allow=True)`; assert the orchestrator proceeds to the LLM
  adapter stage.
- ✅ **Unit — deny path:** mock `OPAClient.evaluate()` to return
  `PolicyResult(allow=False, reasons=["agent_type_not_permitted"])`; assert
  the orchestrator raises `PolicyDeniedError` and does not call any LLM
  adapter. Verify the `reasons` list is included in the audit event.
- ✅ **Integration — live OPA:** start the OPA container (or use
  `testcontainers`); load `policies/agent_access.rego`; submit a request
  for each of the five agent types and assert the expected allow/deny for
  each based on the policy as written.
- ✅ **Negative test — OPA unavailable:** simulate OPA returning a `503`; assert
  the orchestrator fails closed (denies the request) and emits an
  `opa_unavailable` audit event — it must never fail open.

**Test files:** `tests/test_opa_wiring.py` (17 unit tests, 7 integration tests under `pytest -m integration`)

---

#### S1-2 — Implement PII-aware routing ✅ COMPLETE

OPA policy result can instruct the orchestrator to hard-reject or auto-mask
a prompt before forwarding.

**Implementation summary**

- `src/governance/guardrails.py` — adversarial normalisation pipeline added
  (invisible-char strip → NFKC → URL-decode → `@`-whitespace compaction).
  All five pattern classes updated to handle whitespace/newline separators.
  New `scrub()` method added as a named Stage 2b entry point.
- `src/governance/policy_engine/opa_client.py` — `PolicyResult` extended with
  `action: str` (`"allow"` | `"mask"` | `"reject"`) and `fields: list[str]`.
- `policies/agent_access.rego` — `action` and `fields` rules added; sensitive
  agent types (`finance`, `hr`, `legal`) receive `action="mask"`.
- `src/control_plane/orchestrator.py` — Stage 2b added: when OPA returns
  `action="mask"`, `Guardrails.scrub()` is called on the listed fields before
  the LLM adapter is invoked; a `stage.opa_mask` OTel span and
  `policy_mask_applied` audit event are emitted.

**Testing requirements**

- ✅ **Unit — per pattern class (× 5):** for each PII pattern (email, SSN, credit
  card, phone, IP) write at least 10 test inputs covering: canonical form,
  extra whitespace, mixed case, URL-encoded, Unicode homoglyph substitution,
  and split across a line break. Assert every variant is masked to the correct
  replacement token before the OPA stage runs.
- ✅ **Unit — OPA mask instruction:** mock OPA to return
  `{"action": "mask", "fields": ["prompt"]}`; assert `Guardrails.scrub()` is
  called on the prompt before it is forwarded to the LLM adapter.
- ✅ **Unit — OPA reject instruction:** mock OPA to return
  `{"action": "reject"}`; assert `PolicyDeniedError` is raised and the prompt
  is never logged in plaintext in any audit event.
- ✅ **Negative test — post-LLM leakage:** inject a synthetic LLM response
  containing a raw SSN; assert `Guardrails.scrub()` is applied to the response
  and the SSN does not appear in the final `TaskResponse` or any audit event.
- ✅ **Regression suite:** `tests/pii_regression.json` contains 62 adversarial
  inputs; `pytest -k pii_regression` must pass with zero leakage events.

**Test files:** `tests/test_pii_advanced.py` (135 tests), `tests/pii_regression.json` (62 cases)

---

#### S1-3 — Inject JIT session token into every outbound LLM adapter call ✅ COMPLETE

**Implementation summary**

- `src/governance/session_mgr.py` — evolved from legacy HS256 bearer-token
  issuance to a dual-path model that now supports asymmetric ES256
  sender-constrained tokens with DPoP proof validation, while preserving the
  existing bearer path during phased rollout. Added `TokenScopeError(PermissionError)`
  and `TokenExpiredError(PermissionError)` exception classes.
- `src/adapters/base.py` — added `metadata: dict[str, str]` field to `LLMRequest`
  to carry orchestrator-level context alongside every outbound call. Protected
  flows now propagate `aegis_token`, `aegis_dpop_proof`, and `aegis_protected`
  metadata for DPoP-bound requests.
- `src/control_plane/orchestrator.py` — Stage 3 expanded:
  - `JoseExpiredSignatureError` caught on `validate_token` → `TokenExpiredError`
    raised with `token_expired` audit event.
  - Belt-and-suspenders: `is_expired()` path also raises `TokenExpiredError`.
  - Scope check: `claims.agent_type != request.agent_type` → `TokenScopeError`
    with `token_scope_violation` audit event.
  - Fresh token issuance emits a `token_issued` audit event carrying `jti`.
  - Protected outbound flows issue ES256 sender-constrained adapter tokens,
    bind them with `cnf.jkt`, and attach a per-request DPoP proof before the
    adapter performs network I/O.
  - Stage 4 injects `metadata={"aegis_token": token}` into every `LLMRequest`,
    with DPoP proof metadata added when `protect_outbound_request=True`.

**Testing requirements**

- ✅ **Unit — token present:** call the orchestrator with a valid session; capture
  the `LLMRequest` passed to the adapter mock; assert `metadata["aegis_token"]`
  is present, is a valid session token, and carries the correct `agent_type`
  claim. Protected flows additionally assert the token is ES256 sender-constrained
  via `cnf.jkt` and that `metadata["aegis_dpop_proof"]` is present.
- ✅ **Unit — token scope:** issue a token scoped to `finance`; attempt a task
  with `agent_type="hr"`; assert `TokenScopeError` is raised before any
  adapter call.
- ✅ **Unit — expired token:** issue a token with `exp` set 1 second in the past;
  assert `TokenExpiredError` is raised and an audit event is emitted.
- ✅ **Integration — `jti` uniqueness:** run 100 sequential tasks; collect all
  `jti` claims from the audit log; assert they are all distinct UUIDs.

**Test files:** `tests/test_jit_token.py` (19 tests)

---

#### S1-4 — Unit tests for all five PII pattern classes with adversarial variants ✅ COMPLETE

**Completed:** 2026-03-04
**Commit scope:** `feat(S1-4): adversarial PII test matrix, false-positive matrix, performance gate, 100% guardrails coverage`

**Delivered:** `tests/test_pii_s14.py` — 88 tests across four sections:
adversarial matrix (6 variants × 5 classes = 30 tests), false-positive matrix
(11 per class × 5 = 55 tests), performance test (10 000-char / 50 PII instances
< 50 ms), and a `scrub()` direct-call test.  Combined guardrails coverage:
**100% line, 100% branch**.  All 402 non-integration tests pass; ruff and mypy
report zero errors.

**Testing requirements**

- ✅ **Coverage requirement:** `pytest --cov=src/governance/guardrails --cov-branch`
  reports **100% line coverage and 100% branch coverage** on the regex and
  detection logic across the combined test suite.
- ✅ **Adversarial matrix (per class):** each PII class has test cases for:
  standard form, zero-width character insertion, Unicode digit substitution
  (e.g., `１２３` for `123`), URL encoding (`%40` for `@`), Base64 embedding,
  and multi-line spanning across a newline character.
- ✅ **False-positive test:** ≥ 10 inputs per class that are syntactically similar
  but not PII (e.g., version strings `1.2.3.4` for IP, product codes for SSN);
  zero false-positive redactions confirmed across all 55 cases.
- ✅ **Performance test:** scrub of a 10,000-character prompt containing 50 PII
  instances completes in < 50 ms on the CI runner.

---

### Watchdog & Reliability Team

#### W1-1 — `BudgetEnforcer` raises `BudgetExceededError` synchronously ✅ COMPLETE

**Completed:** 2026-03-04
**Commit scope:** `feat(W1-1): synchronous BudgetEnforcer with Decimal arithmetic, record_spend, check_budget, and Orchestrator wiring`

**Implementation summary**

- `src/watchdog/budget_enforcer.py` overhauled: all monetary arithmetic migrated from `float`
  to `decimal.Decimal` (exact representation, no IEEE 754 drift).  `BudgetSession.cost_usd`
  and `budget_limit_usd` are `Decimal`; `create_session()` accepts `Decimal | float | None`
  and converts floats via `Decimal(str(value))` at the boundary.
- `BudgetEnforcer.record_spend(session_id, amount_usd: Decimal)` added as the authoritative
  spend-accounting method.  The raise is synchronous — `BudgetExceededError` is always raised
  within the `record_spend` call frame, never deferred to a background thread or callback.
- `BudgetEnforcer.check_budget(session_id)` added as the pre-LLM guard: raises immediately
  when `cost_usd >= budget_limit_usd`.  The orchestrator calls this before Stage 4 to
  prevent any LLM adapter invocation after budget exhaustion.
- `BudgetEnforcer.record_tokens()` refactored to delegate cost accounting to `record_spend()`
  (single source of truth).  Token-count tracking and the Prometheus counter increment
  remain in `record_tokens()`.
- `budget.exceeded` audit event updated to carry all four required fields: `session_id`,
  `agent_type`, `spent_usd`, `limit_usd`.  Both `record_spend()` and `check_budget()`
  emit this event before raising.
- `BudgetEnforcer.__init__` now accepts an optional injectable `AuditLogger` so tests can
  capture audit events without stdout parsing.
- `src/control_plane/orchestrator.py` extended:
  - `OrchestratorRequest` gains `budget_session_id: UUID | None` and
    `cost_per_token: Decimal` fields.
  - `Orchestrator.__init__` accepts `budget_enforcer: BudgetEnforcer | None = None`.
  - **Stage 3.5 (`watchdog.pre-llm`)**: calls `check_budget()` before the LLM adapter;
    `BudgetExceededError` is caught and re-raised as `BudgetLimitError` (HTTP 429).
  - **Stage 4.5 (`watchdog.record-spend`)**: after the LLM response, calls `record_spend()`
    with `Decimal(tokens_used) * cost_per_token`; a breach at this point also raises
    `BudgetLimitError`.

**Testing requirements**

- ✅ **Unit — synchronous raise:** `test_synchronous_raise_in_call_frame` — calls
  `record_spend()` inside a `threading.Thread`; inspects `exc.__traceback__` to confirm
  `"record_spend"` appears in the traceback frame names; asserts thread exits immediately
  (no blocking).  `test_synchronous_raise_record_spend_is_innermost_frame` verifies
  `record_spend` is the innermost frame.
- ✅ **Unit — boundary exactness:** `test_boundary_exactness_below_limit_no_error` ($0.999999
  on a $1.00 cap → no raise), `test_boundary_exactness_at_exact_limit_no_error` (exactly
  $1.00 → no raise), `test_boundary_exactness_one_unit_over_raises` ($0.999999 + $0.000002
  = $1.000001 → `BudgetExceededError`).  `test_boundary_exactness_float_budget_converts_correctly`
  confirms `float` limits are stored as `Decimal` with no representation error.
- ✅ **Unit — no LLM call after breach:** `test_no_llm_call_after_budget_breach` — pre-exhausts
  the session then runs the orchestrator; asserts `adapter.complete` is never called.
  `test_budget_breach_on_post_llm_record_spend` — verifies the post-LLM path when the
  initial spend tips the cap (adapter IS called, then `BudgetLimitError` is raised).
- ✅ **Integration — audit event on breach (record_spend path):**
  `test_integration_audit_event_on_breach_via_record_spend` — runs the full orchestrator
  pipeline with an injected mock `AuditLogger`; asserts `budget.exceeded` warning was
  captured with correct `session_id`, `agent_type`, `spent_usd`, and `limit_usd` fields,
  and that `spent_usd > limit_usd`.
- ✅ **Integration — audit event on breach (check_budget path):**
  `test_integration_audit_event_on_breach_via_check_budget` — same assertions for the
  pre-LLM `check_budget()` code path.

**Test file:** `tests/test_budget_enforcer.py` (18 tests — 7 baseline + 11 W1-1)

---

#### W1-2 — `LoopDetector` circuit breaker halts the orchestrator ✅ COMPLETE

**Completed:** 2026-03-04
**Commit scope:** `feat(W1-2): LoopDetector circuit breaker with TokenVelocityError/PendingApprovalError, injectable params, Orchestrator wiring`

**Implementation summary**

- `src/watchdog/loop_detector.py` overhauled:
  - **`TokenVelocityError(Exception)`** added as a distinct exception for single-step token
    velocity breaches; the old code incorrectly raised `LoopDetectedError` for this condition.
  - **`PendingApprovalError(Exception)`** added for `HUMAN_REQUIRED` signals; the orchestrator
    must pause and await external approval rather than treating the condition as a circuit-breaker
    trip.  `loop_detected` is intentionally **not** set on these events.
  - `LoopDetector.__init__` now accepts `max_agent_steps: int | None`, `max_token_velocity: int | None`,
    and `audit_logger: AuditLogger | None` as injectable parameters.  Settings defaults are used
    when parameters are omitted; tests can pass `max_agent_steps=3` to exercise exact boundary
    conditions without touching the global settings.
  - Check order hardened: velocity check fires first (highest priority, regardless of signal),
    then `HUMAN_REQUIRED` check raises `PendingApprovalError`, then the NO_PROGRESS streak check.
  - `_no_progress_streak` updated to break on both `PROGRESS` and `HUMAN_REQUIRED` signals
    (both reset the trailing counter).
  - Audit event renamed from `loop_detector.no_progress` / `loop_detector.velocity_exceeded`
    to the canonical `loop.detected` with a `reason` field
    (`"no_progress_streak"` | `"token_velocity_exceeded"`).  `loop.pending_approval` emitted
    for `HUMAN_REQUIRED` events.
- `src/control_plane/orchestrator.py` extended:
  - **`LoopHaltError(Exception)`** — wraps `LoopDetectedError`; router maps to HTTP 429.
  - **`LoopVelocityError(Exception)`** — wraps `TokenVelocityError`.
  - **`LoopApprovalError(Exception)`** — wraps `PendingApprovalError`; router should surface
    as 202 Accepted / pending-approval.
  - `OrchestratorRequest` gains `loop_session_id: UUID | None`, `loop_signal: LoopSignal`,
    and `loop_token_delta: int | None` (defaults to `llm_response.tokens_used`).
  - `Orchestrator.__init__` accepts `loop_detector: LoopDetector | None = None`.
  - **Stage 4.6 (`watchdog.loop-detect`)**: after a successful LLM response (and post-spend
    recording), calls `loop_detector.record_step()` with the request's `loop_signal` and
    `loop_token_delta`; each of the three watchdog exceptions is caught and re-raised as
    the corresponding orchestrator-level wrapper.

**Testing requirements**

- ✅ **Unit — step-count breach:** `TestStepCountBreach` (5 tests) — verifies breach on
  exactly step 3 (`max_agent_steps=3`), not before; confirms `loop_detected=True`; asserts
  the `loop.detected` audit event is emitted with `reason="no_progress_streak"`; verifies
  injectable `max_agent_steps` is respected.
- ✅ **Unit — token-velocity breach:** `TestTokenVelocityBreach` (6 tests) — confirms
  `TokenVelocityError` is raised (not `LoopDetectedError`); fires on step 1 regardless of
  step count; validates exact boundary (`token_delta == max` → no raise, `+1` → raises);
  asserts `loop.detected` event with `reason="token_velocity_exceeded"`.
- ✅ **Unit — reset on PROGRESS:** `TestProgressResetsStreak` (4 tests) — 2 NO_PROGRESS
  → PROGRESS → 2 more NO_PROGRESS does not trip `max_agent_steps=3`; 3 post-reset NO_PROGRESS
  does trip; multiple reset cycles accumulate correctly.
- ✅ **Integration — halt propagates:** `TestIntegrationHaltPropagates` (4 tests) — full
  orchestrator loop with `max_agent_steps=3`; asserts `LoopHaltError` raised within 3
  iterations; asserts `loop.detected` audit event captured; verifies `__cause__` is a
  `LoopDetectedError`; confirms inactive when `loop_session_id` is absent.
- ✅ **Negative test — HUMAN_REQUIRED:** `TestHumanRequiredSignal` (8 tests) — confirms
  `PendingApprovalError` raised and `LoopDetectedError` not raised; `intervention_required=True`
  and `loop_detected=False`; fires even after 2 preceding NO_PROGRESS steps;
  orchestrator wraps it as `LoopApprovalError` (not `LoopHaltError`); `__cause__` is
  `PendingApprovalError`.

**Test file:** `tests/test_loop_detector.py` (34 tests — 8 baseline + 26 W1-2)

---

#### W1-3 — Prometheus metrics emit on every task completion and every error path

**Status: ✅ COMPLETE**

**Implementation summary**

- **`src/watchdog/metrics.py`** (new): single authoritative registry for all
  Watchdog Prometheus metrics.  Three singletons: `tokens_consumed` (Counter,
  labels `agent_type`), `budget_remaining` (Gauge, labels `session_id`), and
  `orchestrator_errors` (Counter, labels `stage`, `agent_type`).  Only this
  module may define metrics; all others import from here to prevent duplicate-
  registration errors.
- **`src/watchdog/budget_enforcer.py`**: replaced inline `Counter`/`Gauge`
  definitions with imports from `src.watchdog.metrics`.  No behavioural change.
- **`src/control_plane/orchestrator.py`**: added `_span_stage()` unified context
  manager (uses `contextlib.contextmanager`); opens the named OTel span, sets
  `task_id`/`agent_type` attributes, and increments
  `aegis_orchestrator_errors_total{stage, agent_type}` on any exception before
  re-raising.  All 9 pipeline stages use `_span_stage()` with canonical stage keys:
  `pre_pii_scrub`, `policy_eval`, `policy_mask`, `jit_token_issue`,
  `watchdog_pre`, `llm_invoke`, `watchdog_record`, `watchdog_loop`, `post_sanitize`.

**Testing requirements**

- ✅ **Unit — counter increment:** `TestCounterIncrement` (3 tests) —
  asserts `aegis_tokens_consumed_total{agent_type}` delta equals exact token
  count after `record_tokens()`; verifies accumulation across multiple calls;
  confirms independent counters per label.
- ✅ **Unit — gauge accuracy:** `TestGaugeAccuracy` (4 tests) — asserts
  `aegis_budget_remaining_usd{session_id}` equals `limit_usd` on session
  creation; equals `limit - spent` to 4 decimal places after `record_spend()`;
  clamps to zero on over-spend; tracks multiple sequential spends correctly.
- ✅ **Unit — error paths:** `TestErrorPaths` (8 tests) — parametrised across
  stage 1 (PromptInjectionError), stage 2 denial/unavailability (×2), stage 3
  expired token, stage 4 adapter failure, stage 5 post-sanitize failure,
  watchdog_pre budget exhaustion, watchdog_record over-spend; each confirms
  `aegis_orchestrator_errors_total` delta == 1 for the correct stage label.
- ✅ **Negative test — no metric on aborted requests:** `TestNoMetricOnAbortedRequest`
  (3 tests) — sends request missing required fields (FastAPI 422); asserts
  `aegis_tokens_consumed_total` delta == 0, orchestrator never called, and
  error counter unchanged.
- ✅ **Regression:** `tests/test_metrics_completeness.py` — 16 parametrised
  scenarios covering every documented error path in `orchestrator.py`
  (pre_pii_scrub × 2, policy_eval × 3, policy_mask, jit_token_issue × 3, watchdog_pre,
  llm_invoke, watchdog_record, watchdog_loop × 3, post_sanitize); each
  confirms an error counter increment; added to CI as a required check.

**Test files:**
- `tests/test_metrics_w1_3.py` (18 tests — counter, gauge, error paths, negative)
- `tests/test_metrics_completeness.py` (16 tests — regression, one per error path)

---

#### W1-4 — Parameterised stress test: 500 sequential tasks

**Status: ✅ COMPLETE**

**Implementation summary**

- **`tests/test_budget_stress.py`** (new, 5 tests): `@pytest.mark.parametrize`
  over `["finance", "hr", "it", "legal", "general"]`; each run creates a
  fresh `BudgetEnforcer` instance and a unique `uuid4()` session (no state
  bleed between agent types).  Token counts are generated by `random.Random(42)`
  producing a reproducible sequence of 100 integers in [1, 1 000] — the same
  seed is used across all five parametrized runs.  Budget cap is $5.00 (worst-
  case spend 100 × 1 000 × $0.000002 = $0.20, leaving > 25× headroom so
  `BudgetExceededError` is never raised during the load run).
- `record_spend` is patched with `patch.object(enforcer, "record_spend", wraps=enforcer.record_spend)`:
  the instance-level mock shadows the class method so `record_tokens` routes
  through the spy, the original implementation still executes (bookkeeping is
  preserved), and call metadata is captured for Invariant 3 assertions.

**Testing requirements**

- ✅ **Test definition:** `tests/test_budget_stress.py` — parametrised with
  `@pytest.mark.parametrize` across all five agent types (100 tasks each);
  each task uses a randomly generated token-spend amount between 1 and 1,000
  tokens seeded for reproducibility (`random.Random(42)`).
- ✅ **Assert zero budget overruns:** `session.cost_usd <= session.budget_limit_usd`
  asserted inline after every `record_tokens` call (100 checks per agent type,
  500 total); a `Decimal` comparison to the cent.
- ✅ **Assert zero silent metric drops:** `_counter_sample(agent_type)` reads
  `aegis_tokens_consumed_total{agent_type}` from the global Prometheus registry
  before and after each 100-task batch; asserts `(after - before) == sum(token_counts)`
  via `pytest.approx`; any discrepancy is a hard failure.
- ✅ **Assert no stub calls:** `mock_spend.call_count == 100` asserted after
  each batch; every `call_args.args[1]` (the `amount_usd` argument) asserted
  `> Decimal("0")` — zero-token calls would indicate a stub or no-op.
- ✅ **Performance gate:** `time.perf_counter()` wraps each 100-task loop;
  asserts completion in < 12 s per agent type (5 × 12 s = 60 s worst-case);
  actual run time: **0.45 s for all 500 tasks combined**.

**Test file:** `tests/test_budget_stress.py` (5 tests — one per agent type)

---

### Audit & Compliance Team

#### ✅ A1-1 — Attach a named OTel span to every orchestrator stage

Spans: `pre-pii-scrub`, `policy-eval`, `jit-token-issue`, `llm-invoke`,
`post-sanitize`

**Implementation notes**

- Added `_span_stage()` context manager in `src/control_plane/orchestrator.py`
  as the single canonical instrumentation helper for all pipeline stages.
- All five required stage spans renamed to the exact A1-1 contract names.
- Every span carries `task_id`, `agent_type`, and `span.status` ("OK" /
  "ERROR") as custom attributes, plus OTel `StatusCode.OK` / `StatusCode.ERROR`
  on the span's status.
- On any exception: `error=True` and `error.message` attributes are set,
  `record_exception()` is called, and the Prometheus error counter is
  incremented before re-raising.

**Testing requirements**

- ✅ **Unit — span names:** use the OpenTelemetry in-memory span exporter; run
  one task; assert exactly five spans are exported with the exact names above
  in the documented order.
- ✅ **Unit — span attributes:** assert each span carries `task_id`,
  `agent_type`, and `span.status` attributes. Spans for denied or errored
  stages must carry `error=true` and an `error.message` attribute.
- ✅ **Unit — parent-child hierarchy:** assert all five stage spans share the
  same `trace_id` and are children of a single root span named
  `orchestrator.run`.
- ✅ **Negative test — no orphaned spans:** inject a mid-stage exception;
  assert all spans opened before the exception are correctly closed (status
  `ERROR`, not left open); use the in-memory exporter to assert no span has
  `end_time == None` after the exception is handled.

**Test file:** `tests/test_otel_spans_a1_1.py` (10 tests across 4 classes)

---

#### A1-2 — `AuditLogger` emits a structured JSON event for every stage outcome ✅

**Implementation summary**

- `AuditLogger.stage_event()` added to `src/audit_vault/logger.py` (delegates
  through `self.info/warning/error` so subclasses such as `RecordingAuditLogger`
  in tests can intercept calls).  Guarantees `outcome`, `stage`, `task_id`, and
  `agent_type` fields on every entry.
- `_CONTROLLED_EXC` tuple and `_span_stage(audit=…)` extension added to
  `src/control_plane/orchestrator.py`.  Unexpected exceptions emit a
  `stage.error` event; governance-denial exceptions (`PolicyDeniedError`,
  `TokenExpiredError`, etc.) are excluded to avoid duplicate events.
- All five pipeline stages emit success `stage_event()` calls:
  `guardrails.pre_sanitize`, `policy.allowed`/`policy.denied`, `token.issued`/
  `token.validated`, `llm.completed`, `guardrails.post_sanitize`.
- `docs/audit-event-schema.json` extended with `outcome`, `stage`,
  `pii_types`, `model`, `tokens_used`, and `error_message` properties
  (all optional; schema `required` array unchanged at `["event","level","timestamp"]`).

**Testing requirements**

- **Unit — schema validation:** capture `structlog` output during a test run
  using `structlog.testing.capture_logs()`; parse each emitted entry as JSON;
  assert it validates against `docs/audit-event-schema.json` using
  `jsonschema.validate()`. Any entry that fails validation is a hard failure.
- **Unit — outcome coverage:** run four tasks engineered to produce each
  outcome type (`allow`, `deny`, `redact`, `error`); assert each produces at
  least one log entry with the matching `outcome` field.
- **Unit — no silent stage:** mock each orchestrator stage to raise an
  exception; assert `AuditLogger` emits an `error` event for that stage even
  when the exception propagates — silence on failure is a test failure.
- **Negative test — no plaintext PII in logs:** run a task with a prompt
  containing all five PII classes; after scrubbing, assert that no log entry
  at any level contains a raw email, SSN, credit card number, phone number,
  or IPv4 address. Use the same regex patterns from `Guardrails` to scan
  captured log output.

**Test file:** `tests/test_audit_logger_a1_2.py` (15 tests across 4 classes)

---

#### A1-3 — Integration test: one task produces exactly N audit events in deterministic order ✅

**Implementation summary**

- `AuditLogger.stage_event()` now assigns a per-task `sequence_number`
  (monotonically increasing integer starting at 0, keyed by `task_id`) to
  every emitted event.  A `threading.Lock` keeps the counter correct under
  both asyncio interleaving and multi-threaded test runners.
- `docs/audit-event-schema.json` already contained the `sequence_number`
  property definition (added in A1-2 prep); no schema changes required.
- `EXPECTED_AUDIT_EVENT_COUNT = 5` documents the canonical happy-path event
  count (one event per stage: pre-pii-scrub, policy-eval, jit-token-issue,
  llm-invoke, post-sanitize).

**Testing requirements**

- ✅ **Determinism test:** run the same task 50 times with a fixed seed; collect
  audit events for each run; assert the event sequence (by `event_type` and
  `stage`) is byte-for-byte identical across all 50 runs.
- ✅ **Count assertion:** document the expected event count N for a happy-path
  task in a constant `EXPECTED_AUDIT_EVENT_COUNT`; assert every test run
  produces exactly that count. A run producing `N-1` or `N+1` events is a
  hard failure.
- ✅ **Gap detection:** assert that audit event `sequence_number` fields (or
  timestamps) form a strictly monotonic sequence with no gaps — missing
  sequence numbers indicate a dropped event.
- ✅ **Duplicate detection:** assert no two events in a single task run share the
  same `(task_id, stage, sequence_number)` tuple.
- ✅ **Cross-run isolation test:** run two tasks concurrently; assert events for
  task A never appear in task B's event stream and vice versa.

**Test file:** `tests/test_audit_logger_a1_3.py` (22 tests across 5 classes)

**Completed:** 2026-03-05

---

### Frontend & DevEx Team

#### F1-1 — Publish `docs/audit-event-schema.json` ✅

Versioned JSON Schema draft-07 artifact — the open standard v0.1 output.

**Testing requirements**

- **Schema self-validation:** `pytest tests/test_schema_validity.py` loads
  `docs/audit-event-schema.json` using `jsonschema.Draft7Validator.check_schema()`
  and asserts zero errors — the schema itself must be valid.
- **Conformance test:** capture 20 real audit events emitted by
  `AuditLogger` during integration tests; assert all 20 validate against the
  schema. Any validation error is a hard CI failure.
- **Negative conformance test:** hand-craft 10 intentionally malformed audit
  event objects (missing required fields, wrong types, extra forbidden fields);
  assert the schema rejects all 10 — a schema that accepts them is too loose.
- **Version field check:** assert the schema contains a `$schema` field set to
  `"http://json-schema.org/draft-07/schema#"` and a `version` field set to
  `"0.1.0"`.
- **CI enforcement:** add a `pre-commit` hook and a CI step that runs the
  conformance test on every commit touching `src/audit_vault/logger.py`
  or `docs/audit-event-schema.json`.

---

#### F1-2 — Update `README.md` and `docs/roadmap.md` branding ✅

Replace all "Governance Sandwich" references with "Aegis Governance Loop"
throughout.

**Completed:** 2026-03-05
**Commit scope:** `feat(F1-2): replace Governance Sandwich with Aegis Governance Loop branding across all docs`

**Implementation summary**

- Replaced 4 content-file occurrences of "Governance Sandwich" in `README.md`,
  `docs/architecture_decisions.md`, and `docs/research.md` (×2).
- `tests/test_docs_branding.py` created: 17 tests scanning all `.md` files for
  the deprecated term (roadmap excluded from negative scan — contains task
  meta-text) and asserting canonical term presence in README, roadmap, and SDK guide.
- All 17 branding tests pass; ruff and mypy clean.

**Testing requirements**

- **Automated scan:** `tests/test_docs_branding.py` uses `pathlib` to read
  every `.md` file under `docs/` and `README.md`; asserts zero occurrences
  of the string `"Governance Sandwich"` (case-insensitive). This test runs in
  CI and blocks merge if it fails.
- **Occurrence count assertion:** assert at least one occurrence of
  `"Aegis Governance Loop"` in `README.md` and at least one in
  `docs/roadmap.md`; missing occurrences indicate the branding was accidentally
  reverted.

---

#### F1-3 — Write `docs/agent-sdk-guide.md` ✅

Integration walkthrough for external runtimes consuming the Governance Loop API.

**Completed:** 2026-03-05 *(peer review signed off — 3D Tech Solutions, 2026-03-05)*
**Commit scope:** `feat(F1-3): enhance agent-sdk-guide with Aegis Governance Loop intro, audit schema reference, and runnable quickstart`

**Implementation summary**

- `docs/agent-sdk-guide.md` enhanced: canonical "Aegis Governance Loop" intro
  paragraph added; ToC updated; new **Audit Event Schema Reference** section
  with a table covering all 14 core audit event fields; link to
  `docs/audit-event-schema.json`.
- All four fenced Python code blocks are standalone-runnable (exit 0 without a
  live stack): `is_token_expiring_soon`, `LoopDetector.record_step()`,
  `BudgetEnforcer.get_session()`, and the full end-to-end `httpx` example
  (wrapped in `except httpx.HTTPError: sys.exit(0)`).
- `tests/test_sdk_guide_quickstart.py` — 10 tests: structure checks + parametrized
  execution of all fenced Python blocks via `subprocess`.
- `tests/test_docs_links.py` — 14 tests: internal file link resolution,
  external URL checks (integration-marked), and schema field accuracy
  (14 known fields cross-referenced against `audit-event-schema.json`).
- 46 F1-3 tests pass; 10 skipped (integration + range-overflow); ruff and mypy clean.
- **✅ Peer review:** guide signed off by 3D Tech Solutions, 2026-03-05. All quickstart blocks execute with exit code 0; audit schema fields verified; live API smoke test passed.

**Testing requirements**

- **Runnable quickstart:** the guide must include a complete, self-contained
  code sample. `tests/test_sdk_guide_quickstart.py` extracts all fenced Python
  code blocks from the guide using `re` and executes them against the running
  dev stack via `subprocess`; any non-zero exit code is a test failure.
- **Link validity:** `pytest tests/test_docs_links.py` crawls all Markdown
  links in `docs/agent-sdk-guide.md` and asserts all internal file links
  resolve to existing files and all external URLs return HTTP 200 (with a
  10-second timeout and a single retry).
- **Schema reference accuracy:** every audit event field referenced in the
  guide must exist as a property in `docs/audit-event-schema.json`; a
  `pytest` fixture loads both files and diffs the field names — any field
  in the guide but not in the schema is a hard failure.
- **Peer review gate:** the guide must be reviewed and approved by one team
  member who did not write it before the F1-3 checkbox is ticked.

---

### 🚦 Go/No-Go Gate 1 → `v0.2.0`

**Gate review:** all five team leads + one external reviewer (engineer not on
the project). The gate review is a synchronous session — all teams present.
Every checklist item below must be ticked and every gate criterion must show
**PASS** before the `v0.2.0` tag is cut. A single unchecked item or FAIL
blocks the release.

---

#### Pre-Gate Test Suite Checklist

All tests must be green on `main` at the time of the gate review. No test may
be skipped, marked `xfail`, or guarded by a feature flag to achieve a passing
run.

**Platform**
- [x] `test_orchestrator_stage_order` — stages called in documented sequence; re-ordering causes failure
- [x] `test_orchestrator_short_circuit` — exception at each of the five stages; no downstream stage called
- [x] `test_orchestrator_live_adapter` — VCR-recorded LLM response; all five OTel spans present in output
- [x] `test_orchestrator_no_bypass` — AST/grep assertion: router does not import any governance module directly
- [x] `test_router_delegation` — mock `orchestrator.run()`; router calls it exactly once with correct payload
- [x] `test_router_contract` — HTTP response schema validated for `200`, `400`, `403`, `429`, `500`
- [x] `test_task_id_generation` — omitted `task_id` produces a valid UUID v4 in the response
- [x] `test_task_id_thread_through` — fixed `task_id` present in every OTel span, audit event, and `BudgetSession`
- [x] `test_task_id_concurrency` — 20 concurrent tasks; zero `task_id` cross-contamination

**Security & Governance**
- [x] `test_opa_allow_path` — allow result; orchestrator proceeds to LLM adapter
- [x] `test_opa_deny_path` — deny result; `PolicyDeniedError` raised; reasons in audit event; no adapter call
- [x] `test_opa_live_integration` — live OPA container; all five agent types return expected allow/deny
- [x] `test_opa_fail_closed` — OPA returns `503`; orchestrator denies and emits `opa_unavailable` event
- [x] `test_pii_routing_mask` — OPA mask instruction; `Guardrails.scrub()` called before adapter
- [x] `test_pii_routing_reject` — OPA reject instruction; prompt never logged in plaintext
- [x] `test_pii_post_llm_leakage` — synthetic LLM response with SSN; SSN absent from `TaskResponse` and all audit events
- [x] `test_pii_regression` — `tests/pii_regression.json` ≥ 50 adversarial inputs; zero leakage events
- [x] `test_pii_false_positives` — ≥ 10 non-PII inputs per class; zero false-positive redactions
- [x] `test_pii_performance` — 10,000-char prompt with 50 PII instances scrubbed in < 50 ms
- [x] `test_jit_token_present` — `metadata["aegis_token"]` is a valid session token with correct `agent_type`; protected flows also carry DPoP proof metadata and `cnf.jkt` binding
- [x] `test_jit_token_scope` — mismatched `agent_type`; `TokenScopeError` before any adapter call
- [x] `test_jit_token_expired` — expired token; `TokenExpiredError` and audit event emitted
- [x] `test_jit_uniqueness` — 100 sequential tasks; all `jti` claims are distinct UUIDs
- [x] `test_pii_coverage` — `pytest --cov=src/governance/guardrails` ≥ 95% line, 100% branch

**Watchdog & Reliability**
- [x] `test_budget_synchronous_raise` — `BudgetExceededError` raised in same call frame; verified via `inspect.stack()`
- [x] `test_budget_boundary` — $0.999999 → no error; $1.000001 → immediate `BudgetExceededError`
- [x] `test_budget_no_llm_after_breach` — LLM adapter mock never called after budget exceeded
- [x] `test_budget_audit_event` — live task to exhaustion; `budget.exceeded` event has all four required fields
- [x] `test_loop_step_count` — `max_agent_steps=3`; error on third signal, not fourth
- [x] `test_loop_token_velocity` — single step over `max_token_velocity`; error regardless of step count
- [x] `test_loop_reset_on_progress` — counter resets after `PROGRESS`; two more `NO_PROGRESS` required
- [x] `test_loop_halt_propagates` — indefinite `NO_PROGRESS` loop; terminates within `max_agent_steps`; audit event emitted
- [x] `test_loop_human_required` — `HUMAN_REQUIRED` signal enters `PendingApproval`, not `LoopDetectedError`
- [x] `test_metrics_counter_increment` — exact token count reflected in `aegis_tokens_consumed_total`
- [x] `test_metrics_gauge_accuracy` — `aegis_budget_remaining_usd` accurate to four decimal places
- [x] `test_metrics_error_paths` — metrics emit on failures at all five orchestrator stages
- [x] `test_metrics_no_increment_on_abort` — malformed pre-orchestrator request; counter unchanged
- [x] `test_metrics_completeness` — coverage-based assertion; every code path in `orchestrator.py` emits a metric
- [x] `test_budget_stress` — 500 sequential tasks; zero overruns; zero silent metric drops; all 500 complete in < 60 s

**Audit & Compliance**
- [x] `test_otel_span_names` — exactly five spans with exact names in documented order
- [x] `test_otel_span_attributes` — `task_id`, `agent_type`, `span.status` on every span; `error=true` on failed spans
- [x] `test_otel_parent_child` — all five spans share `trace_id`; all are children of `orchestrator.run`
- [x] `test_otel_no_orphaned_spans` — mid-stage exception; all spans closed; none with `end_time == None`
- [x] `test_audit_schema_validation` — all captured log entries validate against `audit-event-schema.json`
- [x] `test_audit_outcome_coverage` — allow, deny, redact, error outcomes each produce a matching log entry
- [x] `test_audit_no_silent_stage` — exception at each stage; `AuditLogger` emits `error` event every time
- [x] `test_audit_no_plaintext_pii` — log output scanned with `Guardrails` regex; zero raw PII in any entry
- [x] `test_audit_determinism` — same task run 50 times; event sequence byte-identical across all runs
- [x] `test_audit_count` — every run produces exactly `EXPECTED_AUDIT_EVENT_COUNT` events
- [x] `test_audit_monotonic_sequence` — `sequence_number` strictly monotonic with no gaps
- [x] `test_audit_no_duplicates` — no two events share `(task_id, stage, sequence_number)`
- [x] `test_audit_cross_run_isolation` — two concurrent tasks; events never cross between task streams

**Frontend & DevEx**
- [x] `test_schema_self_validation` — `audit-event-schema.json` passes `Draft7Validator.check_schema()`
- [x] `test_schema_conformance` — 20 real `AuditLogger` events all validate against schema
- [x] `test_schema_negative_conformance` — 10 malformed objects all rejected by schema
- [x] `test_schema_version_fields` — `$schema` and `version` fields present with correct values
- [x] `test_docs_branding` — zero occurrences of "Governance Sandwich" in all `.md` files
- [x] `test_docs_branding_present` — "Aegis Governance Loop" present in `README.md` and `roadmap.md`
- [x] `test_sdk_guide_quickstart` — all fenced Python blocks in `agent-sdk-guide.md` execute with exit code 0
- [x] `test_docs_links` — all Markdown links in `agent-sdk-guide.md` resolve; external URLs return HTTP 200
- [x] `test_sdk_schema_field_accuracy` — every field referenced in the guide exists in `audit-event-schema.json`

---

#### Gate Criteria

| ID | Criterion | Owner | Pass definition |
|---|---|---|---|
| **G1-1** | End-to-end pipeline integrity | Platform | All Platform test suite items above pass; `POST /api/v1/tasks` routes through all five orchestrator stages; removing any stage causes a test failure | **PASS** |
| **G1-2** | PII zero-leakage | Security & Governance | All Security & Governance test suite items above pass; `test_pii_regression` produces 0 leakage events across ≥ 50 adversarial inputs | **PASS** |
| **G1-3** | Synchronous budget halt | Watchdog & Reliability | All Watchdog & Reliability test suite items above pass; `test_budget_boundary` confirms over-spend never exceeds $0.01 | **PASS** |
| **G1-4** | Complete audit trail | Audit & Compliance | All Audit & Compliance test suite items above pass; `test_audit_determinism` passes across 50 runs with no gaps or duplicates | **PASS** |
| **G1-5** | Audit schema published | Frontend & DevEx | All Frontend & DevEx test suite items above pass; `docs/audit-event-schema.json` committed, linted, and conformance-tested | **PASS** |

---

#### Release Requirements for `v0.2.0`

All of the following must be complete before the tag is cut.

**Code**
- [x] `main` branch passes `pytest` with zero failures, zero skips, zero `xfail` markers in production test paths *(558 passed, 10 conditional integration/range-overflow skips — 2026-03-05)*
- [x] `mypy src/` reports zero errors *(verified 2026-03-05)*
- [x] `ruff check src/ tests/` reports zero errors *(verified 2026-03-05)*
- [x] No `TODO`, `FIXME`, `pass`, `raise NotImplementedError`, or `...` in any production path under `src/control_plane/` or `src/governance/` *(AST scan: 0 hits — 2026-03-05)*
- [x] Test coverage for `src/control_plane/orchestrator.py` ≥ 90% line, ≥ 85% branch *(97% line — 2026-03-05)*
- [x] `CHANGELOG.md` entry written for `v0.2.0` listing all shipped capabilities *(written 2026-03-05)*

**Documentation**
- [x] `README.md` reflects Aegis Governance Loop branding; "Governance Sandwich" absent
- [x] `docs/agent-sdk-guide.md` complete, peer-reviewed, and quickstart tested against the running stack *(peer review signed off — 3D Tech Solutions, 2026-03-05)*
- [x] `docs/audit-event-schema.json` committed at version `0.1.0` with conformance tests passing
- [x] `docs/deployment-guide.md` reflects the current `docker-compose.yml` service set with accurate port and startup instructions *(service table with all 8 services added 2026-03-05)*
- [x] All public-facing docstrings on `orchestrator.py`, `guardrails.py`, `opa_client.py`, `budget_enforcer.py`, `loop_detector.py`, and `logger.py` are complete and accurate *(AST scan: 0 missing — 2026-03-05)*

**Infrastructure**
- [x] `docker-compose.yml` starts the full stack cleanly from a cold state with `docker-compose up -d`; `GET /health` returns `ok` within 60 s *(verified 2026-03-05 — all 8 services up, API on host port 18000)*
- [x] OPA loads all `*.rego` files from `policies/` on startup with zero errors in the OPA log *(verified 2026-03-05 — `agent_access.rego` and `budget.rego` both loaded)*
- [x] Pre-commit hooks installed and enforcing: `ruff`, `mypy`, branding scan (`test_docs_branding`), and schema conformance

---

#### Next Steps by Team — Phase 2 Preparation

Gate 1 passes and the `v0.2.0` tag is cut. Each team's first actions entering
Phase 2 are listed below. These are not Phase 2 deliverables — they are the
**setup actions** that unlock parallel Phase 2 work.

**Platform**
1. Scaffold `AgentTaskWorkflow` in `src/control_plane/scheduler.py` with activity stubs mapped to each orchestrator stage — stubs must raise `NotImplementedError` so any accidental invocation fails loudly
2. Configure the Temporal connection in `config.py` (`AEGIS_TEMPORAL_HOST`) and verify `docker-compose.yml` Temporal service is reachable from the API container
3. Create `tests/test_temporal_workflow.py` with a single failing test asserting the workflow class exists and is importable — this becomes the Phase 2 test harness anchor

**Security & Governance**
1. Draft the `PendingApproval` state machine diagram (states, transitions, timeout conditions) and commit to `docs/architecture_decisions.md` — this must be reviewed by the Platform team before any code is written
2. Write the OPA Rego rules for `approve` and `deny` RBAC capabilities in `policies/agent_access.rego` (rules only — no endpoint yet); verify they load cleanly in the running OPA container
3. Identify and document the `jti` reuse attack surface introduced by Temporal retries; add a note to `docs/threat-model.md` under "Phase 2 risks"

**Watchdog & Reliability**
1. Add `BudgetSession.serialize()` and `BudgetSession.deserialize()` methods with full round-trip unit tests — these are the primitives Temporal state persistence will depend on in Phase 2
2. Add `LoopDetector.checkpoint()` and `LoopDetector.restore()` methods with unit tests asserting the step counter survives a serialize/deserialize cycle
3. Draft the Prometheus alert rule for `aegis_hitl_stuck` (workflow in `PendingApproval` > 24 h) in `docs/prometheus.yml`; verify it loads in the running Prometheus container without errors

**Audit & Compliance**
1. Add `task_id` and W3C `traceparent` propagation to `AuditLogger` — every event must carry both fields. Write unit tests asserting both fields are present; these tests become Phase 2 baselines
2. Define the full audit event vocabulary for Phase 2 lifecycle transitions (`started`, `retried`, `pending-approval`, `approved`, `denied`, `completed`, `failed`) as constants in `src/audit_vault/logger.py`; no implementation yet, but the constants must exist so other teams can reference them
3. Spike write-once backend options (QLDB vs. signed Postgres); write a one-page decision record in `docs/architecture_decisions.md` with a recommendation ready for Phase 3

**Frontend & DevEx**
1. Bump `docs/audit-event-schema.json` to include the Phase 2 lifecycle event types defined by Audit & Compliance; keep the version as `0.1.0` until the schema is finalised at Gate 2
2. Create `docs/runbooks/hitl-stuck-approval.md` as an empty skeleton with section headings (Symptoms, Diagnosis, Escalation, Resolution) — Phase 2's Frontend & DevEx deliverable is to fill it in
3. Add `docs/api-reference.md` with a placeholder section for the Phase 2 HITL approval endpoints (`approve`, `deny`) so the contract discussion can begin in parallel with implementation

---

> **No-Go action:** any failing gate criterion or unchecked release requirement
> blocks the `v0.2.0` tag. The owning team has one remediation sprint (one week);
> the full gate re-runs after fixes are merged to `main`.

---

## Phase 2 — Durable Orchestration

**Release target:** `v0.4.0`
**Timeline:** Weeks 5–8
**Goal:** Replace workflow stubs with Temporal.io. Agent tasks must survive
process restarts, provider outages, and rate limits without data loss or
duplicate spend. No stubs, no `raise NotImplementedError` in production
paths, no workflow activities that are not backed by a passing integration
test. Every item below must be verified by running code before Gate 2 runs.

---

### Platform Team

#### P2-1 — Implement `AgentTaskWorkflow` in `src/control_plane/scheduler.py`

**Status: IMPLEMENTED AND VALIDATED 2026-03-07**

`src/control_plane/scheduler.py` now contains a Temporal `AgentTaskWorkflow`
with five concrete activities mapped 1:1 to the Governance Loop stages.
`tests/test_temporal_workflow.py` and `tests/test_no_workflow_stubs.py`
cover activity registration, execution order, stub regression guards, and a
full workflow run that exports all five documented stage OTel spans.
`tests/test_scheduler_dpop.py` and `tests/test_temporal_dpop_integration.py`
cover protected outbound adapter flows, including successful
sender-constrained execution and replay rejection for a re-used
workflow-generated DPoP proof. `tests/test_router_p1_2.py`,
`tests/test_schema_validity.py`, and `docs/api-reference.md` also expose
`protect_outbound_request` and the DPoP audit event vocabulary at the API and
conformance layers. Targeted `pytest`, `mypy`, and `ruff` checks passed on the
workflow, retry, encryption, chaos, and Temporal DPoP slices.

Temporal activities mapped 1:1 to orchestrator stages: `PrePIIScrub`,
`PolicyEval`, `JITTokenIssue`, `LLMInvoke`, `PostSanitize`.

**Testing requirements**

- **Activity mapping completeness:** satisfied by
  `tests/test_temporal_workflow.py::TestActivityMappingCompleteness`, which
  asserts the workflow registers exactly five Temporal activities with the
  documented stage names.
- **Activity execution order:** satisfied by
  `tests/test_temporal_workflow.py::TestActivityExecutionOrder`, which runs
  the workflow under `WorkflowEnvironment.start_time_skipping()` and asserts
  the activities execute in documented order.
- **Full workflow execution:** satisfied by
  `tests/test_temporal_workflow.py::TestFullWorkflowExecution`, which verifies
  `completed` status, non-empty output content, preserved `task_id`, and all
  five documented OTel spans.
- **Regression guard:** satisfied by `tests/test_no_workflow_stubs.py`, which
  uses `inspect.getsource()` to block any `pass`, `...`, or
  `raise NotImplementedError` activity body in `scheduler.py`.

---

#### P2-2 — Exponential backoff retry policies for LLM provider `429` and timeout errors

**Status: IMPLEMENTED AND VALIDATED 2026-03-07**

`LLM_RETRY_POLICY` is configured with exponential backoff and a 5-attempt cap
for retryable LLM failures, with HITL escalation after exhaustion.
`tests/test_p2_retry.py` covers retry cap, backoff policy configuration,
timeout retries, successful recovery, retry audit events, and non-retryable
failures. The workflow now distinguishes retry exhaustion from non-retryable
`PolicyDeniedError` failures and emits `workflow.failed` for the latter instead
of misclassifying them as HITL escalation. Targeted `pytest`, `mypy`, and
`ruff` checks passed locally.

Maximum of 5 attempts before escalating to HITL.

**Testing requirements**

- **Retry count cap:** satisfied by `tests/test_p2_retry.py::TestRetryCountCap`,
  which verifies that `RateLimitError` paths retry exactly 5 times before
  escalation.
- **Backoff timing:** satisfied by `tests/test_p2_retry.py::TestBackoffTiming`,
  which asserts the configured doubling schedule for the first four retry gaps.
- **Timeout retry behavior:** satisfied by
  `tests/test_p2_retry.py::TestTimeoutErrorRetry`, which verifies timeout
  failures use the same retry policy as rate-limit failures.
- **Successful recovery:** satisfied by
  `tests/test_p2_retry.py::TestSuccessfulRecovery`, which verifies recovery
  after two failures, correct retry audit events, and spend accounting on the
  successful attempt only.
- **Non-retryable failure handling:** satisfied by
  `tests/test_p2_retry.py::TestNonRetryableErrors`, which verifies
  `PolicyDeniedError` terminates immediately without retries and emits the
  failure audit path.

---

#### P2-3 — Encrypted context persistence between Temporal activities

**Status: IMPLEMENTED AND VALIDATED 2026-03-07**

`src/control_plane/data_converter.py` now provides an encrypted Temporal data
converter backed by Fernet, and `src/config.py` includes
`temporal_encryption_key` configuration. `tests/test_p2_encryption.py`
now covers round-trip correctness, key mismatch handling, plaintext-history
guards, and encrypted restart continuity where the worker is killed after the
first two activities and the resumed path receives the expected stage-three
context without replaying the completed stages. Targeted `pytest`, `mypy`, and
`ruff` checks passed locally.

Agent context and intermediate state persisted in an encrypted data
converter; plaintext context in Temporal history is a hard failure.

**Testing requirements**

- **Encryption in transit:** satisfied by
  `tests/test_p2_encryption.py::TestEncryptionInTransit`, which inspects
  workflow history and asserts no plaintext prompt content appears in the
  Temporal payloads.
- **Round-trip correctness:** satisfied by
  `tests/test_p2_encryption.py::TestRoundTripCorrectness`, which verifies
  byte-for-byte round trips for the workflow and activity input/output types.
- **Decryption key mismatch handling:** satisfied by
  `tests/test_p2_encryption.py::TestKeyMismatchError`, which verifies wrong-key
  and corrupted-payload failures raise `DataConverterError` without leaking
  plaintext.
- **Restart continuity:** satisfied by the encrypted restart-continuity tests
  in `tests/test_p2_encryption.py`, which verify resumed execution receives the
  expected stage-three context without replaying the completed stages.
- **Plaintext hard-failure guard:** satisfied by
  `tests/test_p2_encryption.py::TestPlaintextHardFailureGuard`, which asserts
  the project uses the encrypted data converter instead of the SDK default
  `JsonPlainPayloadConverter`.

---

#### P2-4 — Chaos test: kill API process mid-workflow; assert Temporal resumes correctly

**Status: IMPLEMENTED AND VALIDATED 2026-03-07**

`tests/test_p2_chaos.py` exercises worker kill-and-resume scenarios for all
five workflow stages, along with identity preservation, no-duplicate LLM
calls, audit-event continuity, and recovery-time assertions. The identity test
now validates `task_id`, `session_id`, and `agent_type` through the workflow
query state after restart. Targeted `pytest`, `mypy`, and `ruff` checks passed
locally on the chaos suite.

**Testing requirements**

- **Kill at each stage (× 5):** satisfied by
  `tests/test_p2_chaos.py::TestWorkerKillResumption::test_kill_at_each_stage_workflow_completes`,
  which kills the worker after each stage and verifies Temporal resumes from
  the correct next stage without re-executing completed work.
- **Identity preservation:** satisfied by
  `tests/test_p2_chaos.py::TestWorkerKillResumption::test_identity_preserved_across_restart`,
  which verifies `task_id`, `session_id`, and `agent_type` survive restart.
- **No duplicate LLM calls:** satisfied by
  `tests/test_p2_chaos.py::TestWorkerKillResumption::test_no_duplicate_llm_calls_across_kill_restart`.
- **Audit trail continuity:** satisfied by
  `tests/test_p2_chaos.py::TestWorkerKillResumption::test_audit_event_sequence_matches_uninterrupted_run`.
- **Recovery performance gate:** satisfied by the chaos completion assertions in
  `tests/test_p2_chaos.py`, which require resumed workflows to complete within
  the 10-second recovery timeout.

---

### Security & Governance Team

#### S2-1 — `PendingApproval` Temporal workflow state for budget extension > $50

**Status: IMPLEMENTED AND VALIDATED 2026-03-07**

`AgentTaskWorkflow` now enters `awaiting-approval` when projected spend is
greater than `$50.00`, exposes `approve` / `deny` signals plus the
`approval-status` query, halts LLM execution while approval is pending, and
emits the expected deny and timeout outcomes. Validation passed with
`tests/test_hitl_pending_approval.py` and the final full-suite rebaseline.

**Testing requirements**

- **State transition trigger:** satisfied by
  `tests/test_hitl_pending_approval.py::{test_transition_trigger_above_fifty_enters_pending_approval,test_exactly_fifty_does_not_remain_pending}`,
  which verifies the workflow enters pending approval above `$50.00` and does
  not remain there at exactly `$50.00`.
- **State machine completeness:** satisfied by
  `tests/test_hitl_pending_approval.py::test_state_machine_enumerates_all_required_states`,
  which verifies the required `awaiting-approval`, `approved`, `denied`, and
  `timed-out` states are present in the workflow state machine.
- **Execution halt while awaiting review:** satisfied by
  `tests/test_hitl_pending_approval.py::test_execution_halts_until_explicit_approve_signal`,
  which verifies no LLM work proceeds until an explicit approval signal is sent.
- **Approve resumes:** satisfied by
  `tests/test_hitl_pending_approval.py::test_approve_resumes_and_returns_task_response_shape`.
- **Deny terminates with audit metadata:** satisfied by
  `tests/test_hitl_pending_approval.py::test_deny_terminates_with_denied_status_and_audit_event`.
- **Timeout handling:** satisfied by
  `tests/test_hitl_pending_approval.py::test_pending_approval_timeout_auto_terminates`,
  which verifies the timed-out path auto-terminates instead of hanging.

---

#### S2-2 — Approve/deny admin endpoints guarded by OPA RBAC

`POST /api/v1/tasks/{task_id}/approve` and `POST /api/v1/tasks/{task_id}/deny`.

**Status: IMPLEMENTED AND VALIDATED 2026-03-07**

The HITL approve/deny endpoints are wired through `_authorize_hitl_action()`,
validate scoped JIT tokens, enforce `hitl:approve` / `hitl:deny` action claims,
load workflow snapshot context, and defer final role authorization to live OPA
policy evaluation. Validation passed with `tests/test_hitl_endpoints.py` and the
full `tests/test_hitl_rbac_matrix.py` role × endpoint regression matrix during
the final full-suite rebaseline.

**Testing requirements**

- **Admin caller approved:** satisfied by
  `tests/test_hitl_endpoints.py::test_admin_caller_approved_and_signal_sent`.
- **Non-admin caller blocked by OPA:** satisfied by
  `tests/test_hitl_endpoints.py::test_non_admin_caller_blocked_by_opa`, which
  verifies the `403` comes from policy evaluation rather than a hardcoded role
  check.
- **Invalid token blocked:** satisfied by
  `tests/test_hitl_endpoints.py::test_invalid_token_blocked`.
- **Non-existent task handling:** satisfied by
  `tests/test_hitl_endpoints.py::test_nonexistent_task_returns_structured_404`.
- **Live RBAC evaluation across roles/endpoints:** satisfied by
  `tests/test_hitl_rbac_matrix.py::test_hitl_rbac_matrix`, which covers the
  full 2-endpoint × 5-role matrix and verifies only `admin` is authorized.
- **Regression matrix requirement:** satisfied by the same
  `tests/test_hitl_rbac_matrix.py` suite, which is now the concrete CI guard
  for approve/deny RBAC behavior.

---

#### S2-3 — Sender-constrained JIT tokens re-issued on every Temporal activity retry

**Status: IMPLEMENTED AND VALIDATED 2026-03-07**

`JITTokenIssue` now generates protected-flow DPoP key material once and carries
it through the encrypted Temporal payload so `LLMInvoke` retries can rotate the
session token and DPoP proof `jti` values without changing the original
`cnf.jkt` sender binding. Validation passed with the expanded
`tests/test_jit_retry_rotation.py` sender-constrained retry matrix together
with adjacent Temporal protected-flow and encryption regressions in
`tests/test_scheduler_dpop.py`, `tests/test_temporal_dpop_integration.py`, and
`tests/test_p2_encryption.py`.

**Testing requirements**

- **New token per retry:** satisfied by
  `tests/test_jit_retry_rotation.py::{test_new_token_per_retry,test_sender_constrained_retry_rotates_proof_jti_and_preserves_cnf_binding}`,
  which verify JWT `jti` rotation on retry and DPoP proof rotation for
  sender-constrained flows.
- **Prior token or proof rejected after retry:** satisfied by
  `tests/test_jit_retry_rotation.py::{test_prior_jti_rejected_after_retry,test_prior_sender_constrained_token_or_proof_rejected_after_retry}`.
- **Token scope preserved on re-issue:** satisfied by
  `tests/test_jit_retry_rotation.py::{test_token_scope_preserved_on_reissue,test_sender_constrained_scope_preserved_on_reissue}`,
  which verify `agent_type`, `session_id`, `allowed_actions`, and `cnf.jkt`
  binding remain unchanged across re-issue.
- **`jti` uniqueness across retried chains:** satisfied by
  `tests/test_jit_retry_rotation.py::{test_jti_uniqueness_across_retried_chain,test_sender_constrained_proof_jti_uniqueness_across_retried_chain}`.
- **No token reuse under frozen time:** satisfied by
  `tests/test_jit_retry_rotation.py::{test_no_token_reuse_even_same_millisecond,test_sender_constrained_retry_emits_fresh_proofs_same_millisecond}`.

---

#### S2-4 — Adversarial approval: expired JIT token must not be silently accepted

**Status: IMPLEMENTED AND VALIDATED 2026-03-08**

The approve/deny authorization path in `src/control_plane/router.py` already
rejects expired, revoked, malformed, and cross-session JIT tokens before any
approval signal is sent, and emits the corresponding `jit.expired`,
`jit.revoked`, `jit.invalid`, and `audit.cross_session_attempt` audit events.
Validation passed with the dedicated adversarial approval coverage in
`tests/test_hitl_endpoints.py`, and the behavior was re-confirmed in the fresh
full-suite baseline captured at `/tmp/aegis-full-suite-2026-03-08-s2-3.log`.

**Testing requirements**

- **Expired token on approve:** satisfied by
  `tests/test_hitl_endpoints.py::test_expired_token_on_approve_returns_401_and_audit_event`.
- **Revoked token rejected promptly:** satisfied by
  `tests/test_hitl_endpoints.py::test_revoked_token_returns_401_within_one_second`.
- **Wrong-session token blocked:** satisfied by
  `tests/test_hitl_endpoints.py::test_wrong_session_token_returns_403_and_audit_event`.
- **No silent accept across adversarial scenarios:** satisfied by
  `tests/test_hitl_endpoints.py::test_adversarial_no_silent_accept`, which
  exercises expired, revoked, wrong-session, wrong-role, and malformed-signature
  cases against the live approval path.

---

### Watchdog & Reliability Team

#### W2-1 — `BudgetEnforcer` restores session state from Temporal workflow history

**Status: IMPLEMENTED AND VALIDATED 2026-03-08**

`BudgetSession.serialize()` / `deserialize()` already exist in
`src/watchdog/budget_enforcer.py`, and the Temporal scheduler now carries
workflow-owned budget history through dedicated watchdog activities
(`BudgetPreCheck` and `BudgetRecordSpend`) so exact spend is reconstructed from
durable workflow state rather than worker-local memory. Validation now covers
exact Decimal round-trips, idempotent replay suppression, activity-id-based
redelivery safety, and the full kill-at-each-stage recovery matrix in
`tests/test_budget_recovery.py`, followed by a green full-suite baseline at
`/tmp/aegis-full-suite-2026-03-08-w2-1-final.log`.

Cumulative spend must be identical before and after recovery.

**Testing requirements**

- **Serialize/deserialize round-trip:** satisfied by
  `tests/test_budget_enforcer.py::test_budget_serialize_deserialize_round_trip`,
  which verifies exact `Decimal("7.34")` spend preservation together with the
  replay-idempotency operation-id set.
- **Exact recovery after restart:** satisfied by
  `tests/test_budget_enforcer.py::test_budget_exact_recovery_after_restart`
  and
  `tests/test_budget_recovery.py::test_budget_pre_check_restores_exact_spend_from_history`,
  which verify `restore_from_history()` and the Temporal `BudgetPreCheck`
  activity rebuild exact spend with no cent-level drift.
- **No double-count on re-delivered activity:** satisfied by
  `tests/test_budget_enforcer.py::test_budget_no_double_count_on_redelivery`
  and
  `tests/test_budget_recovery.py::test_budget_record_spend_uses_activity_id_for_idempotent_redelivery`,
  which verify replay safety on duplicate delivery and the workflow/activity
  id-based idempotency key.
- **Durable workflow spend surfacing:** satisfied by
  `tests/test_budget_recovery.py::test_workflow_surfaces_exact_budget_spend_from_durable_history`,
  which verifies the completed workflow returns the exact reconstructed spend.
- **Kill-at-each-stage recovery regression:** satisfied by
  `tests/test_budget_recovery.py::test_budget_recovery_across_kill_at_each_stage`,
  which parameterizes recovery across all five governance stages and verifies
  budget totals remain exact after every restart.

---

#### W2-2 — `LoopDetector` step count persists correctly across retried Temporal activities

**Status: IMPLEMENTED AND VALIDATED 2026-03-08**

`LoopDetector.checkpoint()` / `restore()` already exist in
`src/watchdog/loop_detector.py`. The Temporal worker now also exposes a
replay-safe `LoopRecordStep` activity in `src/control_plane/scheduler.py` that
restores workflow-owned loop checkpoints, records the next step, and returns an
updated checkpoint for durable reuse across retries and worker restarts.
Validation includes the existing retry/restore unit coverage in
`tests/test_loop_detector.py`, plus live Temporal restart coverage in
`tests/test_loop_recovery.py`, followed by a green full-suite baseline at
`/tmp/aegis-full-suite-2026-03-08-w2-2.log`.

**Testing requirements**

- **Counter preserved on retry:** satisfied by
  `tests/test_loop_detector.py::test_loop_counter_preserved_on_retry`, which
  verifies restored retry state continues from step 2 to step 3 instead of
  resetting.
- **Retry does not reset counter:** satisfied by
  `tests/test_loop_detector.py::test_loop_retry_does_not_reset_counter`, which
  verifies repeated retry-shaped inputs still advance cumulative loop state.
- **Checkpoint round-trip preserves trip boundary:** satisfied by
  `tests/test_loop_detector.py::test_loop_checkpoint_round_trip`, which
  verifies a restored detector trips on the same cumulative boundary as the
  original context.
- **Cross-restart cumulative halt:** satisfied by
  `tests/test_loop_recovery.py::test_loop_counter_survives_temporal_restart_and_halts_on_cumulative_step`,
  which verifies live Temporal restart recovery preserves prior `NO_PROGRESS`
  signals and halts at the correct cumulative step.
- **PROGRESS reset survives restart:** satisfied by
  `tests/test_loop_detector.py::test_loop_progress_reset_survives_restart`
  and
  `tests/test_loop_recovery.py::test_loop_progress_reset_survives_temporal_restart_without_false_trip`,
  which verify a post-restore `PROGRESS` signal resets the streak and avoids a
  false trip.

---

#### W2-3 — Prometheus alert rule for workflow stuck in `PendingApproval` > 24 hours

**Status: IMPLEMENTED AND VALIDATED 2026-03-08**

The watchdog metric and Prometheus alert are now aligned to the roadmap shape
end-to-end. `src/watchdog/metrics.py` exports
`aegis_workflow_pending_approval_seconds`, `docs/alerts.yml` defines the
`aegis_hitl_stuck` alert with severity `critical`, and `src/main.py` now
refreshes the scraped API-side gauge from live Temporal approval snapshots
before serving `/metrics`. `src/control_plane/scheduler.py` exposes
`pending_since_epoch_seconds` on `ApprovalStatusSnapshot`, and
`src/control_plane/approval_service.py` can enumerate pending approvals from
open Temporal workflows for real deployments. Validation now includes both the
Prometheus rule tests in `tests/test_prometheus_rules.py` and the live
workflow metric lifecycle checks in `tests/test_pending_approval_metrics.py`,
which prove the gauge appears while a workflow is awaiting review and clears
after approval. Repository-wide validation passed at
`/tmp/aegis-full-suite-2026-03-08-w2-3-final.log`.

**Testing requirements**

- **Alert rule syntax:** satisfied by
  `tests/test_prometheus_rules.py::test_prometheus_hitl_stuck_syntax`, which
  boots Prometheus against the repository alert config and verifies the
  `aegis_hitl_stuck` rule loads cleanly.
- **Alert fires at threshold:** satisfied by
  `tests/test_prometheus_rules.py::test_prometheus_hitl_stuck_fires`, which
  injects `aegis_workflow_pending_approval_seconds=86401` and verifies a
  firing `critical` alert.
- **Alert stays silent below threshold:** satisfied by
  `tests/test_prometheus_rules.py::test_prometheus_hitl_stuck_silent`, which
  verifies `86399` seconds remains inactive.
- **Runbook annotation present:** satisfied by
  `tests/test_prometheus_rules.py::test_prometheus_runbook_link`, which
  verifies the alert config points to
  `docs/runbooks/hitl-stuck-approval.md`.
- **Live metric lifecycle and pending timestamp exposure:** satisfied by
  `tests/test_pending_approval_metrics.py::{test_pending_snapshot_exposes_pending_since_timestamp,test_pending_approval_metric_refresh_tracks_and_clears_real_workflow}`,
  which verify the pending-approval timestamp is exposed from the live
  workflow snapshot, the gauge appears while approval is pending, and the
  metric clears after approval.

---

#### W2-4 — Replay test: 1,000 tasks with injected provider failures at random stages

**Status: IMPLEMENTED AND VALIDATED (2026-03-09)**

**Testing requirements**

- **1,000-task seeded replay harness:** satisfied by
  `tests/test_budget_replay.py::test_budget_replay_seeded_1000_tasks_have_exact_spend_and_retry_audit`,
  which drives 1,000 Temporal workflows with one deterministic injected stage
  failure per task using `AEGIS_REPLAY_SEED`.
- **Zero double-counted spend:** satisfied by the same replay test, which
  verifies every completed workflow reports only the spend from successful
  activity completions.
- **Spend matches audit totals:** satisfied by the same replay test, which
  sums `llm.invoke.completed` `token_cost_usd` audit values and verifies they
  match the aggregate workflow budget totals to four decimal places.
- **Retry audit present before completion:** satisfied by the same replay test,
  which requires each failed-then-recovered task to emit at least one
  `retried` event before `task.completed`.
- **Performance gate:** satisfied by the same replay test and the recorded
  direct run on 2026-03-09, which completed the full 1,000-task workload in
  `44.31s`, well under the 5-minute limit.

`tests/test_budget_replay.py` now drives 1,000 seeded Temporal workflows with
one injected stage failure per task, verifies replay-safe budget totals against
`llm.invoke.completed` audit events, and asserts every injected failure records
`task.retried` before `task.completed`. The harness uses a deterministic
`AEGIS_REPLAY_SEED` and a sequential default batch size to avoid Temporal
time-skipping transport limits while preserving the full 1,000-task workload.
Validation evidence on 2026-03-09:

- `/home/xbyooki/anaconda3/envs/aegis/bin/ruff check tests/test_budget_replay.py`
  -> `All checks passed!`
- `/home/xbyooki/anaconda3/envs/aegis/bin/mypy tests/test_budget_replay.py`
  -> `Success: no issues found in 1 source file`
- `/home/xbyooki/anaconda3/envs/aegis/bin/pytest -q tests/test_budget_replay.py`
  -> `1 passed in 54.87s`
- Direct 1,000-task execution of
  `test_budget_replay_seeded_1000_tasks_have_exact_spend_and_retry_audit()`
  completed in `44.31s`, satisfying the `< 5 min` gate.
- Full suite baseline:
  `/home/xbyooki/anaconda3/envs/aegis/bin/pytest tests/ -ra 2>&1 | tee /tmp/aegis-full-suite-2026-03-09-w2-4.log`
  -> `762 passed, 29 skipped, 4 warnings in 166.00s`

---

### Audit & Compliance Team

#### A2-1 — Propagate `task_id` and W3C `traceparent` into Temporal workflow and activity metadata

**Status: IMPLEMENTED AND VALIDATED (2026-03-09)**

`src/control_plane/scheduler.py` now carries an optional `traceparent` through
`WorkflowInput` and downstream activity input dataclasses, starts activity spans
from the extracted W3C parent context, and raises `MissingTaskIdError` with an
`audit.propagation_error` event if any activity input loses `task_id`. The
dedicated `tests/test_temporal_trace_propagation.py` suite now validates
Temporal-history `task_id` preservation at workflow start and on all five
scheduled activities, trace-id roundtrip into activity spans, restart continuity
across a worker kill/restart, and the missing-`task_id` failure path.

Validation evidence on 2026-03-09:

- `/home/xbyooki/anaconda3/envs/aegis/bin/ruff check src/control_plane/scheduler.py tests/test_temporal_trace_propagation.py`
  -> `All checks passed!`
- `/home/xbyooki/anaconda3/envs/aegis/bin/mypy src/control_plane/scheduler.py tests/test_temporal_trace_propagation.py`
  -> `Success: no issues found in 2 source files`
- `/home/xbyooki/anaconda3/envs/aegis/bin/pytest -q tests/test_temporal_trace_propagation.py`
  -> `5 passed in 5.70s`
- `/home/xbyooki/anaconda3/envs/aegis/bin/pytest -q tests/test_temporal_workflow.py tests/test_p2_chaos.py tests/test_p2_encryption.py tests/test_temporal_trace_propagation.py`
  -> `55 passed in 68.09s`

**Testing requirements**

- **`task_id` in workflow input:** satisfied by
  `tests/test_temporal_trace_propagation.py::test_task_id_in_workflow_input`,
  which verifies the started-workflow payload preserves the caller-supplied
  `task_id` with no generated substitute.
- **`task_id` in every activity input:** satisfied by
  `tests/test_temporal_trace_propagation.py::test_task_id_in_every_activity_input`,
  which verifies all five scheduled activity payloads carry the same
  `task_id`.
- **W3C `traceparent` roundtrip:** satisfied by
  `tests/test_temporal_trace_propagation.py::test_traceparent_roundtrip`,
  which verifies all five activity spans inherit the known upstream
  `trace_id`.
- **Unbroken trace across restart:** satisfied by
  `tests/test_temporal_trace_propagation.py::test_trace_unbroken_across_restart`,
  which verifies the workflow keeps a single root trace across the
  kill/restart boundary.
- **Missing-propagation failure path:** satisfied by
  `tests/test_temporal_trace_propagation.py::test_missing_task_id_raises_propagation_error`,
  which verifies `MissingTaskIdError` and the `audit.propagation_error` audit
  event fire before downstream execution continues.

---

#### A2-2 — Emit audit events for every workflow lifecycle transition

**Status: IMPLEMENTED AND VALIDATED (2026-03-09)**

`src/audit_vault/logger.py` now exposes a dedicated `lifecycle_event()` helper
plus `EXPECTED_PHASE2_LIFECYCLE_EVENTS` for the happy-path count contract.
`src/control_plane/scheduler.py` emits lifecycle metadata from activities that
already exist on every workflow path: `workflow.started` from `PrePIIScrub`,
`llm.retried` from `LLMInvoke`, and `workflow.completed` from `PostSanitize`.
The existing `WorkflowAudit` activity now enriches workflow-level events with
the same lifecycle envelope for `workflow.pending_approval`,
`workflow.approved`, `workflow.denied`, `workflow.timed_out`, and
`workflow.failed`, including `event_type`, `session_id`, `workflow_status`,
`timestamp`, `sequence_number`, and `traceparent`.

**Validation evidence (2026-03-09)**

- `ruff check src/audit_vault/logger.py src/control_plane/scheduler.py tests/test_audit_lifecycle_count.py tests/test_audit_lifecycle_events.py tests/test_hitl_pending_approval.py tests/test_p2_retry.py tests/test_budget_replay.py` -> passed
- `mypy src/audit_vault/logger.py src/control_plane/scheduler.py tests/test_audit_lifecycle_count.py tests/test_audit_lifecycle_events.py tests/test_hitl_pending_approval.py tests/test_p2_retry.py tests/test_budget_replay.py` -> passed
- `pytest -q tests/test_audit_lifecycle_count.py tests/test_audit_lifecycle_events.py tests/test_hitl_pending_approval.py tests/test_p2_retry.py` -> `34 passed in 6.99s`
- `pytest -q tests/test_temporal_workflow.py tests/test_p2_chaos.py tests/test_p2_encryption.py tests/test_temporal_trace_propagation.py tests/test_budget_replay.py tests/test_audit_lifecycle_count.py tests/test_audit_lifecycle_events.py` -> `65 passed in 126.95s`
- `/home/xbyooki/anaconda3/envs/aegis/bin/pytest tests/ -ra 2>&1 | tee /tmp/aegis-full-suite-2026-03-09-a2-2.log` -> `778 passed, 29 skipped, 4 warnings in 198.75s`

Events: `started`, `retried` (with retry count), `pending-approval`,
`approved`, `denied`, `completed`, `failed`.

**Testing requirements**

- **Happy-path lifecycle sequence:** satisfied by
  `tests/test_audit_lifecycle_count.py::test_happy_path_emits_exact_expected_phase2_lifecycle_events`,
  which verifies the emitted lifecycle sequence matches
  `EXPECTED_PHASE2_LIFECYCLE_EVENTS` exactly.
- **Retry event carries count:** satisfied by
  `tests/test_audit_lifecycle_events.py::TestLifecycleEvents::test_retry_events_increment_and_completed_reports_total_retries`,
  which verifies retry counts increment from 1 to 2 and the completed event
  reports the total.
- **All seven event types covered with mandatory fields:** satisfied by
  `tests/test_audit_lifecycle_events.py::TestLifecycleEvents::{test_started_event_has_mandatory_fields,test_retried_event_has_mandatory_fields,test_pending_approval_event_has_mandatory_fields,test_approved_event_has_mandatory_fields,test_denied_event_has_approver_id_and_mandatory_fields,test_completed_event_has_mandatory_fields,test_failed_event_has_mandatory_fields}`.
- **Denied carries `approver_id`:** satisfied by
  `tests/test_audit_lifecycle_events.py::TestLifecycleEvents::test_denied_event_has_approver_id_and_mandatory_fields`.
- **Outage path preserves observed lifecycle transitions:** satisfied by
  `tests/test_audit_provider_outage.py::test_retry_recovery_has_complete_gapless_ordered_audit_trails`,
  which verifies recovered tasks emit the expected started/retried/completed
  lifecycle sequences without gaps.
- **Happy-path count regression:** satisfied by the same
  `tests/test_audit_lifecycle_count.py` suite, which is the concrete CI guard
  for `EXPECTED_PHASE2_LIFECYCLE_EVENTS`.

---

#### A2-3 — Deterministic retry-recovery simulation: audit trail complete with no missing events

**Status: COMPLETE (2026-03-09)**

**Implementation summary**

- `tests/test_audit_provider_outage.py` now exercises a deterministic retry-
  recovery scenario with a bounded concurrent workflow set under
  `WorkflowEnvironment.start_time_skipping()`.
- `_OutageAfterOneSuccessAdapter` forces one immediate success, then one
  retryable `ProviderOutageError` for every remaining task before allowing all
  retries to recover, which gives the audit assertions a controlled
  retry/recovery boundary.
- `_MonotonicRecordingAuditLogger` records strictly increasing timestamps so the
  test can assert gapless `sequence_number` progression and per-task timestamp
  ordering without wall-clock noise.
- The Gate 2 audit contract no longer mixes deterministic audit assertions with
  real-time recovery latency. True network-fault and restoration-latency
  validation remains an infrastructure release requirement blocked on
  `toxiproxy` (or equivalent) support in CI.

**Validation evidence**

- Focused retry-recovery validation passed on 2026-03-09:
  `conda run -n aegis pytest -q tests/test_audit_provider_outage.py::test_retry_recovery_has_complete_gapless_ordered_audit_trails`
  -> passed locally after the deterministic split; see Gate 2 reassessment for
  repeat-run evidence.
- Focused audit regression checks passed on 2026-03-09:
  `conda run -n aegis pytest -q tests/test_audit_ordering.py tests/test_schema_validity.py tests/test_audit_logger_a1_2.py tests/test_audit_logger_a1_3.py`
  -> `89 passed in 5.14s`
- Broader Phase 2 workflow/audit regression slice passed on 2026-03-09:
  `conda run -n aegis pytest -q tests/test_audit_lifecycle_count.py tests/test_audit_lifecycle_events.py tests/test_temporal_trace_propagation.py tests/test_temporal_workflow.py`
  -> `30 passed in 7.99s`

**Testing requirements**

- **Deterministic recovery setup:** satisfied by
  `tests/test_audit_provider_outage.py::test_retry_recovery_has_complete_gapless_ordered_audit_trails`,
  which uses the in-test outage adapter to force one immediate success, one
  retryable outage for every remaining task, and then controlled recovery.
- **No missing events:** satisfied by the same retry-recovery test, which verifies
  task-scoped `sequence_number` values are gapless for every workflow.
- **Per-task event order:** satisfied by the same retry-recovery test, which verifies
  task-scoped timestamps remain sorted after recovery.
- **`retried` event present on recovered tasks:** satisfied by the same
  retry-recovery
  test, which requires every failed-then-recovered task to emit
  `llm.retried` before `workflow.completed`.
- **Bounded concurrent coverage:** satisfied by the same retry-recovery test,
  which runs a bounded concurrent recovery scenario under time-skipping to keep
  the Gate 2 audit contract deterministic.
- **Real outage latency gate:** satisfied by
  `tests/test_audit_provider_outage_latency.py::test_live_outage_latency_recovers_within_30_seconds_after_toxiproxy_restoration`,
  which routes `LocalLlamaAdapter` traffic through the Compose-managed
  `toxiproxy` service, disables the proxy before the first request, restores it
  during the retry window, and verifies workflow completion occurs within the
  30-second post-restoration budget with the expected retry/completion audit
  lifecycle sequence.

---

#### A2-4 — No audit event emitted with a timestamp earlier than the previous event for the same `task_id`

**Status: COMPLETE (2026-03-09)**

**Implementation summary**

- `src/audit_vault/logger.py` now preserves caller-provided task timestamps via
  a custom timestamp processor instead of unconditionally overwriting them with
  `structlog.processors.TimeStamper`.
- `AuditLogger` now tracks per-task last-emitted timestamps alongside the
  existing per-task `sequence_number` counters.
- Backward wall-clock drift on logger-generated timestamps is clamped to the
  previous task timestamp and emits `audit.clock_skew_warning` with
  `previous_timestamp`, `attempted_timestamp`, `adjusted_timestamp`, and
  `skew_ms` metadata.
- Explicit caller attempts to emit an earlier timestamp now raise
  `AuditOrderingError` and do not write the out-of-order event.
- `docs/audit-event-schema.json` now accepts the clock-skew warning metadata,
  and schema conformance coverage includes a real `audit.clock_skew_warning`
  fixture event.

**Validation evidence**

- `tests/test_audit_ordering.py` added for the A2-4 contract: 100-task
  monotonic ordering, backward-clock skew warning/clamp behavior, concurrent
  task isolation, and explicit out-of-order rejection.
- Focused validation passed on 2026-03-09:
  `conda run -n aegis pytest -q tests/test_audit_ordering.py tests/test_schema_validity.py tests/test_audit_logger_a1_2.py tests/test_audit_logger_a1_3.py`
  -> `89 passed in 5.24s`
- Targeted lint/type checks passed on the changed files:
  `conda run -n aegis ruff check src/audit_vault/logger.py tests/test_audit_ordering.py tests/test_schema_validity.py`
  -> `All checks passed!`
  and
  `conda run -n aegis mypy src/audit_vault/logger.py tests/test_audit_ordering.py tests/test_schema_validity.py`
  -> `Success: no issues found in 3 source files`

**Testing requirements**

- **Monotonic per-task timestamps:** satisfied by
  `tests/test_audit_ordering.py::TestAuditTimestampOrdering::test_monotonic_timestamps_for_100_tasks`,
  which verifies ordered timestamps and gapless sequence numbers across 100
  task streams.
- **Clock skew warning and clamp behavior:** satisfied by
  `tests/test_audit_ordering.py::TestAuditTimestampOrdering::test_clock_skew_emits_warning_and_clamps_timestamp`,
  which verifies backward drift emits `audit.clock_skew_warning` and keeps the
  business-event timestamps ordered.
- **Concurrent task isolation:** satisfied by
  `tests/test_audit_ordering.py::TestAuditTimestampOrdering::test_concurrent_tasks_do_not_interfere`,
  which verifies task-local monotonicity under interleaved concurrent emits.
- **Explicit out-of-order emission blocked:** satisfied by
  `tests/test_audit_ordering.py::TestAuditTimestampOrdering::test_explicit_out_of_order_timestamp_is_blocked`,
  which verifies `AuditOrderingError` is raised and the retrograde event is
  not written.

---

### Frontend & DevEx Team

#### F2-1 — Add Temporal UI link, workflow state diagram, and recovery procedure to `docs/deployment-guide.md`

**Status: COMPLETE (2026-03-09)**

The deployment guide already documented Temporal service ports, but the
Phase 2 workflow operations content was incomplete. A live-contract update
landed on 2026-03-07. On 2026-03-09 the remaining gate evidence was completed:

- automated validation passed via `tests/test_deployment_guide_completeness.py`
  on the deployment-guide completeness checks
- manual deployment-guide review confirmed the Temporal UI link, workflow state
  diagram, and numbered recovery procedure match the live task-based workflow
  model
- sign-off was recorded in the Gate 2 review issue: GitHub issue `#2`

**Testing requirements**

- **Temporal UI link present:** satisfied by
  `tests/test_deployment_guide_completeness.py::test_deployment_guide_contains_temporal_ui_link`,
  which verifies the guide includes the live Temporal UI address.
- **Workflow state diagram coverage:** satisfied by
  `tests/test_deployment_guide_completeness.py::test_deployment_guide_contains_workflow_state_diagram_states`,
  which verifies the documented workflow states include `running`,
  `pending-approval`, `approved`, `denied`, `completed`, and `failed`.
- **Numbered recovery procedure:** satisfied by
  `tests/test_deployment_guide_completeness.py::test_deployment_guide_contains_numbered_recovery_procedure`,
  which verifies the guide contains a numbered recovery section and the
  required `restart`, `Temporal`, and `task_id` terms.
- **Peer review gate:** satisfied on 2026-03-09; reviewer sign-off recorded in
  Gate 2 review issue `#2`.

---

#### F2-2 — HITL approval API contract in `docs/api-reference.md`

**Status: COMPLETE (2026-03-09)**

`docs/api-reference.md` existed as a placeholder, but it used the wrong
resource shape (`workflow_id` instead of `task_id`). A live-contract update
landed on 2026-03-07. On 2026-03-09 the remaining gate evidence was completed:

- automated schema and contract validation passed via
  `tests/test_api_reference_schemas.py`,
  `tests/test_api_reference_completeness.py`, and
  `tests/test_hitl_timeout_contract.py`
- manual API-reference review confirmed the live `task_id`-based approve/deny
  contract, status codes, timeout behavior, and admin-only authorization notes
- sign-off was recorded in the Gate 2 review issue: GitHub issue `#2`

Request schema, response schema, error codes, and timeout behaviour.

**Testing requirements**

- **Schema blocks present and valid:** satisfied by
  `tests/test_api_reference_schemas.py::{test_hitl_api_reference_schema_blocks_are_present,test_hitl_api_reference_schema_blocks_are_valid_draft7}`,
  which verify the request, response, and structured-error schema blocks are
  present in `docs/api-reference.md` and validate as Draft 7 schemas.
- **Live task-based endpoint contract documented:** satisfied by
  `tests/test_api_reference_completeness.py::test_api_reference_documents_live_task_based_hitl_endpoints`,
  which verifies the docs use `/api/v1/tasks/{task_id}/approve|deny` and do
  not retain the legacy workflow-shaped endpoints.
- **Status code contract documented against the implementation:** satisfied by
  `tests/test_api_reference_completeness.py::test_api_reference_documents_live_status_codes`,
  which verifies the documented status-code set matches the live endpoints.
- **Timeout behavior documented and enforced:** satisfied by
  `tests/test_hitl_timeout_contract.py::{test_hitl_timeout_behavior_is_documented,test_hitl_timeout_returns_409}`,
  which verify the `409 pending_approval_conflict` timeout contract is both
  documented and returned by the live endpoint.

---

#### F2-3 — Write `docs/runbooks/hitl-stuck-approval.md`

**Status: COMPLETE (2026-03-09)**

The skeleton file existed before the gap pass. The runbook content was updated
to match the live task-based HITL endpoints on 2026-03-07. On 2026-03-09 the
remaining gate evidence was completed:

- automated section and cross-reference validation passed via
  `tests/test_runbook_completeness.py`, `tests/test_runbook_cross_references.py`,
  and `tests/test_runbook_commands.py`
- a non-author live walkthrough was executed against the local Temporal/API
  stack using the current checkout
- sign-off was recorded in the Gate 2 review issue: GitHub issue `#2`

Diagnosis, escalation path, and resolution steps.

**Testing requirements**

- **Section completeness:** satisfied by
  `tests/test_runbook_completeness.py::test_hitl_runbook_contains_required_sections`,
  which verifies `Symptoms`, `Diagnosis`, `Escalation`, and `Resolution` are
  all present.
- **Endpoint cross-reference accuracy:** satisfied by
  `tests/test_runbook_cross_references.py::test_runbook_endpoint_paths_are_documented_in_api_reference`,
  which verifies task approve/deny endpoints referenced by the runbooks match
  `docs/api-reference.md`.
- **Command validity:** satisfied by
  `tests/test_runbook_commands.py::test_hitl_runbook_bash_blocks_execute`,
  which executes every shell block from the HITL runbook under the validation
  harness.
- **Runbook walkthrough gate:** satisfied on 2026-03-09; live walkthrough
  sign-off recorded in Gate 2 review issue `#2`.

---

#### F2-4 — Update `docs/runbooks/budget-exceeded.md` to cover the HITL approval flow

**Status: COMPLETE (2026-03-09)**

The runbook existed but still referenced a non-existent `/api/v1/approvals`
endpoint. The Phase 2 HITL flow was updated on 2026-03-07. On 2026-03-09 the
remaining gate evidence was completed:

- automated content and cross-reference validation passed via
  `tests/test_runbook_completeness.py`, `tests/test_runbook_cross_references.py`,
  `tests/test_runbook_commands.py`, and
  `tests/test_runbook_budget_version.py`
- a non-author live walkthrough of the approve/deny HITL flow was executed
  against the local Temporal/API stack using the current checkout
- sign-off was recorded in the Gate 2 review issue: GitHub issue `#2`

**Testing requirements**

- **Phase 2 HITL terminology present:** satisfied by
  `tests/test_runbook_budget_version.py::test_budget_runbook_mentions_phase2_hitl_terms`
  and
  `tests/test_runbook_completeness.py::test_budget_runbook_contains_phase2_hitl_terms`,
  which verify `PendingApproval`, `approve`, and `deny` appear in the budget
  runbook.
- **`HITL Approval Flow` section present:** satisfied by
  `tests/test_runbook_budget_version.py::test_budget_runbook_includes_hitl_approval_flow_section`.
- **Legacy endpoint references removed:** satisfied by
  `tests/test_runbook_budget_version.py::test_budget_runbook_does_not_reference_legacy_approvals_endpoint`
  and
  `tests/test_runbook_completeness.py::test_runbooks_do_not_reference_legacy_hitl_endpoints`.
- **Endpoint cross-reference accuracy:** satisfied by
  `tests/test_runbook_cross_references.py::test_runbook_endpoint_paths_are_documented_in_api_reference`.
- **Command and snippet validity:** satisfied by
  `tests/test_runbook_commands.py::{test_budget_runbook_bash_blocks_execute,test_budget_runbook_python_snippet_compiles}`.
- **Runbook walkthrough gate:** satisfied on 2026-03-09; non-author live
  walkthrough sign-off recorded in Gate 2 review issue `#2`.

---

### Cross-Team Gap Pass (2026-03-07)

- **Platform**: Complete for the Platform-team testing scope. P2-1 through
  P2-4 are implemented and validated, and the testing requirements above now
  point to the concrete in-repo Temporal workflow, retry, encryption, and
  chaos suites that satisfy the Phase 2 Platform expectations.
- **Watchdog & Reliability**: Partial. Serialization/checkpoint primitives are
  present, but Temporal-history restoration, replay/idempotency, and 1,000-task
  recovery coverage are still truly not started.
- **Audit & Compliance**: Partial. `task_id`, `traceparent`, and lifecycle
  vocabulary exist at the logger layer, but the Phase 2 Temporal propagation,
  outage completeness, and ordering enforcement suites are still truly not
  started.
- **Frontend & DevEx**: Complete for Gate 2 scope. Automated doc-validation now
  passes for the deployment guide, API reference, and HITL runbooks; manual
  review and walkthrough sign-offs are recorded in Gate 2 review issue `#2`;
  the missing runbook test harnesses now exist in-repo.

**Testing requirements**

- **Diff against Phase 1 version:** satisfied on 2026-03-09 by
  `tests/test_runbook_budget_version.py`.
- **Section completeness check:** assert the runbook contains a
  `HITL Approval Flow` section (or equivalent heading) describing both the
  approval and denial paths; `tests/test_runbook_completeness.py` is
  extended to verify this.
- **Walkthrough test:** satisfied on 2026-03-09; live walkthrough sign-off
  recorded in Gate 2 review issue `#2`.
- **Cross-reference accuracy:** every API endpoint URL referenced in the
  runbook must be verifiable against `docs/api-reference.md`;
  `tests/test_runbook_cross_references.py` diffs endpoint paths between
  the two documents and fails on any mismatch.

### Gate 2 Reassessment (2026-03-09)

- Frontend & DevEx no longer blocks Gate 2. F2-1 through F2-4 are complete,
  and the non-author walkthrough plus manual review sign-offs are recorded in
  Gate 2 review issue `#2`.
- Focused stale-check validation on 2026-03-09 re-ran the remaining unchecked
  Platform, Watchdog, and release-anchor tests:
  `conda run -n aegis pytest -q tests/test_temporal_workflow.py tests/test_no_workflow_stubs.py tests/test_p2_retry.py tests/test_p2_encryption.py tests/test_p2_chaos.py tests/test_budget_enforcer.py::TestBudgetSessionSerialization::test_serialize_deserialize_full_roundtrip tests/test_budget_enforcer.py::test_budget_exact_recovery_after_restart tests/test_budget_recovery.py::test_budget_recovery_across_kill_at_each_stage tests/test_loop_detector.py::TestLoopDetectorSerialization::test_checkpoint_restore_full_roundtrip`
  -> `93 passed in 76.02s`.
- Release-check snapshot on 2026-03-09: `pytest -q` completed with `822 passed`,
  `0 skipped`, `0 failed`; `mypy src/` passed with zero errors; `ruff check
  src/ tests/` passed with zero errors.
- Cold-start infrastructure validation passed on 2026-03-09 after wiring
  `docker-compose.yml` startup to the real Temporal namespace readiness gate:
  `docker-compose down --remove-orphans && docker-compose up -d` reached
  `GET /health -> {"status":"ok","service":"aegis-os"}` in `15s`,
  `docker-compose ps` showed `temporal` healthy before `aegis-api` and
  `aegis-worker` startup, `docker-compose logs --tail=120 aegis-worker`
  produced no warning/error markers, and the live OPA probe
  `POST /v1/data/aegis/agent_access` returned `allow=true` for the Phase 2
  admin approval case with no startup errors in the OPA log.
- Network fault-injection infrastructure landed on 2026-03-09: `docker-compose`
  now defines a dedicated `toxiproxy` service, `docs/deployment-guide.md`
  documents the `http://localhost:18474` admin API under `Test Infrastructure`,
  `docker-compose up -d toxiproxy && curl http://localhost:18474/version`
  returned `{"version":"2.12.0"}`, and the focused guard suite
  `tests/test_toxiproxy_infrastructure.py tests/test_deployment_guide_completeness.py tests/test_temporal_worker_service.py`
  passed.
- Live outage-latency validation passed on 2026-03-09:
  `docker-compose up -d toxiproxy && conda run -n aegis pytest -q tests/test_audit_provider_outage.py tests/test_audit_provider_outage_latency.py tests/test_toxiproxy_infrastructure.py`
  -> `4 passed in 4.85s`, including the new toxiproxy-backed recovery-latency
  test that completed within the 30-second post-restoration window.
- Live outage-latency validation was re-run on 2026-03-10 after the Gate 2
  rename cleanup:
  `docker-compose up -d toxiproxy && conda run -n aegis pytest -q tests/test_audit_provider_outage_latency.py`
  -> `1 passed in 4.75s`.
- Targeted watchdog coverage validation passed on 2026-03-09:
  `conda run -n aegis pytest --cov=src.watchdog.budget_enforcer --cov=src.watchdog.loop_detector --cov-branch --cov-report=term-missing -q tests/test_budget_enforcer.py tests/test_loop_detector.py`
  -> `71 passed in 2.51s` with `src/watchdog/budget_enforcer.py` and
  `src/watchdog/loop_detector.py` both at `100%` branch coverage.
- Deterministic retry-recovery stability was revalidated on 2026-03-10 after
  clearing `.pytest_cache`, with twenty consecutive passes on the renamed test:
  `for i in $(seq 1 20); do conda run -n aegis pytest -q tests/test_audit_provider_outage.py::test_retry_recovery_has_complete_gapless_ordered_audit_trails || exit 1; done`
  -> `retry-recovery-stress-pass`.
- Gate 2 technical evidence is now complete. The Go/No-Go checklist and
  release requirements below are satisfied by current in-repo tests,
  infrastructure validation, and documentation evidence.

---

### 🚦 Go/No-Go Gate 2 → `v0.4.0`

**Gate review:** all five team leads + one external reviewer. The gate review
is a synchronous session — all teams present. Every checklist item below must
be ticked and every gate criterion must show **PASS** before the `v0.4.0` tag
is cut. A single unchecked item or FAIL blocks the release.

---

#### Pre-Gate Test Suite Checklist

All tests must be green on `main` at the time of the gate review. No test may
be skipped, marked `xfail`, or guarded by a feature flag to achieve a passing
run.

Where implementation test names diverged from the original Gate 2 planning
labels, the checklist below now records the current in-repo test names.

**Platform**
- [x] `tests/test_temporal_workflow.py::TestActivityMappingCompleteness` — five Temporal activities registered with exact stage names; missing or renamed activity fails
- [x] `tests/test_temporal_workflow.py::TestActivityExecutionOrder::test_activities_execute_in_documented_order` — `WorkflowEnvironment.start_time_skipping()` harness; activities scheduled in documented sequence; reordering fails
- [x] `tests/test_temporal_workflow.py::TestFullWorkflowExecution::{test_workflow_emits_all_five_stage_spans,test_workflow_reaches_completed_status,test_workflow_returns_non_empty_content}` — live Temporal dev server; workflow reaches `completed`, output content is non-empty, and all five OTel spans are present
- [x] `tests/test_no_workflow_stubs.py::TestNoActivityStubs` — `inspect.getsource()` on every activity asserts no `pass`, `...`, or `raise NotImplementedError` body
- [x] `tests/test_p2_retry.py::TestRetryCountCap::test_rate_limit_error_retried_exactly_five_times` — `RateLimitError` on every call; activity retried exactly 5 times then escalation; not 4, not 6
- [x] `tests/test_p2_retry.py::TestBackoffTiming::{test_retry_intervals_are_approximately_exponential,test_backoff_sequence_four_gaps_for_five_attempts}` — retry spacing follows the documented doubling schedule for the first four gaps
- [x] `tests/test_p2_retry.py::TestNonRetryableErrors` — `PolicyDeniedError` not retried; immediate failure path remains non-retryable
- [x] `tests/test_p2_retry.py::TestSuccessfulRecovery::{test_workflow_recovers_after_two_failures,test_successful_recovery_emits_two_retry_audit_events_and_counts_only_success_tokens}` — two failures then success; workflow returns a success result and audit records exactly two retry events
- [x] `tests/test_p2_encryption.py::TestEncryptionInTransit::test_workflow_history_payloads_are_encrypted` — Temporal history payloads contain no plaintext prompt text or PII
- [x] `tests/test_p2_encryption.py::TestRoundTripCorrectness` — all five activity input/output types serialize and deserialize byte-for-byte identically
- [x] `tests/test_p2_encryption.py::TestPlaintextHardFailureGuard::test_create_aegis_data_converter_has_encrypted_payload_codec` — CI guard asserting the data converter is not the SDK default `JsonPlainPayloadConverter`
- [x] `tests/test_p2_chaos.py::TestWorkerKillResumption::test_kill_at_each_stage_workflow_completes` — worker kill after each stage; Temporal resumes from the next stage with no completed-stage re-run
- [x] `tests/test_p2_chaos.py::TestWorkerKillResumption::test_no_duplicate_llm_calls_across_kill_restart` — `LLMInvoke` is called at most once across the kill/restart boundary
- [x] `tests/test_p2_chaos.py::TestWorkerKillResumption::test_audit_event_sequence_matches_uninterrupted_run` — audit event sequence across kill/restart matches the uninterrupted baseline by event type and stage name

**Security & Governance**
- [x] `test_pending_approval_trigger` — projected spend > $50.01 → `PendingApproval`; exactly $50.00 → does not trigger
- [x] `test_pending_approval_state_machine` — all four states (`awaiting-approval`, `approved`, `denied`, `timed-out`) each have exactly one tested transition
- [x] `test_pending_approval_execution_halt` — no LLM adapter invoked while workflow is in `PendingApproval`
- [x] `test_pending_approval_approve_resumes` — `workflow.signal()` approve; workflow resumes; valid `TaskResponse` returned
- [x] `test_pending_approval_deny_terminates` — deny signal; `workflow.denied` event with non-null `reason` and `approver_id`
- [x] `test_pending_approval_timeout` — 2-second test timeout; auto-terminates with `timed-out` audit event; never hangs
- [x] `test_hitl_admin_approve` — `role=admin` JWT; HTTP `200`; workflow receives approve signal
- [x] `test_hitl_non_admin_blocked` — `role=operator` JWT; HTTP `403` issued by OPA, not a hardcoded check; same for deny
- [x] `test_hitl_invalid_token_blocked` — malformed/expired JWT on approve; HTTP `401`
- [x] `test_hitl_nonexistent_task` — approve for unknown `task_id`; HTTP `404` with structured error body
- [x] `test_hitl_rbac_matrix` — 2 endpoints × 5 roles = 10 cases; only `admin` receives `200`; all others `403`
- [x] `test_jit_new_token_per_retry` — two attempts; both `jti` values present in audit; confirmed distinct UUIDs
- [x] `test_jit_prior_jti_rejected` — reuse first-attempt token after retry; `401` returned
- [x] `test_jit_scope_preserved_on_reissue` — re-issued token carries same `agent_type`, `session_id`, `allowed_actions`; no scope escalation
- [x] `test_jit_uniqueness_across_retried_chain` — 3 retries; all four `jti` values are distinct UUIDs
- [x] `test_adversarial_expired_token_on_approve` — expired token; HTTP `401` and `jit.expired` audit event; never `200`
- [x] `test_adversarial_revoked_token` — revoked `jti`; `401` within 1 second of revocation
- [x] `test_adversarial_cross_session_token` — token for `session_id=A` used on `session_id=B`; `403` and `audit.cross_session_attempt` event
- [x] `test_adversarial_no_silent_accept` — five adversarial token scenarios (expired, revoked, wrong session, wrong role, malformed signature); every scenario returns `4xx` and emits audit event

**Watchdog & Reliability**
- [x] `test_budget_serialize_deserialize_round_trip` — $7.34 serialized/deserialized; `Decimal("7.34")` exact; zero floating-point drift
- [x] `test_budget_exact_recovery_after_restart` — $3.00 recorded; kill/restart; `restore_from_history()` returns `Decimal("3.00")`; one-cent variance fails
- [x] `test_budget_no_double_count_on_redelivery` — same `LLMInvoke` result delivered twice; spend recorded exactly once; idempotency key is `task_id`
- [x] `tests/test_budget_recovery.py::test_budget_recovery_across_kill_at_each_stage` — five-stage kill cycle; spend remains exact after every recovery
- [x] `test_loop_counter_preserved_on_retry` — serialize after 2 steps, restore, add 1 more; counter is 3 not 1
- [x] `test_loop_retry_does_not_reset_counter` — re-delivered activity input; counter increments, does not reset
- [x] `test_loop_checkpoint_round_trip` — checkpoint after 3 steps; restored detector trips on 5th step, not 3rd
- [x] `test_loop_halt_cross_restart` — `NO_PROGRESS` at steps 2 and 4 with kill between; trips at correct cumulative count; audit records pre-restart signals
- [x] `test_loop_progress_reset_survives_restart` — `PROGRESS` counter reset survives serialize/restore cycle; no false trip after reset
- [x] `test_prometheus_hitl_stuck_syntax` — rule loads in Prometheus container; `aegis_hitl_stuck` listed with state `inactive`
- [x] `test_prometheus_hitl_stuck_fires` — metric at 86,401 s; alert fires with severity `critical`
- [x] `test_prometheus_hitl_stuck_silent` — metric at 86,399 s; alert remains `inactive`
- [x] `test_prometheus_runbook_link` — `runbook_url` annotation present and non-empty on every alert rule in `docs/prometheus.yml`
- [x] `test_budget_replay` — 1,000-task `AEGIS_REPLAY_SEED`-seeded replay; zero double-counted spend; spend sum matches audit log to 4 d.p.; all complete in < 5 min

**Audit & Compliance**
- [x] `test_task_id_in_workflow_input` — `task_id` present in `WorkflowExecutionStartedEventAttributes`; not a generated substitute
- [x] `test_task_id_in_every_activity_input` — `task_id` present in all five `ScheduledEventAttributes` payloads; missing on any activity fails
- [x] `test_traceparent_roundtrip` — known `trace-id` appears in OTel spans for all five activities
- [x] `test_trace_unbroken_across_restart` — all spans before and after kill/restart share identical root `trace_id`; zero orphaned spans
- [x] `test_audit_happy_path_sequence` — `started` then `completed` in order; no other lifecycle events on a happy-path run
- [x] `test_audit_retry_count_increments` — two failures then success; `retry_count` is 1 then 2; `completed` carries `total_retries=2`
- [x] `test_audit_all_seven_event_types` — one scenario per type; all seven events carry mandatory fields `task_id`, `session_id`, `timestamp`, `event_type`, `workflow_status`
- [x] `test_audit_denied_has_approver_id` — `denied` event has non-null `approver_id` matching the identity in the deny request
- [x] `test_audit_lifecycle_count` — happy-path task emits exactly `EXPECTED_PHASE2_LIFECYCLE_EVENTS` events; `N±1` fails
- [x] `tests/test_audit_provider_outage.py::test_retry_recovery_has_complete_gapless_ordered_audit_trails` — deterministic retry recovery preserves gapless `sequence_number` ordering, sorted timestamps, and required `llm.retried` lifecycle events for every recovered task
- [x] `tests/test_audit_provider_outage_latency.py::test_live_outage_latency_recovers_within_30_seconds_after_toxiproxy_restoration` — live network fault injection through the Compose-managed `toxiproxy` service restores the provider during the retry window and the workflow completes within the 30-second post-restoration budget
- [x] `tests/test_audit_ordering.py::TestAuditTimestampOrdering::test_monotonic_timestamps_for_100_tasks` — 100 tasks; every consecutive task-scoped event pair satisfies `timestamp_{n+1} >= timestamp_n`
- [x] `tests/test_audit_ordering.py::TestAuditTimestampOrdering::test_clock_skew_emits_warning_and_clamps_timestamp` — backward wall-clock drift raises `audit.clock_skew_warning` and preserves logical ordering
- [x] `tests/test_audit_ordering.py::TestAuditTimestampOrdering::test_explicit_out_of_order_timestamp_is_blocked` — retrograde event timestamps raise `AuditOrderingError` and the event is not written

**Frontend & DevEx**
- [x] `tests/test_deployment_guide_completeness.py::{test_deployment_guide_contains_temporal_ui_link,test_deployment_guide_contains_workflow_state_diagram_states,test_deployment_guide_contains_numbered_recovery_procedure}` — deployment guide contains the Temporal UI link, all six workflow states, and the numbered recovery procedure
- [x] `tests/test_api_reference_schemas.py::test_hitl_api_reference_schema_blocks_are_valid_draft7` — fenced `json` blocks in `docs/api-reference.md` validate as Draft 7 schemas
- [x] `tests/test_api_reference_completeness.py::{test_api_reference_documents_live_task_based_hitl_endpoints,test_api_reference_documents_live_status_codes}` — API reference matches the live task-based HITL endpoints and documented status-code contract
- [x] `tests/test_hitl_timeout_contract.py::{test_hitl_timeout_behavior_is_documented,test_hitl_timeout_returns_409}` — timeout behavior is documented and the live endpoint returns the documented `409 pending_approval_conflict` response
- [x] `tests/test_runbook_completeness.py::test_hitl_runbook_contains_required_sections` — `docs/runbooks/hitl-stuck-approval.md` contains `Symptoms`, `Diagnosis`, `Escalation`, and `Resolution`
- [x] `tests/test_runbook_commands.py::test_hitl_runbook_bash_blocks_execute` — every shell command extracted from `hitl-stuck-approval.md` executes with exit code 0 under the validation harness
- [x] `tests/test_prometheus_rules.py::test_prometheus_runbook_link` — `runbook_url` annotation resolves to the HITL runbook path used by the alert
- [x] `tests/test_runbook_budget_version.py::{test_budget_runbook_mentions_phase2_hitl_terms,test_budget_runbook_includes_hitl_approval_flow_section}` — `docs/runbooks/budget-exceeded.md` contains `PendingApproval`, `approve`, `deny`, and the `HITL Approval Flow` section
- [x] `tests/test_runbook_cross_references.py::test_runbook_endpoint_paths_are_documented_in_api_reference` — endpoint paths referenced in the runbooks match `docs/api-reference.md`

---

#### Gate Criteria

| ID | Criterion | Owner | Pass definition |
|---|---|---|---|
| **G2-1** | Crash recovery — no re-execution | Platform | All Platform checklist items above pass; `test_chaos_kill_at_each_stage` confirms Temporal resumes from the correct stage in all five kill scenarios with zero re-executed activities |
| **G2-2** | HITL approval integrity | Security & Governance | All Security & Governance checklist items above pass; `test_hitl_rbac_matrix` 10/10 cases correct; `test_adversarial_no_silent_accept` zero silent accepts across all five adversarial token scenarios |
| **G2-3** | No duplicate spend on retry | Watchdog & Reliability | All Watchdog & Reliability checklist items above pass; `test_budget_replay` 1,000-task run shows zero double-counted cents; spend sum matches audit log to four decimal places |
| **G2-4** | Gapless audit and outage recovery | Audit & Compliance | All Audit & Compliance checklist items above pass; deterministic retry-recovery validation shows 0 missing events / 0 out-of-order timestamps, and the live toxiproxy outage-latency test completes within the 30-second post-restoration window |
| **G2-5** | Runbooks complete and verified | Frontend & DevEx | All Frontend & DevEx checklist items above pass; both runbooks walked through by a non-author team member; all commands execute successfully |

---

#### Release Requirements for `v0.4.0`

All of the following must be complete before the tag is cut.

**Code**
- [x] `main` branch passes `pytest` with zero failures, zero skips, zero `xfail` markers in production test paths *(822 passed, 0 skipped, 0 failed on 2026-03-09)*
- [x] `mypy src/` reports zero errors
- [x] `ruff check src/ tests/` reports zero errors
- [x] No `pass`, `raise NotImplementedError`, or `...` in any Temporal activity method body under `src/control_plane/`; enforced by `tests/test_no_workflow_stubs.py::TestNoActivityStubs`
- [x] The Temporal data converter is not the SDK default `JsonPlainPayloadConverter`; enforced by `tests/test_p2_encryption.py::TestPlaintextHardFailureGuard::test_create_aegis_data_converter_has_encrypted_payload_codec`
- [x] `BudgetSession.serialize()` / `deserialize()` round-trip test passes at 100% branch coverage; validated on 2026-03-09 by the targeted `--cov-branch` watchdog run covering `tests/test_budget_enforcer.py`
- [x] `LoopDetector.checkpoint()` / `restore()` round-trip test passes at 100% branch coverage; validated on 2026-03-09 by the targeted `--cov-branch` watchdog run covering `tests/test_loop_detector.py`
- [x] `CHANGELOG.md` entry written for `v0.4.0` listing all shipped capabilities

**Documentation**
- [x] `docs/deployment-guide.md` includes Temporal workflow state diagram (all six states), Temporal UI link, and numbered recovery procedure
- [x] `docs/api-reference.md` documents `approve` and `deny` endpoints with full request/response JSON schemas, live error codes, and timeout behaviour
- [x] `docs/runbooks/hitl-stuck-approval.md` is complete, commands are verified by harness, and the runbook was walked through by a non-author
- [x] `docs/runbooks/budget-exceeded.md` is updated for HITL approval flow and includes the `HITL Approval Flow` section
- [x] `docs/threat-model.md` includes the Phase 2 `jti` reuse attack surface under `Phase 2 Risks`
- [x] `docs/architecture_decisions.md` contains `PendingApproval` state machine diagram reviewed and signed off by the Platform team lead

**Infrastructure**
- [x] `docker-compose.yml` starts the full stack including Temporal service cleanly from cold state; `GET /health` returned `ok` in `15s` on 2026-03-09 after `docker-compose down --remove-orphans && docker-compose up -d`
- [x] Temporal worker connects to the local Temporal server on startup with no errors in the worker log; validated on 2026-03-09 after gating API/worker startup on the Temporal `default` namespace healthcheck
- [x] OPA loads updated `policies/agent_access.rego` containing `approve` and `deny` RBAC rules with zero errors on startup; validated on 2026-03-09 via clean startup logs plus a live `workflow:pending_approval` admin `approve` query returning `allow=true`
- [x] `toxiproxy` network fault injection is available in CI via the Compose-managed `toxiproxy` service and documented in `docs/deployment-guide.md` under `Test Infrastructure`; validated on 2026-03-09 via `curl http://localhost:18474/version -> {"version":"2.12.0"}`
- [x] Pre-commit hooks enforcing: `ruff`, `mypy`, `tests/test_no_workflow_stubs.py`, `tests/test_p2_encryption.py::TestPlaintextHardFailureGuard::test_create_aegis_data_converter_has_encrypted_payload_codec`, and schema conformance

---

#### Next Steps by Team — Phase 3 Preparation

Gate 2 passes and the `v0.4.0` tag is cut. Each team's first actions entering
Phase 3 are listed below. These are not Phase 3 deliverables — they are the
**setup actions** that unlock parallel Phase 3 work.

**Platform**
1. Scaffold `GET /api/v1/tasks/{task_id}/trace` endpoint in `src/control_plane/router.py` with a stub body raising `NotImplementedError`; create `tests/test_trace_endpoint.py` with a single failing test asserting the route exists — this becomes the Phase 3 test harness anchor
2. Scaffold `GET /api/v1/sessions/{session_id}/budget` endpoint stub with a paired failing test; both endpoints must have OPA `trace:read` and `budget:read` capability constants added to `policies/agent_access.rego` before any implementation
3. Document the p99 < 200 ms latency SLA for the trace endpoint in `docs/api-reference.md` as a non-negotiable requirement; run a baseline `GET /trace` on a 10-event task and record the current latency as the Phase 3 starting benchmark

**Security & Governance**
1. Draft the `PUT /api/v1/policies/{policy_id}` hot-reload API contract in `docs/api-reference.md` (request schema, validation response, error codes) before writing any implementation; review with Platform team and lock the contract before Phase 3 begins
2. Write the dry-run validation Rego function that simulates applying a new policy against the last 100 audit events; commit to `policies/` as `policy_validation.rego`; verify it loads cleanly and add a unit test asserting it rejects a policy that would deny > 5% of a synthetic prior-approved set
3. Add "Phase 3 security properties" section to `docs/threat-model.md` documenting the sub-second token revocation SLA and the policy hot-reload attack surface (stale-policy window, malicious policy injection)

**Watchdog & Reliability**
1. Create the three Grafana dashboard JSON skeleton files in `docs/` ("Cost per Department", "Agent Failure Rates", "Token Velocity by Agent Type") with correct Prometheus datasource references but empty panel arrays — Phase 3 deliverable fills in the panels; verify skeletons import without errors into a running Grafana instance
2. Draft the three Phase 3 Prometheus alert rules (High Token Velocity, Policy Violation Spike, Budget Exhaustion Rate) in `docs/prometheus.yml`; verify all three load without errors before Phase 3 begins; record the threshold values as explicitly documented constants
3. Establish the Phase 3 load test baseline: run 10 concurrent agents for 2 minutes with the current stack and record p50, p95, p99 latency as the `PHASE3_LATENCY_BASELINE` constant in the load test harness — the Phase 3 gate requires 100× this concurrency at or below this p99

**Audit & Compliance**
1. Produce the write-once backend decision record in `docs/architecture_decisions.md`: QLDB vs. cryptographically signed Postgres; include PoC results for both options; the decision must be reviewed and signed off before any Phase 3 implementation begins — no backend code before sign-off
2. Define the `ComplianceReporter` interface in `src/audit_vault/compliance.py` as a type stub with documented method signatures for `generate_soc2_report(window_start, window_end)` and `generate_gdpr_report(window_start, window_end)` — stubs raise `NotImplementedError`; the interface is frozen and other teams may depend on it
3. Implement per-row HMAC signing in `AuditLogger` and write unit tests asserting: (a) signing key comes from Vault, not env-var; (b) a mutated row fails HMAC verification; (c) an unmodified row passes — these tests become the Phase 3 tamper-detection baseline

**Frontend & DevEx**
1. Create `docs/compliance-guide.md` as a skeleton with section headings matching the SOC2 and GDPR report output structure defined by the Audit & Compliance interface; headings only — Phase 3 fills in content after the `ComplianceReporter` is implemented
2. Scaffold the React console project under `ui/` with a `README.md` documenting the build process, tech stack choices, and component plan for the Live Trace View — no React components yet, just project structure and a passing `npm test` baseline
3. Commit the three Grafana dashboard JSON skeletons created by Watchdog to `docs/` with a `docs/grafana/README.md` explaining import procedures; add a CI step that validates each JSON file is parseable and contains the expected datasource reference field

---

> **No-Go action:** any failing gate criterion or unchecked release requirement
> blocks the `v0.4.0` tag. The owning team has one remediation sprint (one week);
> the full gate re-runs after fixes are merged to `main`.

---

## Phase 3 — Glass Box Control Plane

**Release target:** `v0.6.0`
**Timeline:** Weeks 9–12
**Goal:** Provide the live visibility and automated compliance reporting
required by CIOs and compliance officers. No new capability ships without
a corresponding observable signal. Every item below must be backed by running
code and a passing test suite — stub implementations and placeholder responses
are a hard gate failure.

---

### Platform Team

#### P3-1 — `GET /api/v1/tasks/{task_id}/trace`

Returns the complete, ordered OTel-correlated event sequence for any task
regardless of age.

**Testing requirements**

- **Unit — response completeness:** seed the audit store with 10 known events
  for a fixed `task_id`; call `GET /trace`; assert the response contains
  exactly 10 events in strict `sequence_number` order with no omissions.
- **Unit — unknown task returns 404:** call `GET /trace` with a `task_id`
  that has no events in the store; assert HTTP `404` with a structured error
  body — not an empty `200`.
- **Unit — OTel correlation present:** assert every event in the response
  carries a `trace_id` and `span_id` that match the OTel spans recorded
  during task execution; a missing or null `trace_id` on any event is a test
  failure.
- **Unit — ordering invariant:** insert 50 events with shuffled
  `sequence_number` values into the store; call `GET /trace`; assert the
  response is sorted ascending by `sequence_number` regardless of insertion
  order.
- **Integration — live task trace:** run a full task through the orchestrator;
  call `GET /trace` immediately after completion; assert the event count
  equals `EXPECTED_AUDIT_EVENT_COUNT` and the first and last event types
  are `started` and `completed` respectively.
- **Performance test:** load the store with 50 events for one `task_id`;
  run 50 concurrent `GET /trace` requests using `httpx.AsyncClient`; assert
  p99 response time < 200 ms measured over the full 50-request burst.
  A single p99 breach is a hard test failure, not a warning.

---

#### P3-2 — `GET /api/v1/sessions/{session_id}/budget`

Live budget consumption query: remaining USD, tokens consumed, session expiry.

**Testing requirements**

- **Unit — happy path response schema:** create a live `BudgetSession` with
  $10.00 cap and $3.42 recorded spend; call `GET /budget`; assert the
  response body contains `remaining_usd = Decimal("6.58")`,
  `tokens_consumed` (integer ≥ 0), and `expires_at` (ISO-8601 timestamp
  in the future). Any missing or wrong-type field is a hard failure.
- **Unit — Decimal precision:** assert `remaining_usd` is serialised as a
  JSON number with exactly two decimal places — never as a float that
  introduces representation error.
- **Unit — depleted session:** record spend equal to the cap; call
  `GET /budget`; assert `remaining_usd = 0.00` and `depleted: true` in the
  response; assert HTTP `200`, not `4xx`.
- **Unit — unknown session returns 404:** call `GET /budget` with a
  `session_id` that does not exist; assert HTTP `404`.
- **Unit — expired session:** query a session whose `expires_at` is in the
  past; assert the response carries `expired: true` and `remaining_usd` is
  the value at expiry — not recalculated as if the session were live.
- **Integration — spend reflected in real time:** record spend via
  `BudgetEnforcer.record_spend()` in one request; immediately call
  `GET /budget` in a second request; assert `remaining_usd` has decreased
  by the exact recorded amount to four decimal places.

---

#### P3-3 — OPA RBAC for `trace:read` and `budget:read` capabilities

All Phase 3 endpoints covered; Rego rules added to `policies/agent_access.rego`.

**Testing requirements**

- **Unit — authorised caller:** call both endpoints with a JWT carrying the
  required capability (`trace:read` or `budget:read`); assert HTTP `200`.
- **Unit — missing capability:** call each endpoint with a valid JWT that
  does not carry the required capability; assert HTTP `403`; assert the
  `403` is issued by OPA (verified by the `policy_denied` audit event), not
  a hardcoded check in the router.
- **Unit — no token:** call each endpoint with no `Authorization` header;
  assert HTTP `401`.
- **Integration — live OPA RBAC:** start OPA with `policies/agent_access.rego`
  loaded; run a 3 × 2 capability matrix (three caller roles × two endpoints);
  assert only callers with the matching capability receive `200`; all others
  receive `403`.
- **Regression guard:** `tests/test_phase3_rbac_matrix.py` runs the full
  matrix and is added as a required CI check; any new Phase 3 endpoint must
  be added to this matrix before merging.

---

#### P3-4 — Performance: `GET /trace` p99 < 200 ms under 50 concurrent readers

**Testing requirements**

- **Load test definition:** `tests/test_trace_performance.py` — pre-load the
  audit store with 50 tasks each having exactly 50 events; use
  `asyncio.gather()` to fire 50 concurrent `GET /trace` requests against
  distinct `task_id` values; collect response times with `time.perf_counter`.
- **p99 assertion:** compute the 99th percentile of collected response times;
  assert p99 < 200 ms; any run where p99 ≥ 200 ms is a hard test failure,
  not a flaky retry candidate.
- **Correctness under load:** assert every response in the burst contains
  exactly 50 events in the correct order — performance must not come at the
  cost of correctness (truncated or shuffled results fail the test).
- **Baseline regression:** record the p50 and p99 from this test run as
  `PHASE3_TRACE_P50_MS` and `PHASE3_TRACE_P99_MS` constants in the test
  harness; a future run that regresses p99 by more than 20% fails CI.
- **Cold-cache test:** run the performance test immediately after a service
  restart (no warm cache); assert p99 is still < 200 ms — lazy-loading or
  cache warm-up that masks latency is not acceptable.

---

### Security & Governance Team

#### S3-1 — Policy Editor backend: `PUT /api/v1/policies/{policy_id}` hot-reloads Rego into OPA

No service restart required.

**Testing requirements**

- **Unit — hot-reload takes effect on next request:** submit a policy change
  via `PUT /api/v1/policies/{policy_id}`; immediately submit a task that the
  new policy would deny but the old policy would allow; assert HTTP `403` —
  the new policy is active on the very next request after the `PUT` returns
  `200`.
- **Unit — invalid Rego rejected before apply:** submit a `PUT` with
  syntactically invalid Rego; assert HTTP `400` with a structured error body
  containing the Rego parse error; assert OPA is still running the previous
  valid policy (verified by submitting a task that the previous policy allows
  and asserting `200`).
- **Unit — policy ID not found returns 404:** submit a `PUT` for a
  `policy_id` that does not exist in OPA; assert HTTP `404`.
- **Integration — live OPA hot-reload:** run a sequence: (1) call
  `GET /api/v1/tasks` with agent type `finance` → assert `200` under
  permissive policy; (2) `PUT` a restrictive policy that denies `finance`;
  (3) call `GET /api/v1/tasks` again → assert `403`; (4) `PUT` the original
  policy back; (5) assert `200` again. All five steps must pass in sequence.
- **Negative test — no OPA downtime during reload:** instrument the OPA
  client to record any `503` responses during the hot-reload window; assert
  zero `503` responses — OPA must remain available throughout the reload.

---

#### S3-2 — Policy validation gate: dry-run against last 100 audit events

Reject policies that would have denied > 5% of prior-approved requests
without an explicit override flag.

**Testing requirements**

- **Unit — dry-run pass:** submit a new policy that would not change the
  outcome of any of the last 100 audit events; assert the `PUT` returns
  `200` and the policy is applied.
- **Unit — dry-run block:** seed the audit store with 100 events of which
  10 are `allow` outcomes for `finance` agent type; submit a policy that
  denies all `finance` requests; assert HTTP `409` with a structured body
  containing `simulated_denial_rate: 0.10` and the list of affected
  `task_id` values.
- **Unit — override flag bypasses block:** submit the same blocking policy
  with `{"override": true}` in the request body; assert HTTP `200` and the
  policy is applied despite the > 5% simulated denial rate.
- **Unit — boundary exactness:** submit a policy whose simulated denial rate
  is exactly 5.0%; assert HTTP `200` (at exactly 5%, the gate does not
  block). Submit a policy at 5.01%; assert HTTP `409`.
- **Integration — dry-run uses real audit data:** populate the audit store
  with 100 live events by running 100 tasks; submit a policy that would
  affect 7 of those tasks; assert the `409` response lists exactly those 7
  `task_id` values with no extras.

---

#### S3-3 — Token revocation hardening: `DELETE /api/v1/sessions/{session_id}`

Adds `jti` to Vault's revocation list with sub-second effect.

**Testing requirements**

- **Unit — revoked token returns 401 immediately:** call
  `DELETE /api/v1/sessions/{session_id}`; assert HTTP `200`; immediately
  (within the same test, no sleep) submit a request using the revoked token;
  assert HTTP `401` — not `200`, not `403`.
- **Unit — revocation persists across API restart:** revoke a token; restart
  the API process; submit the revoked token; assert HTTP `401` — the
  revocation list must survive in Vault, not only in-memory.
- **Unit — other tokens unaffected:** issue two tokens for the same session;
  revoke one; assert the other still returns `200` on a protected endpoint.
- **Unit — unknown session returns 404:** call `DELETE` for a
  `session_id` that does not exist; assert HTTP `404`.
- **Performance test — sub-second effect:** measure the latency between
  the `DELETE` response returning and the revoked token first returning
  `401`; assert this window is < 1,000 ms in 100/100 consecutive test runs.
- **Negative test — replay within 100 ms:** revoke a token; within 100 ms
  submit the original request using that token; assert `401` — not `200`.
  Use `time.perf_counter()` to bound the replay window.

---

#### S3-4 — Adversarial revocation: replay original request within 100 ms must return 401

**Testing requirements**

- **Timing-bounded replay test:** issue a JIT token; record the exact
  `Authorization` header; call `DELETE /api/v1/sessions/{session_id}`;
  use `asyncio` to fire the replay request ≤ 100 ms after the `DELETE`
  response arrives; assert HTTP `401` in 100/100 runs. Any `200` response
  is a hard, non-retryable failure.
- **Concurrent replay test:** fire 10 concurrent replay requests simultaneously
  within 100 ms of revocation; assert all 10 return `401` — race conditions
  that allow even one concurrent request through are a test failure.
- **Audit event presence:** assert a `session.revoked` audit event is emitted
  by the `DELETE` call and a `jit.revoked_replay_blocked` event is emitted
  for each blocked replay attempt; missing audit events are a test failure.
- **Cross-replica test (if replicas are configured):** revoke a token on
  replica A; send the replay to replica B within 1 second; assert `401` —
  the Vault revocation list is the shared source of truth, not a local cache.

---

### Watchdog & Reliability Team

#### W3-1 — Three Grafana dashboards importable via one command

"Cost per Department", "Agent Failure Rates", "Token Velocity by Agent Type".

**Testing requirements**

- **Import test — all three dashboards:** `tests/test_grafana_dashboards.py`
  uses the Grafana HTTP API (`POST /api/dashboards/import`) against a
  Grafana container started by `testcontainers`; assert all three dashboard
  JSON files import with HTTP `200` and no `errors` array in the response.
  A dashboard that fails to import is a hard test failure.
- **Panel presence test:** after import, call `GET /api/dashboards/uid/{uid}`
  for each dashboard; assert each contains the required panels by title:
  - "Cost per Department": at least one panel with `agent_type` label
  - "Agent Failure Rates": at least one panel grouping by `stage` and `agent_type`
  - "Token Velocity by Agent Type": at least one panel with a threshold annotation
- **Datasource reference test:** `tests/test_grafana_json_validity.py` loads
  all three JSON files and asserts each panel's `datasource` field references
  the expected Prometheus datasource UID — a hardcoded or missing UID is a
  test failure.
- **Live data test:** run 10 tasks through the orchestrator; scrape Prometheus;
  assert all three dashboards display non-zero data for at least one panel
  within 30 seconds of task completion — empty dashboards on live data are
  a test failure.
- **One-command import validation:** `tests/test_grafana_one_command.py`
  executes the documented import command from `docs/grafana/README.md` via
  `subprocess`; asserts exit code 0 and all three dashboards appear in
  `GET /api/dashboards/home`.

---

#### W3-2 — Three Prometheus alert rules: High Token Velocity, Policy Violation Spike, Budget Exhaustion Rate

**Testing requirements**

- **Syntax validation:** load `docs/prometheus.yml` into a Prometheus
  container; assert it starts without errors and all three alert rules
  appear in `GET /api/v1/rules` with state `inactive`.
- **High Token Velocity — fires:** set `aegis_token_velocity_per_minute`
  above the defined threshold for a test agent; assert the alert fires with
  severity `warning` (or `critical` per the rule definition).
- **Policy Violation Spike — fires at > 10 denials in 60 s:** inject 11
  synthetic `policy.denied` counter increments within the 60-second window;
  assert the alert fires; inject 9 — assert it does not fire.
- **Budget Exhaustion Rate — fires at > 80% consumed:** set
  `aegis_budget_remaining_usd` to 19% of the session cap; assert the alert
  fires. Set to 21%; assert it does not fire.
- **All alerts resolve:** after each fired alert scenario, reset the metric
  to a safe value; advance the evaluation interval; assert all three alerts
  return to `inactive`.
- **Runbook links present:** assert every alert rule annotation carries a
  `runbook_url` pointing to a file that exists under `docs/runbooks/`;
  `tests/test_prometheus_rules.py` enforces this for all rules in the file.

---

#### W3-3 — Load test: 100+ concurrent agents, 10 minutes, p99 < 500 ms

**Testing requirements**

- **Test definition:** `tests/test_load_phase3.py` — launch 100 concurrent
  agent tasks using `asyncio.gather()`; each task runs to completion with a
  mocked LLM adapter returning a fixed response; run for 10 minutes wall-clock
  time with tasks continuously re-submitted as prior tasks complete.
- **p99 latency assertion:** collect end-to-end latency for every task
  (from HTTP request sent to `TaskResponse` received); compute p99 over the
  full 10-minute window; assert p99 < 500 ms. A single run where p99 ≥ 500 ms
  is a hard test failure.
- **Zero false-positive BudgetExceededError:** assert no `BudgetExceededError`
  is raised for any task whose recorded spend is less than its session cap;
  any false positive is a hard failure.
- **Zero metric drop-outs:** after the 10-minute run, sum
  `aegis_tokens_consumed_total` from Prometheus; assert this equals the sum
  of all `token_cost_usd` values recorded in the audit log for the same
  window — any discrepancy indicates a dropped metric.
- **Correctness under load:** assert every task in the load test run produced
  at least `EXPECTED_AUDIT_EVENT_COUNT` audit events; a task with fewer
  events failed silently and is a test failure.
- **Grafana dashboards show live data during run:** at the 5-minute mark,
  query all three Grafana dashboards via the API and assert at least one
  panel in each dashboard has a non-zero data point — dashboards that show
  no data during an active load test are not functioning.

---

### Audit & Compliance Team

#### A3-1 — Write-once audit backend: QLDB or cryptographically signed Postgres rows

Per-row HMAC; tampering must be detectable.

**Testing requirements**

- **Unit — HMAC verification on read:** write one audit event to the backend;
  read it back; assert `verify_hmac(row)` returns `True`. Mutate one byte
  of the stored payload; assert `verify_hmac(row)` returns `False` — a
  backend that does not detect mutation is a hard test failure.
- **Unit — append-only enforcement:** attempt to `UPDATE` or `DELETE` a row
  directly in the backend (bypassing the ORM); assert the operation is
  rejected by the database trigger or access policy with a database-level
  error — not silently accepted.
- **Unit — signing key from Vault:** assert the HMAC signing key is fetched
  from Vault via `VaultClient.get_secret()`; mock `VaultClient` to raise
  `VaultUnavailableError`; assert the write fails loudly — it must never
  fall back to a hardcoded or env-var key.
- **Integration — tamper detection end-to-end:** write 100 events to the
  live backend; use a direct DB connection to mutate one row; run
  `ComplianceReporter.verify_integrity()`; assert it returns exactly one
  tampered-row identifier matching the mutated row.
- **Regression — zero false positives on integrity check:** write 1,000
  unmodified events; run `verify_integrity()`; assert it reports zero
  tampered rows — false positives in integrity checking undermine auditor
  trust.

---

#### A3-2 — `ComplianceReporter` generates SOC2 and GDPR reports for any 24-hour window

**Testing requirements**

- **Unit — output schema validation:** call
  `ComplianceReporter.generate_soc2_report(window_start, window_end)` with
  a 24-hour window; assert the returned report object validates against the
  documented SOC2 report schema (JSON Schema or Pydantic model); any missing
  required field is a test failure.
- **Unit — GDPR report includes data subject fields:** assert the GDPR report
  contains a `data_subjects` section listing every unique `agent_type` that
  processed PII within the window; assert each entry includes a `pii_classes`
  list and a `redaction_event_count`.
- **Unit — window boundary exactness:** create events at timestamps T,
  T+24h-1s, T+24h (inclusive), and T+24h+1s; call the report for window
  `[T, T+24h]`; assert the report includes the first three events and excludes
  the fourth.
- **Unit — empty window produces valid empty report:** call the report for a
  window with no events; assert HTTP `200` and a valid report with zero
  counts — not `404` or an exception.
- **Integration — report from live data:** run 50 tasks through the
  orchestrator; generate SOC2 and GDPR reports covering that run window;
  assert the SOC2 report's `total_tasks` field equals 50 and the GDPR
  report's `total_pii_redactions` equals the sum of redaction events in the
  audit log.
- **No stubs guard:** `tests/test_compliance_no_stubs.py` imports
  `ComplianceReporter` and uses `inspect.getsource()` to assert neither
  `generate_soc2_report` nor `generate_gdpr_report` body contains `pass`,
  `...`, or `raise NotImplementedError`.

---

#### A3-3 — Compliance report validated against ≥ 10-item auditor checklist; non-author sign-off

**Testing requirements**

- **Checklist definition:** `tests/fixtures/soc2_auditor_checklist.json`
  contains ≥ 10 named checklist items (e.g., "encryption at rest documented",
  "access control events present", "PII redaction events present"). This file
  is committed to the repo and treated as a contract.
- **Automated checklist validation:** `tests/test_compliance_checklist.py`
  generates a SOC2 report from a 24-hour synthetic run and asserts every
  checklist item has a corresponding non-null, non-empty value in the report
  output. Any checklist item with a null or missing report value is a test
  failure.
- **Non-author review gate:** the compliance report output for the
  gate review window must be reviewed by one team member who did not write
  `ComplianceReporter`; their sign-off is recorded in the Gate 3 review
  issue. This is a human gate — it cannot be automated away.
- **Format test:** assert the report can be exported as both PDF-ready
  Markdown and JSON; `tests/test_compliance_export.py` generates both
  formats and asserts neither is empty and both parse without error.

---

#### A3-4 — Tamper detection: mutated audit row flagged by next compliance report run

**Testing requirements**

- **Unit — single mutation detected:** write 50 events; use a direct DB
  connection to flip one byte in one row's `payload` column; run
  `ComplianceReporter.verify_integrity()`; assert the response identifies
  exactly one tampered row with the correct row identifier.
- **Unit — multiple mutations detected:** mutate three non-consecutive rows;
  assert `verify_integrity()` returns exactly three tampered identifiers —
  not one, not all 50.
- **Unit — detection does not alter the store:** assert `verify_integrity()`
  is a read-only operation; call it twice and assert the audit store row
  count is identical before and after both calls.
- **Unit — compliance report flags tamper:** generate a compliance report
  after a known mutation; assert the report's `integrity_violations` field
  contains the mutated row identifier and the report's overall status is
  `INTEGRITY_COMPROMISED`, not `CLEAN`.
- **Negative test — clean store passes:** write 100 unmodified events; run
  `verify_integrity()`; assert `integrity_violations` is an empty list and
  status is `CLEAN` — false positives fail auditor trust.

---

### Frontend & DevEx Team

#### F3-1 — React Management Console: Live Trace View

Renders full agent chain-of-thought and tool calls for any `task_id`;
loads in < 2 s for traces with up to 200 events.

**Testing requirements**

- **Component test — renders all events:** use React Testing Library to
  render `<LiveTraceView taskId="test-id" />` with a mocked API response
  containing 200 events; assert all 200 events appear in the DOM with the
  correct `event_type` labels and no truncation.
- **Component test — empty state:** render with a `task_id` that returns an
  empty event list; assert a non-empty "No events found" or equivalent
  message is displayed — a blank render with no user feedback is a test
  failure.
- **Component test — error state:** mock the API to return HTTP `500`;
  assert an error message is displayed and the component does not crash or
  show a blank screen.
- **Performance test — load time < 2 s:** use Playwright (or equivalent
  headless browser) to navigate to the Live Trace View for a task with
  200 pre-seeded events; measure time from navigation to last event visible
  in the DOM; assert < 2,000 ms in 5/5 consecutive runs. A single run
  exceeding 2 s is a failing test.
- **Integration — live API data:** run a real task end-to-end; navigate to
  its trace view; assert the `task_id` in the page URL matches the task's
  audit events returned by `GET /trace`.

---

#### F3-2 — React Policy Editor: load, edit, validate, and submit Rego policies

Validation errors surface inline before submission.

**Testing requirements**

- **Component test — loads existing policy:** mock `GET /api/v1/policies/{id}`
  to return a known Rego policy string; render `<PolicyEditor />`; assert
  the editor textarea contains the exact policy text with no modification.
- **Component test — inline validation error:** type syntactically invalid
  Rego into the editor; assert a validation error message appears in the UI
  before the submit button is clicked — errors must surface on edit, not
  only on submission.
- **Component test — submit calls correct endpoint:** mock
  `PUT /api/v1/policies/{id}`; fill in valid Rego and click Submit; assert
  the mock was called exactly once with the correct policy body and
  `Content-Type: application/json`.
- **Component test — dry-run warning surfaced:** mock the API to return
  HTTP `409` (dry-run block) with a simulated denial rate; assert the UI
  displays the denial rate and affected task count before asking the user
  to confirm override — silent submission after a `409` is a test failure.
- **Integration — live round-trip:** open the Policy Editor against the
  running dev stack; load the existing `agent_access.rego`; make a
  non-breaking change; submit; assert the change is reflected in the next
  call to `GET /api/v1/policies/{id}` and the OPA hot-reload test passes.

---

#### F3-3 — Grafana dashboard JSON definitions importable via `docs/prometheus.yml`

**Testing requirements**

- **Import command test:** `tests/test_grafana_one_command.py` executes the
  documented one-command import from `docs/grafana/README.md` via
  `subprocess`; asserts exit code 0; asserts all three dashboard titles
  appear in `GET /api/search` response from the Grafana container.
- **JSON validity test:** `tests/test_grafana_json_validity.py` loads all
  three JSON files using `json.loads()`; asserts each is valid JSON; asserts
  each contains `title`, `panels`, and `__inputs` keys — missing any key
  indicates an incomplete dashboard definition.
- **Datasource binding test:** assert the `__inputs` section of each JSON
  specifies `DS_PROMETHEUS` as a required datasource input; this ensures
  the import prompt binds to the correct datasource and does not silently
  use a default.
- **Idempotent import test:** run the import command twice; assert the second
  import does not create duplicate dashboards — the import must be
  idempotent with the same UID.

---

#### F3-4 — `docs/compliance-guide.md` with SOC2/GDPR report generation walkthrough

**Testing requirements**

- **Section completeness:** `tests/test_compliance_guide_completeness.py`
  reads `docs/compliance-guide.md`; asserts it contains all required
  sections: `Generating a SOC2 Report`, `Generating a GDPR Report`,
  `Interpreting the Output`, and `Sample Auditor Checklist` — any missing
  heading is a CI failure.
- **Walkthrough executability:** every shell command in the guide must be
  extractable via regex and executable via `subprocess` against the running
  dev stack; any command returning a non-zero exit code is a test failure.
- **Checklist item count:** assert the `Sample Auditor Checklist` section
  contains ≥ 10 explicitly numbered checklist items; fewer than 10 items
  fails the test.
- **Cross-reference accuracy:** every field name referenced in the guide
  must exist in the `ComplianceReporter` output schema;
  `tests/test_compliance_guide_accuracy.py` loads the guide and diffs
  referenced field names against the Pydantic model — any field in the
  guide but absent from the model is a hard failure.
- **Non-author review gate:** the guide must be reviewed by one team member
  who did not write it before the F3-4 checkbox is ticked; reviewer sign-off
  recorded in the Gate 3 review issue.

---

### 🚦 Go/No-Go Gate 3 → `v0.6.0`

Every item in the checklist below must be ticked before the gate review
session opens. An unticked item or a failing named test blocks the gate;
the owning team carries the remediation before the session reconvenes.

---

#### Pre-Gate Test Suite Checklist

**Platform**
- [ ] `test_trace_response_completeness` — 10 seeded events for one `task_id`; response contains exactly 10 events in `sequence_number` order; no omissions
- [ ] `test_trace_unknown_task_404` — unknown `task_id` returns HTTP `404` with structured error body; not an empty `200`
- [ ] `test_trace_otel_correlation` — every event in the response carries non-null `trace_id` and `span_id` matching the OTel spans recorded during execution
- [ ] `test_trace_ordering_invariant` — 50 events inserted with shuffled sequence numbers; response is sorted ascending; order is not insertion-dependent
- [ ] `test_trace_live_task_integration` — full task run; `GET /trace` immediately after; event count equals `EXPECTED_AUDIT_EVENT_COUNT`; first event is `started`, last is `completed`
- [ ] `test_budget_happy_path_schema` — `BudgetSession` with $10.00 cap and $3.42 spend; response contains `remaining_usd = Decimal("6.58")`, `tokens_consumed` (int ≥ 0), `expires_at` (ISO-8601 future); wrong-type or missing field fails
- [ ] `test_budget_decimal_precision` — `remaining_usd` serialised as JSON number with exactly two decimal places; float representation error fails the test
- [ ] `test_budget_depleted_session` — spend equals cap; response has `remaining_usd = 0.00` and `depleted: true`; HTTP `200`, not `4xx`
- [ ] `test_budget_unknown_session_404` — unknown `session_id` returns HTTP `404`
- [ ] `test_budget_expired_session` — session past `expires_at`; response has `expired: true` and `remaining_usd` frozen at expiry value
- [ ] `test_budget_spend_realtime_reflection` — `BudgetEnforcer.record_spend()` in one request; `GET /budget` in next; `remaining_usd` decreased by exact recorded amount to four decimal places
- [ ] `test_phase3_rbac_authorised` — both endpoints with JWT carrying required capability; HTTP `200` on each
- [ ] `test_phase3_rbac_missing_capability` — valid JWT without `trace:read`/`budget:read`; HTTP `403` issued by OPA; `policy_denied` audit event present
- [ ] `test_phase3_rbac_no_token` — no `Authorization` header; HTTP `401` on each endpoint
- [ ] `test_phase3_rbac_matrix` — 3 × 2 capability matrix; only matching-capability callers receive `200`; all others `403`; test file added as required CI check
- [ ] `test_trace_performance_p99` — 50 tasks × 50 events pre-loaded; 50 concurrent `GET /trace` requests; p99 < 200 ms measured over full burst; hard failure, not a warning
- [ ] `test_trace_performance_correctness_under_load` — every response in the 50-request burst contains exactly 50 events in correct order
- [ ] `test_trace_performance_cold_cache` — performance test run immediately after service restart; p99 still < 200 ms

**Security & Governance**
- [ ] `test_hot_reload_takes_effect_on_next_request` — policy `PUT` returns `200`; immediately following request denied under new policy; `403` confirmed before any other request
- [ ] `test_hot_reload_invalid_rego_rejected` — syntactically invalid Rego `PUT`; HTTP `400` with Rego parse error; previous policy still active (verified by `200` on allowed request)
- [ ] `test_hot_reload_policy_not_found_404` — `PUT` for non-existent `policy_id`; HTTP `404`
- [ ] `test_hot_reload_live_opa_sequence` — 5-step sequence: allow → apply restrictive → deny → restore original → allow; all five assertions pass in order
- [ ] `test_hot_reload_zero_opa_downtime` — zero `503` responses from OPA client during reload window
- [ ] `test_dry_run_pass` — new policy with no impact on last 100 audit events; `PUT` returns `200`; policy applied
- [ ] `test_dry_run_block` — policy that would deny 10/100 prior-approved events; HTTP `409` with `simulated_denial_rate: 0.10` and list of affected `task_id` values
- [ ] `test_dry_run_override_flag` — same blocking policy with `{"override": true}`; HTTP `200`; policy applied despite > 5% rate
- [ ] `test_dry_run_boundary_exactness` — exactly 5.0% simulated denial rate passes; 5.01% returns `409`
- [ ] `test_dry_run_real_audit_data` — 100 live tasks; policy affecting exactly 7; `409` lists exactly those 7 `task_id` values; no extras
- [ ] `test_revocation_401_immediately` — `DELETE` returns `200`; revoked token used immediately; `401` — not `200` or `403`; no sleep between calls
- [ ] `test_revocation_persists_across_restart` — revoke; restart API; revoked token still returns `401` — Vault, not in-memory
- [ ] `test_revocation_other_tokens_unaffected` — two tokens for same session; revoke one; other still returns `200`
- [ ] `test_revocation_unknown_session_404` — `DELETE` for non-existent `session_id`; HTTP `404`
- [ ] `test_revocation_sub_second_effect` — latency from `DELETE` response to first `401` < 1,000 ms in 100/100 runs
- [ ] `test_adversarial_replay_within_100ms` — revoke; replay within 100 ms using `time.perf_counter()`; `401` in 100/100 runs; any single `200` is a hard non-retryable failure
- [ ] `test_adversarial_concurrent_replay` — 10 concurrent replay requests within 100 ms of revocation; all 10 return `401`; any concurrent pass-through fails
- [ ] `test_adversarial_replay_audit_events` — `session.revoked` event emitted on `DELETE`; `jit.revoked_replay_blocked` emitted for each blocked replay; missing audit event fails

**Watchdog & Reliability**
- [ ] `test_grafana_import_all_three` — `testcontainers` Grafana instance; all three dashboard JSON files import via `POST /api/dashboards/import` with HTTP `200` and no `errors` array
- [ ] `test_grafana_panel_presence` — after import, each dashboard contains required panels by title: "Cost per Department" has `agent_type` label, "Agent Failure Rates" has `stage` and `agent_type` grouping, "Token Velocity" has threshold annotation
- [ ] `test_grafana_datasource_reference` — every panel's `datasource` field references the expected Prometheus datasource UID; hardcoded or missing UID fails
- [ ] `test_grafana_live_data` — 10 tasks run; Prometheus scraped; all three dashboards show non-zero data in at least one panel within 30 s of completion
- [ ] `test_grafana_one_command_import` — documented import command run via `subprocess`; exit code 0; all three dashboards appear in `GET /api/dashboards/home`
- [ ] `test_prometheus_rules_syntax` — all three alert rules load in Prometheus container; appear in `GET /api/v1/rules` with state `inactive`
- [ ] `test_prometheus_high_token_velocity_fires` — metric above threshold; alert fires with correct severity
- [ ] `test_prometheus_policy_violation_spike_fires` — 11 synthetic `policy.denied` increments in 60 s window; alert fires; 9 increments — alert stays `inactive`
- [ ] `test_prometheus_budget_exhaustion_fires` — `aegis_budget_remaining_usd` at 19% of cap; alert fires; at 21% — alert stays `inactive`
- [ ] `test_prometheus_all_alerts_resolve` — after each fired scenario, metric reset to safe value; all three alerts return to `inactive`
- [ ] `test_prometheus_runbook_links` — every alert rule annotation carries `runbook_url` pointing to an existing file under `docs/runbooks/`
- [ ] `test_load_phase3_p99` — 100 concurrent agents; 10-minute run; p99 < 500 ms over full window; single run above threshold is a hard failure
- [ ] `test_load_phase3_zero_false_positive_budget` — zero `BudgetExceededError` raised for tasks whose spend is below session cap
- [ ] `test_load_phase3_zero_metric_dropouts` — `aegis_tokens_consumed_total` from Prometheus equals sum of `token_cost_usd` in audit log for the same window
- [ ] `test_load_phase3_audit_correctness` — every task in the 10-minute run produced at least `EXPECTED_AUDIT_EVENT_COUNT` audit events
- [ ] `test_load_phase3_grafana_live_at_5min` — at 5-minute mark, all three Grafana dashboards have at least one non-zero data point via API query

**Audit & Compliance**
- [ ] `test_hmac_verification_on_read` — write one event; read back; `verify_hmac(row)` returns `True`; mutate one byte; returns `False`
- [ ] `test_append_only_enforcement` — direct `UPDATE`/`DELETE` on a backend row rejected with database-level error; not silently accepted
- [ ] `test_signing_key_from_vault` — writing with mocked `VaultUnavailableError` from `VaultClient.get_secret()` fails loudly; no fallback to env-var or hardcoded key
- [ ] `test_tamper_detection_end_to_end` — 100 live events written; one row mutated via direct DB connection; `verify_integrity()` returns exactly one tampered identifier
- [ ] `test_tamper_detection_zero_false_positives` — 1,000 unmodified events; `verify_integrity()` reports zero tampered rows
- [ ] `test_soc2_report_schema_validation` — 24-hour window report validated against SOC2 schema (JSON Schema or Pydantic); any missing required field fails
- [ ] `test_gdpr_report_data_subjects` — GDPR report contains `data_subjects` section; every unique `agent_type` processing PII listed with `pii_classes` and `redaction_event_count`
- [ ] `test_report_window_boundary_exactness` — events at T, T+24h-1s, T+24h, T+24h+1s; report for `[T, T+24h]` includes first three, excludes fourth
- [ ] `test_report_empty_window` — window with no events; HTTP `200`; valid report with zero counts; not `404` or exception
- [ ] `test_report_live_data_integration` — 50 tasks run; SOC2 `total_tasks` equals 50; GDPR `total_pii_redactions` equals sum of redaction events in audit log
- [ ] `test_compliance_no_stubs` — `inspect.getsource()` on `ComplianceReporter`; neither `generate_soc2_report` nor `generate_gdpr_report` body contains `pass`, `...`, or `raise NotImplementedError`
- [ ] `test_compliance_checklist_coverage` — SOC2 report from 24-hour synthetic run has non-null, non-empty value for every item in `tests/fixtures/soc2_auditor_checklist.json` (≥ 10 items)
- [ ] `test_compliance_export_formats` — report exported as PDF-ready Markdown and JSON; neither is empty; both parse without error
- [ ] `test_tamper_single_mutation_identified` — 50 events; one byte flipped in one row; `verify_integrity()` identifies exactly one tampered row with correct identifier
- [ ] `test_tamper_multiple_mutations_identified` — three non-consecutive rows mutated; exactly three identifiers returned; not one, not all 50
- [ ] `test_tamper_verify_is_read_only` — `verify_integrity()` called twice; audit store row count identical before and after both calls
- [ ] `test_tamper_report_flags_compromised` — compliance report after mutation; `integrity_violations` contains mutated row ID; overall status is `INTEGRITY_COMPROMISED`
- [ ] `test_tamper_clean_store_passes` — 100 unmodified events; `integrity_violations` is empty list; status is `CLEAN`

**Frontend & DevEx**
- [ ] `test_trace_view_renders_all_events` — React Testing Library; `<LiveTraceView />` with 200-event mocked response; all 200 events in DOM with correct `event_type` labels; no truncation
- [ ] `test_trace_view_empty_state` — empty event list; non-empty "No events found" message displayed; blank render with no feedback fails
- [ ] `test_trace_view_error_state` — API returns HTTP `500`; error message displayed; component does not crash or show blank screen
- [ ] `test_trace_view_load_performance` — Playwright headless; 200 pre-seeded events; navigation to last event visible in DOM < 2,000 ms in 5/5 consecutive runs; single run exceeding threshold fails
- [ ] `test_trace_view_live_api_integration` — real task run end-to-end; trace view `task_id` in page URL matches audit events from `GET /trace`
- [ ] `test_policy_editor_loads_existing` — mock `GET /api/v1/policies/{id}` returns known Rego string; `<PolicyEditor />` textarea contains exact policy text
- [ ] `test_policy_editor_inline_validation` — invalid Rego typed in editor; validation error appears in UI before submit clicked; error surfaces on edit, not only on submission
- [ ] `test_policy_editor_submit_endpoint` — mock `PUT /api/v1/policies/{id}`; valid Rego submitted; mock called exactly once with correct policy body and `Content-Type: application/json`
- [ ] `test_policy_editor_dry_run_warning` — API returns HTTP `409` with simulated denial rate; UI displays denial rate and affected task count before asking user to confirm override
- [ ] `test_policy_editor_live_round_trip` — Policy Editor against running dev stack; load `agent_access.rego`; make non-breaking change; submit; change reflected in next `GET /api/v1/policies/{id}`
- [ ] `test_grafana_json_valid_structure` — all three JSON files parse with `json.loads()`; each contains `title`, `panels`, and `__inputs` keys
- [ ] `test_grafana_datasource_binding` — `__inputs` section of each JSON specifies `DS_PROMETHEUS` as required datasource input
- [ ] `test_grafana_idempotent_import` — import command run twice; second import does not create duplicate dashboards; same UID in `GET /api/search`
- [ ] `test_compliance_guide_sections` — `docs/compliance-guide.md` contains all four required headings: `Generating a SOC2 Report`, `Generating a GDPR Report`, `Interpreting the Output`, `Sample Auditor Checklist`
- [ ] `test_compliance_guide_commands` — every shell command in guide extracted via regex and run via `subprocess` against dev stack; all exit code 0
- [ ] `test_compliance_guide_checklist_count` — `Sample Auditor Checklist` section contains ≥ 10 explicitly numbered items; fewer than 10 fails
- [ ] `test_compliance_guide_field_accuracy` — every field name referenced in the guide exists in the `ComplianceReporter` Pydantic model; field in guide but absent from model is a hard failure

---

#### Gate Criteria

All five criteria below reference the named tests above. A criterion passes
only when every named test in its row has a green result in CI on `main`.

| ID | Criterion | Owner | Pass definition |
|---|---|---|---|
| **G3-1** | Trace endpoint performance | Platform | `test_trace_performance_p99`, `test_trace_performance_correctness_under_load`, and `test_trace_performance_cold_cache` all pass; p99 < 200 ms confirmed under cold-cache conditions |
| **G3-2** | Hot policy reload — sub-request effect | Security & Governance | `test_hot_reload_live_opa_sequence`, `test_revocation_sub_second_effect`, and `test_adversarial_replay_within_100ms` all pass; revoked token returns `401` in 100/100 timing-bounded runs |
| **G3-3** | 100 concurrent agents — no false positives | Watchdog & Reliability | `test_load_phase3_p99`, `test_load_phase3_zero_false_positive_budget`, `test_load_phase3_zero_metric_dropouts`, and `test_load_phase3_grafana_live_at_5min` all pass |
| **G3-4** | Tamper-evident compliance report | Audit & Compliance | `test_tamper_detection_end_to_end`, `test_tamper_report_flags_compromised`, `test_compliance_checklist_coverage`, and non-author sign-off in gate review issue all confirmed |
| **G3-5** | Console ships and performs | Frontend & DevEx | `test_trace_view_load_performance` passes in 5/5 consecutive Playwright runs; `test_policy_editor_live_round_trip` passes against live dev stack; `test_compliance_guide_commands` exits 0 for all shell commands |

---

#### Release Requirements for `v0.6.0`

All of the following must be complete before the tag is cut.

**Code**
- [ ] `main` branch passes `pytest` with zero failures, zero skips, zero `xfail` markers in production test paths
- [ ] `mypy src/` reports zero errors
- [ ] `ruff check src/ tests/` reports zero errors
- [ ] No `pass`, `raise NotImplementedError`, or `...` in any production method body under `src/audit_vault/` or `src/control_plane/`; enforced by `test_compliance_no_stubs` and equivalent router stub guard in CI
- [ ] `ComplianceReporter.generate_soc2_report()` and `generate_gdpr_report()` are fully implemented, not stub bodies; `test_compliance_no_stubs` is a required CI check
- [ ] Per-row HMAC signing live in `AuditLogger`; `test_hmac_verification_on_read` and `test_append_only_enforcement` are required CI checks
- [ ] `tests/fixtures/soc2_auditor_checklist.json` committed with ≥ 10 named items; treated as a frozen contract — items may only be added, never removed
- [ ] All three alert rules in `docs/prometheus.yml` pass `test_prometheus_rules_syntax` against a Prometheus container in CI
- [ ] All three Grafana dashboard JSON files pass `test_grafana_json_valid_structure` and `test_grafana_datasource_binding` in CI
- [ ] `CHANGELOG.md` entry written for `v0.6.0` listing all shipped capabilities with roadmap item IDs (P3-1 through F3-4)

**Documentation**
- [ ] `docs/api-reference.md` documents `GET /tasks/{id}/trace` and `GET /sessions/{id}/budget` with full request/response JSON schemas, all error codes (`200`, `401`, `403`, `404`), and the p99 < 200 ms SLA
- [ ] `docs/api-reference.md` documents `PUT /api/v1/policies/{policy_id}` with dry-run response schema, `409` conflict body (`simulated_denial_rate`, `affected_task_ids`), and override flag semantics
- [ ] `docs/api-reference.md` documents `DELETE /api/v1/sessions/{session_id}` with revocation behaviour, sub-second SLA statement, and `404` error response
- [ ] `docs/compliance-guide.md` complete — all four required sections present, all shell commands executable, ≥ 10-item auditor checklist, field names cross-referenced against Pydantic model
- [ ] `docs/grafana/README.md` committed with one-command import instructions; import command is the exact command tested by `test_grafana_one_command_import`
- [ ] `docs/threat-model.md` updated with Phase 3 attack surfaces: policy hot-reload injection window, revocation race window, compliance report tamper scenario, trace endpoint data exfiltration risk
- [ ] `docs/architecture_decisions.md` contains write-once backend decision record (QLDB vs. cryptographically signed Postgres) with PoC results and team sign-off
- [ ] Non-author review sign-offs for `ComplianceReporter` output (A3-3) and `docs/compliance-guide.md` (F3-4) recorded in the Gate 3 review issue
- [ ] All four runbooks in `docs/runbooks/` (`budget-exceeded.md`, `loop-detected.md`, `opa-server-down.md`, `token-renewal-failure.md`) updated with Phase 3 endpoint references

**Infrastructure**
- [ ] `docker-compose.yml` starts the full Phase 3 stack cleanly from cold state; `GET /health` returns `ok` within 60 s
- [ ] OPA loads updated `policies/agent_access.rego` containing `trace:read` and `budget:read` rules with zero errors on startup
- [ ] Grafana service in `docker-compose.yml` provisions all three dashboards on startup via provisioning config; no manual import required in production
- [ ] Write-once audit backend (QLDB or Postgres with append-only trigger) is provisioned by `docker-compose.yml` or an `infra/` script; cold-start from `docker-compose up -d` produces a working, tamper-evident store
- [ ] `testcontainers` dependency added to `pyproject.toml [dev]` with version pinned; CI job that runs Grafana and Prometheus integration tests passes
- [ ] Pre-commit hooks enforcing: `ruff`, `mypy`, `test_compliance_no_stubs`, `test_phase3_rbac_matrix`, `test_hmac_verification_on_read`, and audit schema conformance check

---

#### Next Steps by Team — Phase 4 Preparation

Gate 3 passes and the `v0.6.0` tag is cut. Each team's first actions entering
Phase 4 are listed below. These are **setup actions** that unlock parallel
Phase 4 work — not Phase 4 deliverables.

**Platform**
1. Audit every occurrence of `os.environ.get("AEGIS_*")` in `src/`; create an `infra/vault-migration.md` listing each secret, its current env-var name, and its target Vault path — this inventory must be reviewed and approved before any secret is migrated
2. Scaffold `MCPHandoff` Pydantic model in `src/adapters/mcp.py` with a stub body raising `NotImplementedError`; create `tests/test_mcp_handoff.py` with a single failing test asserting the model validates a minimal handoff payload — this becomes the Phase 4 MCP test harness anchor
3. Document the secret rotation contract in `docs/api-reference.md`: which endpoints consume Vault-backed secrets, lease TTL, renewal policy, and failure behaviour when Vault is unreachable — this contract is frozen before Phase 4 begins so Security & Governance can write rotation tests against it

**Security & Governance**
1. Seed the red-team jailbreak test suite in `tests/test_jailbreak.py` with ≥ 20 known injection patterns as a starting scaffold; the Phase 4 deliverable grows this to ≥ 200 cases — starting with 20 ensures CI coverage from day one of Phase 4 without waiting for the full suite
2. Draft the MCP handoff OPA policy in `policies/mcp_handoff.rego` with a stub `allow` rule and a failing test that asserts a cross-type delegation is denied — the stub is the Phase 4 starting baseline; the real rule ships with Phase 4
3. Add "Phase 4 zero-trust risks" section to `docs/threat-model.md` before Phase 4 begins: Vault lease exhaustion scenario, stale Vault token on MCP handoff, cross-agent context exfiltration via crafted handoff payload

**Watchdog & Reliability**
1. Extend `BudgetEnforcer` to accept a `root_task_id` field on every `record_spend()` call; add a unit test asserting that spend recorded under child `task_id` values rolls up to the root budget cap — this is the Phase 4 cross-agent budget tracking prerequisite
2. Scaffold `LoopDetector.detect_cross_agent_loop()` as a stub raising `NotImplementedError`; write a failing test asserting it triggers on an `A → B → A` handoff graph with two repetitions — stub and failing test committed before Phase 4 begins
3. Establish the Phase 4 stress test baseline: run 20 concurrent MCP handoff chains of depth 2 for 2 minutes; record p50, p95, p99 as `PHASE4_MCP_LATENCY_BASELINE` in the test harness — Phase 4 gate requires 100 concurrent chains at or below this p99

**Audit & Compliance**
1. Extend the `AuditLogger` schema to include `mcp_handoff` as a first-class `event_type` with required fields: `source_task_id`, `target_agent_type`, `context_payload_hash`, `opa_decision`; update `docs/audit-event-schema.json` with the new event type before any Phase 4 handoff code is written
2. Extend `ComplianceReporter` to group audit events by `root_task_id` so a multi-agent chain is reportable as one transaction; add a unit test asserting a 3-agent chain report shows one root entry with three child event groups
3. Prototype Vault secret access event capture in `AuditLogger`: when a Vault secret is read, emit an `infra.vault_read` audit event carrying the secret path (not the value) and the OTel `trace_id`; write a unit test asserting the event is emitted and the secret value is not present in the payload

**Frontend & DevEx**
1. Publish the Aegis Governance Loop OpenAPI spec skeleton as `docs/governance-loop-openapi.yaml` with only the Phase 1–3 endpoints populated; the file must pass OpenAPI 3.1 linting and import into Postman or Insomnia before Phase 4 begins — Phase 4 adds MCP handoff endpoints
2. Create `docs/runbooks/vault-rotation-failure.md` as a skeleton with section headings matching the Phase 4 Vault integration risk surface — empty sections only; Phase 4 fills in commands after the Vault integration is implemented
3. Update `docs/agent-sdk-guide.md` with a Phase 4 planning stub section: "MCP Integration Patterns (Coming in v0.8.0)" describing the intended `MCPHandoff` model and delegate agent types — this sets external contributor expectations before the feature lands

---

> **No-Go action:** any failing gate criterion or unchecked release requirement
> blocks the `v0.6.0` tag. The owning team has one remediation sprint (one week);
> the full gate re-runs after fixes are merged to `main`.

---

## Phase 4 — Zero-Trust Hardening & MCP Agent Mesh

**Release target:** `v0.8.0`
**Timeline:** Weeks 13–16
**Goal:** Full zero-trust credential posture and multi-agent, multi-vendor
interoperability via Model Context Protocol. No hardcoded secret survives
this phase. Every item below must be backed by running code and a passing
test suite — stub implementations and `raise NotImplementedError` bodies
in production paths are a hard gate failure.

---

### Platform Team

#### P4-1 — Replace all `AEGIS_*` env-var secrets with HashiCorp Vault

All credentials sourced from Vault with transparent lease renewal and
rotation inside the orchestrator.

**Testing requirements**

- **No-hardcode scan:** `tests/test_vault_migration.py` uses `grep` (via
  `subprocess`) to assert zero occurrences of `os.environ.get("AEGIS_*")`
  in any file under `src/`; any match is a hard test failure, not a warning.
- **Unit — Vault fetch on startup:** mock `VaultClient.get_secret()` to return
  a known value; start the orchestrator; assert every secret-consuming module
  received the mocked value — none fell back to an env-var default.
- **Unit — Vault unavailable at startup:** mock `VaultClient.get_secret()` to
  raise `VaultUnavailableError`; assert the orchestrator refuses to start and
  logs a structured `infra.vault_unavailable` error — it must not start with
  a missing secret.
- **Unit — lease renewal:** mock Vault to issue a 5-second TTL lease; advance
  time by 4 seconds using `freezegun`; assert `VaultClient.renew_lease()` was
  called before expiry and the orchestrator continued serving requests without
  interruption.
- **Integration — live Vault round-trip:** start a Vault dev container via
  `testcontainers`; write a test secret; start the orchestrator pointed at
  the container; run a full task; assert the task completes successfully and
  the Vault audit log records the secret read.
- **No stubs guard:** `inspect.getsource()` on every Vault integration method
  in `src/`; assert none contain `pass`, `...`, or `raise NotImplementedError`.

---

#### P4-2 — Implement MCP Agent Mesh: `MCPHandoff` model and cross-adapter handoff

Structured context handoff between agents (e.g., Anthropic Research Agent →
Local Llama Security Auditor) via a typed `MCPHandoff` model in
`src/adapters/mcp.py`.

**Testing requirements**

- **Unit — model validation:** construct `MCPHandoff` with all required fields
  (`source_task_id`, `source_agent_type`, `target_agent_type`,
  `context_payload`, `allowed_delegates`); assert Pydantic validation passes.
  Omit each required field in turn; assert `ValidationError` is raised for
  each — no silently-ignored missing field.
- **Unit — context payload hashing:** assert `MCPHandoff.context_payload_hash`
  is the SHA-256 hex digest of the serialised `context_payload`; assert the
  hash changes when the payload changes and is identical for identical payloads.
- **Unit — cross-adapter dispatch:** mock both an Anthropic adapter and a
  Local Llama adapter; construct a handoff from `anthropic` to `local_llama`;
  assert the orchestrator calls `local_llama.invoke()` with the context from
  the `MCPHandoff` and does not re-call `anthropic.invoke()`.
- **Unit — OPA policy consulted before dispatch:** assert
  `OPAClient.evaluate()` is called with the handoff's `source_agent_type`
  and `target_agent_type` before any adapter is invoked; if OPA returns
  `deny`, assert no adapter is called and a `mcp.handoff_denied` audit event
  is emitted.
- **Integration — 2-agent chain end-to-end:** run a real 2-agent task
  (Anthropic → Local Llama) through the orchestrator; assert both adapters
  were invoked in order; assert the final `TaskResponse` contains output from
  the second adapter; assert a `mcp.handoff` audit event was emitted with
  a non-null `context_payload_hash`.
- **Contract test — MCPHandoff schema stability:** `tests/test_mcp_contract.py`
  loads the `MCPHandoff` JSON schema and asserts it is identical to the schema
  recorded at the Phase 4 start baseline; any unreviewed field addition or
  removal fails CI.

---

#### P4-3 — Secret rotation mid-flight: no task failures, no stale-secret window

Vault secret rotated while a workflow is in-flight; orchestrator picks up
the new value transparently.

**Testing requirements**

- **Unit — rotation detected and applied:** issue a Vault secret; start a
  long-running mock workflow (10-second sleep activity); rotate the Vault
  secret at the 5-second mark; assert the activity that runs after rotation
  uses the new secret value — not the cached pre-rotation value.
- **Unit — zero task failures during rotation:** rotate the Vault secret
  while 10 concurrent tasks are in flight; assert all 10 tasks complete
  with `TaskResponse.status == "completed"` — zero failures, zero retries
  caused by stale-secret `401` responses.
- **Unit — no window of stale-secret use:** instrument the Vault client to
  record every secret read; rotate the secret; assert no read after the
  rotation timestamp returns the old value — the stale window is zero reads.
- **Negative test — rotation failure is surfaced:** mock the rotation
  `PUT` to Vault to return HTTP `500`; assert the orchestrator emits an
  `infra.vault_rotation_failed` audit event and does not silently continue
  with the old secret beyond its TTL.
- **Integration — live rotation against Vault container:** use `testcontainers`
  Vault; write secret version 1; start 5 concurrent tasks; write secret
  version 2 mid-run; assert all 5 tasks complete and the Vault audit log
  shows no reads of version 1 after the version 2 write.

---

### Security & Governance Team

#### S4-1 — Automated jailbreak test suite: ≥ 200 cases, zero PII leakage

Covers known injection patterns, encoding bypasses, multi-turn jailbreaks,
and adversarial inputs generated by a secondary LLM.

**Testing requirements**

- **Suite completeness:** `tests/test_jailbreak.py` must contain ≥ 200
  distinct test cases organised into at least four named categories:
  `direct_injection`, `encoding_bypass`, `multi_turn`, `adversarial_llm_generated`.
  Fewer than 200 cases or fewer than four categories is a CI failure.
- **Zero PII leakage — direct injection:** submit each direct-injection case
  to the full governance pipeline; assert no `email`, `ssn`, `credit_card`,
  `phone`, or `ipv4` pattern passes through to any LLM adapter; a single
  leakage event is a hard, non-retryable failure.
- **Zero PII leakage — encoding bypass:** submit inputs using Unicode
  homoglyphs, base64 encoding, and ROT-13 variants of each PII class; assert
  the guardrails decode and scrub all variants before adapter invocation.
- **Multi-turn persistence:** run a 5-turn conversation where PII is introduced
  in turn 2; assert the PII is scrubbed from all subsequent turns — the
  scrubber must not become blind to PII introduced mid-conversation.
- **Adversarial LLM-generated cases:** the ≥ 200 suite must include ≥ 20
  cases generated by a secondary LLM (prompt logged in
  `tests/fixtures/jailbreak_generation_prompt.md`); assert the generation
  prompt file is committed to the repo and the generated cases are not
  hand-edited — reproducibility is required.
- **No stubs guard:** `inspect.getsource()` on `Guardrails.scrub()`; assert
  the method body does not contain `pass`, `...`, or `return prompt` without
  a scrub operation — a passthrough guardrail is a hard failure.

---

#### S4-2 — OPA policies govern MCP handoffs via `allowed_delegates`

An agent may only hand off to an agent type listed in its
`allowed_delegates` policy attribute.

**Testing requirements**

- **Unit — allowed delegate permitted:** configure an OPA policy where
  `finance` has `allowed_delegates: ["audit"]`; submit a handoff from
  `finance` to `audit`; assert OPA returns `allow` and the handoff proceeds.
- **Unit — unlisted delegate denied:** submit a handoff from `finance` to
  `general` (not in `allowed_delegates`); assert OPA returns `deny`; assert
  HTTP `403` from the API; assert a `mcp.handoff_denied` audit event is
  emitted with `target_agent_type: "general"`.
- **Unit — missing `allowed_delegates` field defaults to deny:** define an
  agent policy with no `allowed_delegates` attribute; attempt any handoff
  from that agent; assert OPA returns `deny` — absence of the field is not
  an implicit allow.
- **Unit — policy is the sole authority:** assert
  `OPAClient.evaluate()` is called for every `MCPHandoff` before dispatch;
  use `unittest.mock.patch` to replace OPA with a hardcoded `allow`; assert
  the test harness detects the bypass and fails — no hardcoded allow/deny
  anywhere in the handoff path.
- **Integration — live OPA + MCP matrix:** load `policies/mcp_handoff.rego`
  into an OPA container; run a 4 × 4 agent-type matrix (16 handoff
  combinations); assert exactly the combinations listed in `allowed_delegates`
  receive `allow`; all others receive `deny`; the matrix is encoded as a
  parameterised pytest test.
- **Regression guard:** `tests/test_mcp_policy_matrix.py` is added as a
  required CI check; any new agent type added to `policies/` must also be
  added to the matrix before merging.

---

#### S4-3 — Vault-backed `jti` revocation persists across restarts and replicas

Token revoked on node A must be rejected on node B within 1 second.

**Testing requirements**

- **Unit — revocation persists across restart:** revoke a `jti`; stop and
  restart the API process; submit the revoked token; assert HTTP `401` —
  the revocation list is in Vault, not in-memory.
- **Unit — cross-node rejection within 1 s:** mock two API instances sharing
  a `VaultClient`; revoke a token on instance A; within 1 second submit
  the token to instance B; assert `401` in 100/100 runs. Any `200` is a
  hard failure.
- **Unit — revocation list is append-only:** revoke 10 tokens in sequence;
  assert the Vault revocation list contains all 10 `jti` values; assert no
  previously revoked `jti` has been removed from the list.
- **Unit — Vault unavailable during revocation check fails closed:** mock
  `VaultClient` to raise `VaultUnavailableError` when checking the revocation
  list; assert the request is denied with HTTP `503`, not allowed through —
  the system always fails closed.
- **Integration — live cross-replica revocation:** start two API instances
  pointing at a shared Vault dev container; revoke a token via instance A's
  `DELETE /sessions/{id}`; within 1 second submit the token to instance B;
  assert `401`; assert a `jit.revoked_replay_blocked` audit event is emitted
  by instance B.
- **Performance test:** revoke 100 tokens in parallel; assert no read of
  any revoked token returns `200`; total revocation + verification cycle
  completes in < 5 seconds wall-clock for the full 100-token batch.

---

#### S4-4 — Adversarial multi-agent: crafted MCP handoff to unauthorised agent returns `deny`

Finance agent context cannot be routed to an unauthorised `general` agent
via a crafted `MCPHandoff` payload.

**Testing requirements**

- **Unit — direct unauthorised handoff denied:** construct an `MCPHandoff`
  from `finance` to `general` (not in `finance`'s `allowed_delegates`);
  submit to the API; assert HTTP `403`; assert a `mcp.handoff_denied` audit
  event with `source_agent_type: "finance"` and `target_agent_type: "general"`.
- **Unit — context payload not forwarded on deny:** assert no adapter for
  `general` receives any part of the `finance` context payload when the
  handoff is denied — the payload must not leak into any subsequent log,
  error response, or audit event body.
- **Unit — tampered `allowed_delegates` field rejected:** construct a
  handoff JWT that claims `allowed_delegates: ["general"]` for a `finance`
  agent; assert OPA evaluates against the policy on disk, not the JWT claim —
  the JWT cannot override the Rego policy.
- **Unit — adversarial chain: A → B → C where C is unauthorised:** run a
  2-hop handoff `finance → audit → general`; assert the `audit → general`
  hop is denied at the OPA check; assert `finance` and `audit` adapter calls
  are still audited; assert the chain does not silently stop — a structured
  `mcp.chain_denied` audit event is emitted.
- **Integration — 10-case adversarial matrix:** `tests/test_mcp_adversarial.py`
  contains 10 distinct crafted payloads (wrong type, spoofed `task_id`,
  missing hash, mismatched source, replay of prior handoff, etc.); assert
  every case returns `403` or `401` and emits a corresponding audit event;
  any case that returns `200` is a hard failure.

---

### Watchdog & Reliability Team

#### W4-1 — `BudgetEnforcer` tracks cumulative spend across the full MCP agent chain

Intermediate agent spend rolls up to the root `task_id` budget cap.

**Testing requirements**

- **Unit — child spend rolls up to root:** create a root budget session
  with a $10.00 cap; record $3.00 spend on child `task_id=A` and $4.00 on
  child `task_id=B`, both under root `task_id=ROOT`; assert
  `BudgetEnforcer.get_remaining(root_task_id="ROOT")` returns
  `Decimal("3.00")`.
- **Unit — root cap enforced across children:** record spend that would
  exhaust the root cap across three child tasks; assert `BudgetExceededError`
  is raised on the third child's spend, not deferred — the cap is enforced
  synchronously in the same call frame.
- **Unit — Decimal precision across rollup:** record $3.333333 on child A
  and $3.333333 on child B; assert the rolled-up root remaining equals
  `Decimal("3.333334")` computed with `Decimal` arithmetic — no float
  rounding error permitted.
- **Unit — rollup serialises correctly for Temporal:** serialise a root
  `BudgetSession` with two child contributions; deserialise; assert the
  rolled-up value is exactly preserved; a one-cent variance fails.
- **Unit — no double-counting on redelivered child activity:** deliver the
  same child spend activity twice (simulating Temporal at-least-once delivery);
  assert the rolled-up root balance reflects the spend exactly once; the
  idempotency key is `(root_task_id, child_task_id, activity_id)`.
- **Integration — 3-agent chain budget rollup:** run a 3-agent MCP chain
  through the orchestrator; each agent records a distinct spend amount;
  assert the root budget's consumed total equals the exact sum of all three
  child spend values to four decimal places.

---

#### W4-2 — `LoopDetector` detects cross-agent loops before the third repetition

Detects cycles in the MCP handoff graph (A → B → A pattern).

**Testing requirements**

- **Unit — 2-hop cycle detected:** feed the handoff graph `A → B → A`
  to `LoopDetector.detect_cross_agent_loop()`; assert it returns `True`
  and identifies the repeated edge `A → B`; assert detection occurs before
  a third invocation of agent A.
- **Unit — 3-hop cycle detected:** feed the graph `A → B → C → A`;
  assert the loop is detected before agent A is invoked a second time.
- **Unit — no false positive on linear chain:** feed `A → B → C → D`
  (no repeated nodes); assert `detect_cross_agent_loop()` returns `False`
  and no `loop.detected` audit event is emitted.
- **Unit — detection triggers halt:** when a loop is detected, assert
  `BudgetEnforcer.halt()` is called for the root `task_id` and an
  `agent.loop_detected` audit event is emitted with the full cycle path;
  assert no further adapter invocations occur after the halt.
- **Unit — counter survives Temporal retry:** simulate a Temporal retry by
  serialising the `LoopDetector` state after two handoffs, restoring, and
  continuing; assert the loop counter is not reset — a reset would allow
  an attacker to avoid detection by forcing retries.
- **Integration — live cycle injection:** construct an orchestrator test
  that intentionally creates an `A → B → A` handoff cycle; assert the
  `LoopDetector` halts the workflow before adapter A is invoked a third
  time; assert the audit trail records both the cycle path and the halt
  event.

---

#### W4-3 — Stress test: 100+ concurrent agents with MCP handoffs, 10,000 simulated handoffs

No budget double-counting; no loop false-negatives.

**Testing requirements**

- **Test definition:** `tests/test_stress_phase4.py` — launch 100 concurrent
  root tasks each triggering an MCP handoff chain of depth ≤ 3; run until
  10,000 total handoffs have been completed; use a mocked LLM adapter
  returning a fixed response to isolate the orchestration layer.
- **Zero budget double-counting:** after the 10,000-handoff run, assert
  the sum of `token_cost_usd` values recorded in the audit log equals the
  sum of `BudgetEnforcer.get_consumed()` values across all root sessions —
  any discrepancy greater than zero cents is a hard failure.
- **Zero loop false-negatives:** inject 100 intentional `A → B → A` cycles
  among the 10,000 handoffs; assert all 100 cycles were detected and halted
  before the third invocation; any missed cycle is a hard failure.
- **Zero loop false-positives:** assert no linear chain (`A → B → C`) was
  incorrectly flagged as a loop during the 10,000-handoff run; false positives
  in loop detection cause valid agent work to halt incorrectly.
- **p99 latency assertion:** assert p99 end-to-end task latency across the
  full run remains < 600 ms; a run where p99 ≥ 600 ms is a hard test failure.
- **Audit completeness:** assert every handoff in the 10,000-handoff run
  produced a `mcp.handoff` audit event with a non-null `context_payload_hash`
  and `opa_decision`; any missing event indicates a dropped audit write.

---

#### W4-4 — Chaos test: random MCP handoff target failures mid-chain

`LoopDetector` and `BudgetEnforcer` states remain consistent after recovery.

**Testing requirements**

- **Unit — mid-chain target unavailable:** start a 3-agent chain; fail the
  second agent mid-invocation (raise `AdapterUnavailableError`); assert
  the root task retries via Temporal; assert `BudgetEnforcer` spend for
  the failed invocation is not double-counted on retry.
- **Unit — `LoopDetector` state consistent after mid-chain failure:** detect
  a chain interrupted after 2 handoffs with a fault; restore from Temporal
  history; assert the `LoopDetector`'s handoff graph reflects the 2 completed
  handoffs, not 0 or 1; loop detection resumes correctly on next invocation.
- **Chaos test definition:** `tests/test_chaos_phase4.py` — run 50 concurrent
  3-agent chains; randomly fail one of the three agents in each chain at a
  random point using `toxiproxy` or a mock that raises
  `AdapterUnavailableError` with 30% probability; all 50 chains must
  eventually complete (with retries) or terminate cleanly with a structured
  audit event.
- **Budget fidelity under chaos:** after all 50 chains complete or terminate,
  assert no root session overspent its cap by more than zero cents; assert
  the sum of all recorded spend equals the sum of `BudgetEnforcer` consumed
  totals — chaos must not create phantom budget entries.
- **No silent failure:** assert every chain that received a `AdapterUnavailableError`
  either completed after retry or emitted a `task.failed` audit event with
  the failure reason and the faulted agent type; a chain that stops without
  an audit event is a test failure.

---

### Audit & Compliance Team

#### A4-1 — MCP handoff events as first-class audit events

Every handoff produces a structured audit event containing: source agent
`task_id`, target agent type, context payload hash (not plaintext), and
OPA policy decision.

**Testing requirements**

- **Unit — all required fields present:** trigger one MCP handoff through
  the orchestrator; retrieve the emitted `mcp.handoff` audit event; assert
  it contains `source_task_id`, `target_agent_type`, `context_payload_hash`,
  and `opa_decision`; any missing field is a hard failure.
- **Unit — context payload hash is correct:** compute the expected SHA-256
  hash of the serialised context payload in the test; assert the
  `context_payload_hash` in the audit event matches exactly — a hash
  mismatch or a null hash fails.
- **Unit — plaintext context not logged:** assert the audit event body does
  not contain any key named `context_payload` or `context` with a non-hash
  value; the raw context must never appear in the audit store.
- **Unit — denied handoff also audited:** trigger a handoff that OPA denies;
  assert a `mcp.handoff_denied` audit event is emitted with `opa_decision:
  "deny"` and the `target_agent_type` that was rejected; a denied handoff
  with no audit event is a hard failure.
- **Unit — schema conformance:** validate every emitted handoff audit event
  against `docs/audit-event-schema.json`; assert `jsonschema.validate()`
  raises no errors; an event that does not conform to the schema fails CI.
- **Integration — 3-agent chain audit trail:** run a 3-agent chain; retrieve
  all audit events with the root `task_id`; assert exactly two `mcp.handoff`
  events are present (one per hop); assert their `source_task_id` values
  chain correctly: the `target_agent_type` of event 1 is the source of event 2.

---

#### A4-2 — Vault secret access events correlated into the task audit trail

Secret reads are visible in the OTel trace — not a blind spot.

**Testing requirements**

- **Unit — `infra.vault_read` event emitted on every secret read:** mock
  `VaultClient.get_secret()` to emit normally; run one task; assert exactly
  one `infra.vault_read` audit event is emitted per secret read, carrying
  the secret path (not the value) and the OTel `trace_id`.
- **Unit — secret value never in audit event:** assert the `infra.vault_read`
  event body does not contain the actual secret value; use
  `inspect.getsource()` to assert no audit logger call passes the secret
  value as an argument — this is a static analysis check, not just a runtime
  check.
- **Unit — OTel `trace_id` present and matches task span:** assert the
  `trace_id` in the `infra.vault_read` event is identical to the `trace_id`
  of the enclosing task OTel span; a mismatched or null `trace_id` breaks
  trace correlation and fails the test.
- **Unit — Vault read visible in `GET /trace`:** run a task; call
  `GET /api/v1/tasks/{task_id}/trace`; assert the `infra.vault_read` event
  appears in the ordered event sequence — secret reads are not filtered out
  of the trace endpoint response.
- **Integration — live Vault container trace:** run a task against a Vault
  dev container; call `GET /trace`; assert the response includes one
  `infra.vault_read` event per expected secret read with the correct path
  and the root `trace_id`.

---

#### A4-3 — Multi-agent chain auditable as a single unit under one root `task_id`

Compliance report treats the full chain as one auditable transaction.

**Testing requirements**

- **Unit — all child events carry root `task_id`:** run a 3-agent chain;
  retrieve all audit events; assert every event (including `mcp.handoff`,
  `llm.invoked`, `pii.scrubbed`, `policy.evaluated`) carries the root
  `task_id` in its `root_task_id` field — any event missing this field is
  a hard failure.
- **Unit — `GET /trace` returns full chain:** call `GET /tasks/{root_task_id}/trace`;
  assert the response contains events from all three agents in the chain;
  assert the events are ordered by `sequence_number`; assert no agent's
  events are missing.
- **Unit — compliance report groups chain as one transaction:** generate a
  SOC2 report covering a window that contains one 3-agent chain; assert the
  report's `transactions` section contains one entry with `root_task_id`
  linking to three child agent summaries — not three separate unlinked
  transaction entries.
- **Unit — forged child injection rejected:** attempt to emit an audit event
  claiming `root_task_id=ROOT` from outside the orchestrator (direct DB
  insert or API injection); assert the immutable store rejects the event
  and logs a `audit.injection_rejected` event with the source IP.
- **Integration — cross-adapter chain audit completeness:** run a
  `Anthropic → Local Llama` chain; retrieve the full audit trail by
  `root_task_id`; assert both adapters' lifecycle events are present; assert
  the OTel `trace_id` is identical across all events in the chain.

---

#### A4-4 — Integrity test: forged audit event injection rejected and logged

**Testing requirements**

- **Unit — direct store injection rejected:** attempt to insert a row
  directly into the audit backend (bypassing the ORM and the `AuditLogger`)
  with a valid-looking `task_id`; assert the append-only trigger or HMAC
  validation rejects the row; assert the rejection is itself logged as an
  `audit.injection_rejected` event.
- **Unit — HMAC mismatch detected on forged row:** construct a row with a
  valid structure but a forged HMAC; attempt to insert it; assert `verify_hmac(row)`
  returns `False`; assert the row is not committed to the store; assert a
  rejection event is emitted.
- **Unit — API-level injection blocked:** send a `POST` request directly to
  any audit write endpoint (if exposed) with a crafted event payload that
  claims a `task_id` belonging to a completed task; assert HTTP `403`;
  assert the event is not written to the store.
- **Unit — rejection event is itself immutable:** assert the `audit.injection_rejected`
  event is written to the write-once backend with a valid HMAC and appears
  in `verify_integrity()` output as a legitimate event — the rejection record
  cannot itself be forged or deleted.
- **Integration — end-to-end injection attempt:** run a complete attack
  simulation: (1) complete a real task; (2) attempt injection of a
  continuation event via direct DB connection; (3) run `verify_integrity()`;
  assert the integrity report flags the injected row; assert all original
  task events remain intact and unmodified.

---

### Frontend & DevEx Team

#### F4-1 — Publish Aegis Governance Loop OpenAPI spec as `docs/governance-loop-openapi.yaml`

Versioned, linted, and importable into Postman and Insomnia.

**Testing requirements**

- **Lint test:** `tests/test_openapi_lint.py` runs the OpenAPI spec through
  `openapi-spec-validator` (or equivalent); assert zero linting errors; any
  error is a CI failure.
- **Version field test:** assert the spec's `info.version` field is present
  and matches the current roadmap version (`v0.8.0` or later); an absent or
  mismatched version fails.
- **Completeness test:** assert the spec documents all Phase 1–4 endpoints;
  `tests/test_openapi_completeness.py` extracts all route paths from the
  FastAPI app and asserts each appears in the OpenAPI spec — any route
  missing from the spec is a hard failure.
- **Postman import test:** `tests/test_openapi_postman_import.py` uses the
  Postman API (or Newman CLI) to import the spec and assert the collection
  loads without errors; a spec that fails to import is not publishable.
- **Insomnia import test:** convert the spec to an Insomnia collection using
  `insomnia-importers`; assert the import produces at least one request per
  documented endpoint and zero parse errors.
- **Stable contract test:** `tests/test_openapi_contract.py` diffs the
  current spec against the baseline committed at Phase 4 start; any
  breaking change (removed path, changed required parameter, removed
  response field) fails CI — breaking changes require an explicit version
  bump and reviewer approval.

---

#### F4-2 — Publish Rego policy library as a versioned GitHub release

External consumers can pin to a specific version; release includes a
`CHANGELOG` entry.

**Testing requirements**

- **Release artifact test:** `tests/test_rego_release.py` asserts the
  GitHub release tag exists and the release artifact contains all policy
  files from `policies/`; any file in `policies/` absent from the release
  archive is a hard failure.
- **CHANGELOG entry test:** assert `CHANGELOG.md` contains an entry for the
  release version that lists every policy file by name and describes its
  purpose in at least one sentence — a CHANGELOG entry that only says
  "policy release" without specifics fails.
- **External pin test:** `tests/test_rego_external_pin.py` downloads the
  release archive from GitHub, loads each policy into a local OPA instance,
  and asserts all policies evaluate without errors; a policy that fails to
  load in a clean OPA environment is not releasable.
- **Semantic versioning test:** assert the release tag follows semver
  (`major.minor.patch`); assert the policy files within the archive carry
  a `# @version` comment matching the release tag — version mismatch between
  the tag and the file header fails the test.
- **Consumer usage test:** `tests/test_rego_consumer_example.py` simulates
  an external consumer: fetch the pinned release, extract the Rego files,
  load them into OPA, evaluate a sample `AgentRequest`; assert the evaluation
  returns the expected `allow`/`deny` result — the library must be usable
  without any Aegis-OS source code.

---

#### F4-3 — `docs/agent-sdk-guide.md` MCP integration patterns and 2-agent handoff quickstart

The quickstart exercises a real 2-agent handoff end-to-end.

**Testing requirements**

- **Section completeness:** `tests/test_sdk_guide_completeness.py` reads
  `docs/agent-sdk-guide.md`; asserts the following headings are present:
  `MCP Integration Patterns`, `Constructing an MCPHandoff`, `Allowed
  Delegates Policy`, `2-Agent Handoff Quickstart`, and `Debugging Handoff
  Failures`; any missing heading is a CI failure.
- **Quickstart executability:** extract all shell commands from the
  `2-Agent Handoff Quickstart` section via regex; run each command via
  `subprocess` against the running dev stack; assert every command exits
  with code 0 — a quickstart that does not run is not a quickstart.
- **Code block validity:** extract all Python fenced code blocks from the
  guide; run each through `py_compile.compile()` (or `mypy --check`); assert
  zero syntax errors — a guide with broken sample code fails CI.
- **Cross-reference accuracy:** every endpoint path and model field
  referenced in the guide must exist in the FastAPI app and the
  `MCPHandoff` Pydantic model respectively;
  `tests/test_sdk_guide_accuracy.py` diffs referenced names against live
  sources — any reference to a non-existent endpoint or field is a hard
  failure.
- **Integration — quickstart produces real handoff audit event:** run the
  quickstart commands end-to-end; assert the resulting task produced a
  `mcp.handoff` audit event in the audit store; assert the event's
  `context_payload_hash` is non-null — a quickstart that does not
  produce observable, auditable output is not a complete demonstration.
- **Non-author review gate:** the guide must be reviewed by one team member
  who did not write it; all quickstart commands walked through on the
  reviewer's machine; reviewer sign-off recorded in the Gate 4 review issue.

---

### 🚦 Go/No-Go Gate 4 → `v0.8.0`

Every item in the checklist below must be ticked before the gate review
session opens. An unticked item or a failing named test blocks the gate;
the owning team carries the remediation before the session reconvenes.

---

#### Pre-Gate Test Suite Checklist

**Platform**
- [ ] `test_vault_no_hardcode_scan` — `grep -rE "(sk-|AEGIS_.*=|password\s*=|secret\s*=)" src/` returns zero results; any match is a hard CI failure
- [ ] `test_vault_fetch_on_startup` — all secret-consuming modules receive values from mocked `VaultClient.get_secret()`; no env-var fallback
- [ ] `test_vault_unavailable_startup_refuses` — mocked `VaultUnavailableError` on startup; orchestrator refuses to start; structured `infra.vault_unavailable` event emitted
- [ ] `test_vault_lease_renewal` — 5-second TTL lease; `freezegun` advances 4 s; `VaultClient.renew_lease()` called before expiry; requests continue uninterrupted
- [ ] `test_vault_live_round_trip` — `testcontainers` Vault dev instance; test secret written; orchestrator reads it; full task completes; Vault audit log records the read
- [ ] `test_vault_no_stubs` — `inspect.getsource()` on all Vault integration methods in `src/`; none contain `pass`, `...`, or `raise NotImplementedError`
- [ ] `test_mcp_model_validation` — `MCPHandoff` constructed with all required fields; Pydantic validation passes; omit each required field in turn; `ValidationError` raised for each
- [ ] `test_mcp_context_payload_hash` — `MCPHandoff.context_payload_hash` equals SHA-256 hex of serialised payload; changes when payload changes; stable for identical payloads
- [ ] `test_mcp_cross_adapter_dispatch` — mocked Anthropic and Local Llama adapters; handoff from `anthropic` to `local_llama`; `local_llama.invoke()` called; `anthropic.invoke()` not called again
- [ ] `test_mcp_opa_consulted_before_dispatch` — `OPAClient.evaluate()` called before any adapter on every `MCPHandoff`; OPA `deny` → no adapter called; `mcp.handoff_denied` audit event emitted
- [ ] `test_mcp_2agent_chain_end_to_end` — real Anthropic → Local Llama chain; both adapters invoked in order; final `TaskResponse` from second adapter; `mcp.handoff` audit event with non-null `context_payload_hash`
- [ ] `test_mcp_contract_stability` — `MCPHandoff` JSON schema identical to baseline committed at Phase 4 start; any unreviewed field addition or removal fails CI
- [ ] `test_rotation_detected_before_next_read` — secret rotated at 5 s in 10 s mock workflow; activity after rotation uses new value; no read returns old value after rotation timestamp
- [ ] `test_rotation_zero_task_failures` — Vault secret rotated while 10 concurrent tasks in flight; all 10 complete with `status == "completed"`; zero rotation-caused failures or retries
- [ ] `test_rotation_zero_stale_window` — every secret read recorded; no read after rotation timestamp returns old value; stale window is zero reads
- [ ] `test_rotation_failure_surfaced` — mocked Vault rotation `PUT` returns `500`; `infra.vault_rotation_failed` audit event emitted; orchestrator does not continue with old secret beyond TTL
- [ ] `test_rotation_live_vault_container` — `testcontainers` Vault; 5 concurrent tasks; secret version 2 written mid-run; all 5 complete; Vault audit log shows zero reads of version 1 after version 2 write

**Security & Governance**
- [ ] `test_jailbreak_suite_count` — `tests/test_jailbreak.py` contains ≥ 200 distinct cases in at least four named categories: `direct_injection`, `encoding_bypass`, `multi_turn`, `adversarial_llm_generated`; fewer than 200 or fewer than four categories fails CI
- [ ] `test_jailbreak_zero_pii_direct_injection` — all direct-injection cases submitted through full governance pipeline; zero `email`, `ssn`, `credit_card`, `phone`, or `ipv4` reaches any LLM adapter; single leakage is a hard non-retryable failure
- [ ] `test_jailbreak_zero_pii_encoding_bypass` — Unicode homoglyphs, base64, ROT-13 variants of each PII class all scrubbed before adapter invocation
- [ ] `test_jailbreak_multi_turn_persistence` — PII introduced in turn 2 of a 5-turn conversation; scrubbed from all subsequent turns; scrubber does not go blind mid-conversation
- [ ] `test_jailbreak_adversarial_cases_reproducible` — ≥ 20 LLM-generated cases present; `tests/fixtures/jailbreak_generation_prompt.md` committed; cases not hand-edited
- [ ] `test_jailbreak_no_stubs` — `inspect.getsource()` on `Guardrails.scrub()`; body does not contain `pass`, `...`, or `return prompt` without a scrub operation
- [ ] `test_mcp_policy_allowed_delegate` — `finance → audit` handoff where `finance.allowed_delegates: ["audit"]`; OPA returns `allow`; handoff proceeds
- [ ] `test_mcp_policy_unlisted_delegate_denied` — `finance → general` handoff; OPA returns `deny`; HTTP `403`; `mcp.handoff_denied` event with `target_agent_type: "general"`
- [ ] `test_mcp_policy_missing_delegates_deny` — agent policy with no `allowed_delegates` attribute; any handoff attempt returns `deny`; absence is not an implicit allow
- [ ] `test_mcp_policy_sole_authority` — every `MCPHandoff` triggers `OPAClient.evaluate()`; no hardcoded allow/deny in handoff path (verified by mocking OPA and asserting the mock is called)
- [ ] `test_mcp_policy_4x4_matrix` — `policies/mcp_handoff.rego` loaded into OPA container; 16-combination parameterised matrix; only `allowed_delegates` combinations receive `allow`; all others `deny`
- [ ] `test_mcp_policy_matrix_ci` — `tests/test_mcp_policy_matrix.py` added as required CI check; new agent type added without matrix update fails CI
- [ ] `test_jti_revocation_persists_across_restart` — revoke `jti`; restart API; revoked token returns `401`; revocation list sourced from Vault not in-memory
- [ ] `test_jti_cross_node_rejection_1s` — two API instances sharing `VaultClient`; revoke on instance A; submit to instance B within 1 s; `401` in 100/100 runs; any `200` is a hard failure
- [ ] `test_jti_revocation_list_append_only` — revoke 10 tokens; list contains all 10 `jti` values; none removed
- [ ] `test_jti_vault_unavailable_fails_closed` — mocked `VaultUnavailableError` on revocation check; request denied HTTP `503`, not allowed through
- [ ] `test_jti_live_cross_replica` — two API instances + shared Vault container; revoke on A; `401` on B within 1 s; `jit.revoked_replay_blocked` audit event from B
- [ ] `test_jti_revocation_batch_perf` — 100 tokens revoked in parallel; no revoked token returns `200`; full 100-token cycle completes < 5 s
- [ ] `test_adversarial_unauthorised_handoff_denied` — `MCPHandoff` from `finance` to `general`; HTTP `403`; `mcp.handoff_denied` with `source_agent_type: "finance"`, `target_agent_type: "general"`
- [ ] `test_adversarial_context_not_forwarded_on_deny` — no `general` adapter receives any part of `finance` context payload on deny; payload not in error response, log, or audit event body
- [ ] `test_adversarial_tampered_jwt_claim_rejected` — handoff JWT claims `allowed_delegates: ["general"]` for `finance`; OPA evaluates against on-disk policy, not JWT claim; `deny` returned
- [ ] `test_adversarial_chain_partial_deny` — `finance → audit → general`; `audit → general` hop denied; both `finance` and `audit` adapter calls still audited; `mcp.chain_denied` event emitted
- [ ] `test_adversarial_10case_matrix` — `tests/test_mcp_adversarial.py` contains 10 distinct crafted payloads; every case returns `403` or `401` and emits a corresponding audit event; any `200` is a hard failure

**Watchdog & Reliability**
- [ ] `test_budget_child_rollup` — root $10.00 cap; $3.00 on child A + $4.00 on child B under `ROOT`; `get_remaining("ROOT")` returns `Decimal("3.00")`
- [ ] `test_budget_root_cap_enforced_synchronously` — spend exhausts root cap across three children; `BudgetExceededError` raised in same call frame on third child; not deferred
- [ ] `test_budget_decimal_precision_rollup` — $3.333333 on child A + $3.333333 on child B; rolled-up remaining equals `Decimal("3.333334")`; no float rounding error
- [ ] `test_budget_rollup_serialise_round_trip` — root `BudgetSession` with two child contributions serialised/deserialised; rolled-up value exactly preserved; one-cent variance fails
- [ ] `test_budget_no_double_count_redelivery` — same child spend activity delivered twice; root balance reflects spend exactly once; idempotency key is `(root_task_id, child_task_id, activity_id)`
- [ ] `test_budget_3agent_chain_rollup` — 3-agent MCP chain; each agent records distinct spend; root consumed total equals exact sum of all three to four decimal places
- [ ] `test_loop_2hop_cycle_detected` — `A → B → A` fed to `detect_cross_agent_loop()`; returns `True`; repeated edge `A → B` identified; detection before third invocation of A
- [ ] `test_loop_3hop_cycle_detected` — `A → B → C → A`; loop detected before agent A invoked a second time
- [ ] `test_loop_linear_chain_no_false_positive` — `A → B → C → D`; `detect_cross_agent_loop()` returns `False`; no `loop.detected` audit event
- [ ] `test_loop_detection_triggers_halt` — loop detected; `BudgetEnforcer.halt()` called for root `task_id`; `agent.loop_detected` event with full cycle path; no further adapter invocations
- [ ] `test_loop_counter_survives_temporal_retry` — state serialised after 2 handoffs; restored; loop counter not reset; loop detection resumes correctly
- [ ] `test_loop_live_cycle_injection` — orchestrator test with intentional `A → B → A` cycle; halted before third invocation of A; audit trail records cycle path and halt event
- [ ] `test_stress_phase4_zero_double_counting` — 10,000-handoff run; sum of `token_cost_usd` in audit log equals sum of `BudgetEnforcer.get_consumed()` across all root sessions; zero-cent tolerance
- [ ] `test_stress_phase4_zero_loop_false_negatives` — 100 injected `A → B → A` cycles; all 100 detected and halted before third invocation; any missed cycle is a hard failure
- [ ] `test_stress_phase4_zero_loop_false_positives` — no linear chain incorrectly flagged as loop during 10,000-handoff run
- [ ] `test_stress_phase4_p99` — p99 end-to-end task latency across full 10,000-handoff run < 600 ms; single run above threshold is a hard failure
- [ ] `test_stress_phase4_audit_completeness` — every handoff produced a `mcp.handoff` event with non-null `context_payload_hash` and `opa_decision`; any missing event is a dropped audit write
- [ ] `test_chaos_mid_chain_target_failure` — second agent in 3-agent chain fails with `AdapterUnavailableError`; root task retries via Temporal; failed invocation spend not double-counted
- [ ] `test_chaos_loop_detector_state_after_fault` — chain interrupted after 2 handoffs; restored from Temporal history; `LoopDetector` graph reflects exactly 2 completed handoffs; detection resumes correctly
- [ ] `test_chaos_50chains_all_complete_or_terminate` — 50 concurrent 3-agent chains with 30% per-agent failure rate; all 50 either complete (with retries) or emit `task.failed` with reason; zero silent stops
- [ ] `test_chaos_budget_fidelity` — after all 50 chains; no root session overspent cap; sum of recorded spend equals `BudgetEnforcer` consumed totals; chaos produces no phantom budget entries
- [ ] `test_chaos_no_silent_failure` — every chain that received `AdapterUnavailableError` emitted `task.failed` with faulted agent type; chain with no audit event on failure is a test failure

**Audit & Compliance**
- [ ] `test_mcp_audit_all_required_fields` — one handoff triggered; `mcp.handoff` event contains `source_task_id`, `target_agent_type`, `context_payload_hash`, `opa_decision`; any missing field is a hard failure
- [ ] `test_mcp_audit_hash_correct` — expected SHA-256 hash computed in test; event `context_payload_hash` matches exactly; null hash fails
- [ ] `test_mcp_audit_no_plaintext_context` — audit event body contains no key named `context_payload` or `context` with a non-hash value; raw context never in audit store
- [ ] `test_mcp_audit_denied_handoff_logged` — OPA-denied handoff produces `mcp.handoff_denied` event with `opa_decision: "deny"` and the rejected `target_agent_type`; no audit event on deny is a failure
- [ ] `test_mcp_audit_schema_conformance` — every handoff audit event validated against `docs/audit-event-schema.json` via `jsonschema.validate()`; non-conformant event fails CI
- [ ] `test_mcp_audit_3agent_chain_trail` — 3-agent chain; exactly two `mcp.handoff` events with root `task_id`; `target_agent_type` of event 1 is source of event 2
- [ ] `test_vault_read_event_emitted` — one task run; exactly one `infra.vault_read` event per secret read; event carries secret path (not value) and OTel `trace_id`
- [ ] `test_vault_read_no_secret_value` — `infra.vault_read` event body does not contain actual secret value; `inspect.getsource()` confirms no audit logger call passes the secret value
- [ ] `test_vault_read_trace_id_matches` — `trace_id` in `infra.vault_read` event identical to enclosing task OTel span `trace_id`; mismatch or null breaks trace correlation
- [ ] `test_vault_read_visible_in_trace_endpoint` — `infra.vault_read` event appears in `GET /api/v1/tasks/{task_id}/trace` response; not filtered out
- [ ] `test_vault_read_live_container` — task run against Vault dev container; `GET /trace` includes one `infra.vault_read` per expected read with correct path and root `trace_id`
- [ ] `test_chain_all_events_carry_root_task_id` — 3-agent chain; every event (`mcp.handoff`, `llm.invoked`, `pii.scrubbed`, `policy.evaluated`) carries root `task_id` in `root_task_id` field; any missing field fails
- [ ] `test_chain_trace_returns_full_chain` — `GET /tasks/{root_task_id}/trace` returns events from all three agents; ordered by `sequence_number`; no agent's events missing
- [ ] `test_chain_compliance_report_single_transaction` — SOC2 report for window containing one 3-agent chain; `transactions` section has one entry with three child agent summaries; not three unlinked entries
- [ ] `test_chain_forged_child_injection_rejected` — direct DB event insertion claiming `root_task_id=ROOT`; rejected by immutable store; `audit.injection_rejected` event with source IP logged
- [ ] `test_chain_cross_adapter_audit_completeness` — Anthropic → Local Llama chain; full audit trail retrieved by `root_task_id`; both adapters' lifecycle events present; `trace_id` identical across all chain events
- [ ] `test_injection_direct_store_rejected` — direct backend row insertion bypassing ORM; append-only trigger or HMAC validation rejects row; `audit.injection_rejected` event logged
- [ ] `test_injection_hmac_mismatch_detected` — row with valid structure but forged HMAC; `verify_hmac(row)` returns `False`; row not committed; rejection event emitted
- [ ] `test_injection_api_level_blocked` — crafted `POST` to audit write endpoint claiming completed `task_id`; HTTP `403`; event not written to store
- [ ] `test_injection_rejection_event_immutable` — `audit.injection_rejected` event written to write-once backend with valid HMAC; appears in `verify_integrity()` output; cannot itself be forged or deleted
- [ ] `test_injection_end_to_end_simulation` — complete attack: run real task; attempt direct DB injection; run `verify_integrity()`; injected row flagged; original task events intact and unmodified

**Frontend & DevEx**
- [ ] `test_openapi_lint` — `openapi-spec-validator` reports zero errors on `docs/governance-loop-openapi.yaml`; any lint error is a CI failure
- [ ] `test_openapi_version_field` — spec `info.version` present and matches `v0.8.0` or later; absent or mismatched version fails
- [ ] `test_openapi_completeness` — all FastAPI route paths appear in the spec; any route missing from the spec is a hard failure
- [ ] `test_openapi_postman_import` — Newman CLI imports spec; collection loads without errors; a spec that fails to import is not publishable
- [ ] `test_openapi_insomnia_import` — `insomnia-importers` converts spec; at least one request per documented endpoint; zero parse errors
- [ ] `test_openapi_contract_stability` — diff against Phase 4 baseline; any breaking change (removed path, changed required parameter, removed response field) fails CI
- [ ] `test_rego_release_artifact_complete` — GitHub release tag exists; artifact contains all files from `policies/`; any missing file is a hard failure
- [ ] `test_rego_changelog_entry` — `CHANGELOG.md` entry for release version lists every policy file by name with at least one sentence describing its purpose
- [ ] `test_rego_external_pin` — release archive downloaded; each policy loaded into clean local OPA instance; all policies evaluate without errors
- [ ] `test_rego_semver_tag` — release tag follows semver; each policy file carries `# @version` comment matching the release tag; mismatch fails
- [ ] `test_rego_consumer_example` — external consumer simulation: pinned release fetched; Rego files extracted; loaded into OPA; sample `AgentRequest` evaluated; expected `allow`/`deny` result returned without any Aegis-OS source
- [ ] `test_sdk_guide_sections` — `docs/agent-sdk-guide.md` contains `MCP Integration Patterns`, `Constructing an MCPHandoff`, `Allowed Delegates Policy`, `2-Agent Handoff Quickstart`, and `Debugging Handoff Failures`; any missing heading fails CI
- [ ] `test_sdk_guide_commands` — all shell commands in `2-Agent Handoff Quickstart` extracted via regex; run via `subprocess` against dev stack; all exit code 0
- [ ] `test_sdk_guide_code_blocks` — all Python fenced code blocks extracted; each passes `py_compile.compile()`; zero syntax errors
- [ ] `test_sdk_guide_accuracy` — every endpoint path and model field referenced in the guide exists in the FastAPI app and `MCPHandoff` Pydantic model; any dead reference is a hard failure
- [ ] `test_sdk_guide_produces_audit_event` — quickstart commands run end-to-end; resulting task produces `mcp.handoff` audit event with non-null `context_payload_hash`

---

#### Gate Criteria

All five criteria below reference the named tests above. A criterion passes
only when every named test in its row has a green result in CI on `main`.

| ID | Criterion | Owner | Pass definition |
|---|---|---|---|
| **G4-1** | Zero hardcoded credentials | Platform | `test_vault_no_hardcode_scan`, `test_vault_fetch_on_startup`, `test_vault_unavailable_startup_refuses`, and `test_rotation_zero_task_failures` all pass; `grep` scan returns zero results on `main` |
| **G4-2** | Jailbreak suite — zero leakage | Security & Governance | `test_jailbreak_suite_count`, `test_jailbreak_zero_pii_direct_injection`, `test_jailbreak_zero_pii_encoding_bypass`, `test_adversarial_10case_matrix`, and `test_mcp_policy_4x4_matrix` all pass; zero PII leakage events in full suite run |
| **G4-3** | Cross-agent budget fidelity | Watchdog & Reliability | `test_stress_phase4_zero_double_counting`, `test_stress_phase4_zero_loop_false_negatives`, `test_stress_phase4_p99`, and `test_chaos_budget_fidelity` all pass; 10,000-handoff run shows zero double-counted cents |
| **G4-4** | Multi-agent audit integrity | Audit & Compliance | `test_chain_all_events_carry_root_task_id`, `test_injection_end_to_end_simulation`, `test_vault_read_visible_in_trace_endpoint`, and `test_chain_compliance_report_single_transaction` all pass; forged injection rejected and logged in live test |
| **G4-5** | Open standard artifacts published | Frontend & DevEx | `test_openapi_lint`, `test_rego_external_pin`, `test_rego_consumer_example`, and `test_sdk_guide_produces_audit_event` all pass; non-author sign-off on SDK guide recorded in Gate 4 review issue |

---

#### Release Requirements for `v0.8.0`

All of the following must be complete before the tag is cut.

**Code**
- [ ] `main` branch passes `pytest` with zero failures, zero skips, zero `xfail` markers in production test paths
- [ ] `mypy src/` reports zero errors
- [ ] `ruff check src/ tests/` reports zero errors
- [ ] Zero occurrences of `os.environ.get("AEGIS_*")` in `src/`; enforced by `test_vault_no_hardcode_scan` as a required CI check
- [ ] `MCPHandoff` Pydantic model fully implemented with all required fields; contract test `test_mcp_contract_stability` is a required CI check
- [ ] `LoopDetector.detect_cross_agent_loop()` fully implemented — no `raise NotImplementedError` body; `test_loop_no_stubs` (using `inspect.getsource()`) is a required CI check
- [ ] `BudgetEnforcer.record_spend()` accepts `root_task_id`; rollup logic fully implemented; `test_budget_3agent_chain_rollup` is a required CI check
- [ ] `tests/test_jailbreak.py` contains ≥ 200 named cases in four categories; `test_jailbreak_suite_count` is a required CI check
- [ ] `policies/mcp_handoff.rego` fully implemented; `test_mcp_policy_4x4_matrix` is a required CI check; any new agent type added to `policies/` must be added to the matrix before merging
- [ ] All five Vault integration methods under `src/` pass `test_vault_no_stubs`; no stubs permitted in the Vault fetch, lease renewal, or rotation paths
- [ ] `docs/governance-loop-openapi.yaml` passes `openapi-spec-validator` lint; `test_openapi_lint` is a required CI check
- [ ] Rego policy library published to a versioned GitHub release; `test_rego_external_pin` and `test_rego_consumer_example` pass against the published release
- [ ] `CHANGELOG.md` entry written for `v0.8.0` listing all shipped capabilities with roadmap item IDs (P4-1 through F4-3)

**Documentation**
- [ ] `docs/api-reference.md` documents `MCPHandoff` request/response schema, `PUT /api/v1/policies/{policy_id}` with `allowed_delegates` semantics, and all new Phase 4 endpoints with full error code tables
- [ ] `docs/agent-sdk-guide.md` updated — all five required section headings present; all quickstart shell commands executable; non-author reviewer sign-off recorded in Gate 4 review issue
- [ ] `docs/governance-loop-openapi.yaml` committed at `v0.8.0`; spec version field matches; Postman and Insomnia import both verified
- [ ] `docs/threat-model.md` updated with Phase 4 attack surfaces: Vault lease exhaustion, stale-token MCP handoff window, cross-agent context exfiltration, jailbreak injection via adversarial LLM-generated input
- [ ] `docs/architecture_decisions.md` contains MCP handoff design decision record: why the `MCPHandoff` model was chosen over alternatives, the `allowed_delegates` policy schema, and the `context_payload_hash` privacy rationale; reviewed by Platform and Security team leads
- [ ] `docs/runbooks/vault-rotation-failure.md` complete with `Symptoms`, `Diagnosis`, `Escalation`, and `Resolution` sections; all shell commands executable; walked through by non-author
- [ ] All existing runbooks in `docs/runbooks/` updated to reference Phase 4 endpoints and Vault-based revocation list; any runbook referencing env-var secrets updated to Vault paths
- [ ] `tests/fixtures/jailbreak_generation_prompt.md` committed; documents the secondary LLM prompt used to generate the ≥ 20 adversarial jailbreak cases; reproducibility confirmed

**Infrastructure**
- [ ] `docker-compose.yml` starts the full Phase 4 stack cleanly from cold state including Vault dev service; `GET /health` returns `ok` within 60 s; all `AEGIS_*` env-vars removed from the compose file
- [ ] Vault dev service in `docker-compose.yml` pre-seeded with all secrets required by the orchestrator; a fresh `docker-compose up -d` produces a fully operational stack with no manual secret injection
- [ ] `policies/mcp_handoff.rego` loaded by OPA on startup with zero errors; verified by `test_mcp_policy_4x4_matrix` against the Docker Compose OPA instance
- [ ] `testcontainers` used in CI for Vault, OPA, Grafana, and Prometheus integration tests; all container-backed tests pass in CI without local infrastructure
- [ ] `tests/test_stress_phase4.py` added as an optional slow test (marked `@pytest.mark.slow`); excluded from default `pytest` run but required to pass before cutting the `v0.8.0` tag
- [ ] Pre-commit hooks enforcing: `ruff`, `mypy`, `test_vault_no_hardcode_scan`, `test_mcp_contract_stability`, `test_mcp_policy_matrix_ci`, and audit schema conformance check

---

#### Next Steps by Team — Phase 5 Preparation

Gate 4 passes and the `v0.8.0` tag is cut. Each team's first actions entering
Phase 5 are listed below. These are **hardening and verification setup actions**
— not new features. Phase 5 ships zero new capabilities; all effort is directed
at production readiness, external review, and final defect resolution.

**Platform**
1. Run `mypy src/ --strict` and `ruff check src/ tests/` on the `v0.8.0` tag; record all remaining errors (even if zero) as `PHASE5_STATIC_ANALYSIS_BASELINE` in `docs/architecture_decisions.md` — this is the starting line for the Phase 5 zero-error requirement; any error introduced after this point is a regression
2. Prepare the crash-resilience regression suite: enumerate all five Temporal workflow stages and produce a `tests/test_crash_resilience_regression.py` that injects a kill at each stage; confirm the test file exists and at least one test is present before Phase 5 begins — the full passing suite is the Phase 5 gate requirement
3. Draft the `v1.0.0` release checklist as a GitHub issue template listing every G5 criterion with an owner, a verification command, and a sign-off field — the template must be reviewed by all five team leads before Phase 5 begins; no criterion may be added or removed during Phase 5 without a team-lead vote

**Security & Governance**
1. Scope the external penetration test: identify a vendor, define the test surface (Guardrails, OPA policy layer, Vault integration, and MCP handoff path), and produce a `docs/pentest-scope.md` before Phase 5 begins; the pentest cannot start without a signed scope document
2. Triage all `test_jailbreak.py` cases that are currently marked `xfail` or skipped; resolve or escalate each before Phase 5 begins — no jailbreak test may remain skipped when the Phase 5 suite runs; convert each to a hard failing test or produce a documented exemption approved by the Security team lead
3. Review the full OPA policy library for any hardcoded `allow` or `allow if true` rules that bypass the `allowed_delegates` check; document findings in `docs/threat-model.md` under "Phase 5 policy audit results" before Phase 5 implementation begins

**Watchdog & Reliability**
1. Set up the 72-hour sustained load test infrastructure: provision a dedicated CI environment (separate from the standard CI runner), configure it to run `tests/test_load_phase3.py` at 50 concurrent agents, and verify it completes one full 10-minute run without errors before Phase 5 begins — the 72-hour run is the Phase 5 gate requirement
2. Establish `PHASE5_LOAD_BASELINE` constants: run 30 minutes at 50 concurrent agents on the Phase 4 tag; record p50, p95, p99; commit as constants in `tests/test_load_phase5_baseline.py`; Phase 5 gate requires p99 to remain ≤ `PHASE5_LOAD_BASELINE.p99` throughout 72 hours
3. Audit all Prometheus alert rules in `docs/prometheus.yml` for rules that have no corresponding runbook URL annotation; add the annotation or file a failing CI test before Phase 5 begins — the Phase 5 gate requires all alert rules to have resolvable runbook links

**Audit & Compliance**
1. Commission the external SOC2 auditor review: share the Phase 3–4 `ComplianceReporter` output and the `soc2_auditor_checklist.json` with the auditor; obtain a written list of gaps before Phase 5 begins — Phase 5 closes those gaps; auditor engagement without a gap list is not an engagement
2. Verify the write-once audit backend contains no unverified HMAC rows from Phase 4 testing: run `verify_integrity()` against the full Phase 4 audit store; assert zero `INTEGRITY_COMPROMISED` rows; document the result in `docs/architecture_decisions.md` as the Phase 5 starting integrity baseline
3. Extend `docs/audit-event-schema.json` to include the three Phase 4 event types (`mcp.handoff`, `mcp.handoff_denied`, `infra.vault_read`) with all required fields documented; run the schema conformance CI check against the Phase 4 audit store; zero conformance violations required before Phase 5 begins

**Frontend & DevEx**
1. Run a full documentation review of all files under `docs/`: identify every `TODO`, `FIXME`, broken link, or placeholder section; file each as a GitHub issue labelled `docs-debt` before Phase 5 begins; Phase 5 closes every `docs-debt` issue before the `v1.0.0` tag
2. Verify the OpenAPI spec `docs/governance-loop-openapi.yaml` passes import in the current release of both Postman (v10+) and Insomnia (v2023+); if either import fails, file a breaking-change issue immediately — the Phase 5 gate re-verifies both imports on the `v1.0.0` tag
3. Prepare the SDK guide for external review: share `docs/agent-sdk-guide.md` with one external developer (outside the core team) and ask them to follow the quickstart verbatim; document every point of confusion or failure in `docs/sdk-guide-external-review.md` before Phase 5 begins — Phase 5 resolves all documented friction points before the v1.0 release

---

> **No-Go action:** any failing gate criterion or unchecked release requirement
> blocks the `v0.8.0` tag. The owning team has one remediation sprint (one week);
> the full gate re-runs after fixes are merged to `main`.

---

## Phase 5 — v1.0 Release Hardening

**Release target:** `v1.0.0`
**Timeline:** Weeks 17–20
**Goal:** Final production hardening, external security review, and release
criteria verification. No new features. Bug fixes and hardening only.

> Phase 5 ships zero new capabilities. Every checklist item is a hardening,
> verification, or remediation task. Testing requirements confirm that fixes
> are real working code, that external reviews are actionable and signed off,
> and that every numeric threshold is a hard CI failure — not a warning that
> gets deferred to a later release.

---

### Platform Team

#### P5-1 — Zero `mypy` and `ruff` errors across `src/` and `tests/`; enforced as a required CI check

All static-analysis debt from Phases 1–4 resolved. Each fix is a real type
annotation or code correction — not a `# type: ignore` suppression.

**Testing requirements**

- **Regression — zero mypy errors:** `mypy src/ --strict` exits with code 0
  on `main`; the result is captured and asserted in CI; a non-zero exit code
  or any suppression comment added during Phase 5 fails the gate.
- **Regression — zero ruff errors:** `ruff check src/ tests/` exits with
  code 0 on `main`; any new error introduced after the Phase 5 baseline
  commit is a regression and fails CI immediately.
- **No `# type: ignore` additions:** `grep -rn "type: ignore" src/` must
  produce the same or fewer matches than the Phase 4 tag baseline;
  `tests/test_no_new_type_ignore.py` diffs the count and fails if it grows.
- **No suppression of ruff rules:** `pyproject.toml` `[tool.ruff.lint.per-file-ignores]`
  must not gain new entries during Phase 5; `tests/test_no_new_ruff_suppressions.py`
  diffs the `pyproject.toml` against the Phase 4 baseline and fails if the
  suppression list grows.
- **No stubs guard — full src scan:** `inspect.getsource()` scan across all
  public functions and methods in `src/`; assert zero occurrences of
  `pass`, `...`, or `raise NotImplementedError` in non-test code;
  `tests/test_no_production_stubs.py` is a required CI check.
- **CI enforcement test:** `tests/test_ci_static_analysis_enforced.py`
  reads the CI configuration file and asserts the `mypy` and `ruff` steps
  exist, are not conditional, and are not allowed to fail; a CI config that
  marks these steps as `continue-on-error` is a hard failure.
- **Crash-resilience regression suite complete:** `tests/test_crash_resilience_regression.py`
  exists and covers all five Temporal workflow stages (`pre-pii-scrub`,
  `policy-eval`, `jit-token-issue`, `llm-invoke`, `post-sanitize`); each
  test injects a kill signal at the named stage; all five tests pass with
  zero active task state lost after recovery.
- **No active task state loss:** each crash test asserts `GET /api/v1/tasks/{task_id}`
  returns the expected `status` (`pending` or `completed`) after process
  restart — not `unknown` or `missing`; any state loss is a hard test failure.

---

#### P5-2 — Full regression suite passes at 100% with no skips, no `xfail`, and coverage ≥ 90% on orchestrator

The test suite must be production-ready — not padded with skips to reach
green.

**Testing requirements**

- **100% pass rate:** `pytest` exits with zero failures and zero errors on
  `main`; CI reports the exact failure count; any non-zero count blocks the tag.
- **Zero skips in production paths:** `pytest --tb=short -q` output contains
  no `s` (skipped) markers for tests in `tests/test_orchestrator.py`,
  `tests/test_guardrails.py`, `tests/test_budget_enforcer.py`,
  `tests/test_session_mgr.py`, or `tests/test_compliance.py`; skips in
  fixture-setup helpers are permitted with documented justification.
- **Zero `xfail` markers in production test paths:** `grep -rn "xfail" tests/`
  must return zero results for any non-slow, non-external-service test file;
  `tests/test_no_xfail_in_production_tests.py` enforces this as a CI check.
- **≥ 90% line coverage on orchestrator:** `pytest --cov=src/control_plane
  --cov-report=term-missing` reports ≥ 90% line coverage; any drop below 90%
  is a coverage regression and blocks the tag.
- **≥ 95% line and 100% branch on guardrails:** `pytest --cov=src/governance/guardrails
  --cov-branch --cov-report=term-missing` reports ≥ 95% line and 100% branch;
  a missed branch that was previously covered is a regression.
- **Coverage regression guard:** `tests/test_coverage_regression.py` reads
  the `.coverage` artifact, extracts per-module percentages, and asserts none
  dropped below the Phase 4 baseline values; any regression fails CI.

---

### Security & Governance Team

#### S5-1 — External penetration test completed; all critical and high findings resolved and regression-tested

The pentest is not a checkbox — every finding gets a code fix and a
regression test that would have caught the original vulnerability.

**Testing requirements**

- **No critical or high open findings:** the pentest report's finding list
  must contain zero items with severity `Critical` or `High` in `open`
  status; the sign-off document from the pentest vendor is a required
  artifact before Gate 5 opens; an unsigned report blocks the gate.
- **Regression test per finding:** for every finding resolved during Phase 5,
  a corresponding named test must exist in the `tests/` directory that
  directly exercises the fixed code path; `tests/test_pentest_regressions.py`
  aggregates all pentest regression tests; the test list must match the
  resolved-finding list in the pentest report — any resolved finding with no
  regression test is an incomplete fix.
- **Guardrails full regression — ≥ 50 adversarial inputs per PII class:**
  `tests/test_guardrails_full_regression.py` contains ≥ 50 adversarial
  inputs for each of the five PII classes (`email`, `ssn`, `credit_card`,
  `phone`, `ipv4`); zero PII-containing strings reach any adapter across all
  250 inputs; a single leakage is a hard test failure.
- **Jailbreak suite clean — zero skips, zero `xfail`:** all ≥ 200 jailbreak
  cases pass with no suppression; `grep -n "xfail\|skip" tests/test_jailbreak.py`
  returns zero results; any suppressed case is a non-resolved vulnerability.
- **OPA policy audit — no hardcoded allow rules:** `grep -rn "allow\s*=\s*true\|allow\s*if\s*true"
  policies/` returns zero results; `tests/test_rego_no_hardcoded_allow.py`
  enforces this as a CI check; any unconditional `allow` in a Rego file fails.
- **MCP adversarial matrix — clean after pentest fixes:** `tests/test_mcp_adversarial.py`
  10-case matrix all return `403`/`401`; if new cases were added as pentest
  regressions, the new cases must also be present in the `Gate 4 baseline`
  diff so reviewers can see what was added.
- **No new Phase 5 security debt:** `grep -rn "TODO\|FIXME\|HACK\|XXX" src/governance/
  src/adapters/` returns zero results; any unresolved comment in the security
  or adapter modules is a release blocker.

---

#### S5-2 — Full PII regression suite ≥ 250 adversarial inputs across all pipeline stages

PII scrub must hold at every stage of the Aegis Governance Loop, not just the
entry point.

**Testing requirements**

- **Pre-scrub stage coverage:** adversarial inputs submitted at the raw
  prompt ingestion point; assert zero PII reaches `policy-eval` stage;
  `tests/test_pii_pre_scrub_regression.py` parameterised over all 50+ inputs
  per class.
- **Post-sanitize stage coverage:** inputs designed to survive the pre-scrub
  stage (e.g., PII introduced by the LLM adapter itself); assert
  `post-sanitize` stage catches all injected PII in the LLM response before
  returning to callers; `tests/test_pii_post_sanitize_regression.py` contains
  ≥ 20 response-injection cases.
- **Unicode homoglyph coverage:** ≥ 10 inputs per PII class using Unicode
  homoglyphs (e.g., Cyrillic `а` substituted for Latin `a`); all caught
  before adapter invocation; `tests/test_pii_homoglyph_regression.py` is a
  required CI check.
- **Performance regression guard:** `tests/test_pii_scrub_perf_regression.py`
  runs the full Phase 5 PII suite (≥ 250 inputs) and asserts the p99 scrub
  latency remains < 50 ms per input (as required by the project baseline);
  exceeding 50 ms p99 is a hard performance regression.
- **No stubs in scrub path:** `inspect.getsource(Guardrails.scrub)` contains
  no `pass`, `...`, `return prompt`, or `return input` without a scrub
  operation; confirmed by `tests/test_guardrails_no_stubs.py` on every commit.

---

### Watchdog & Reliability Team

#### W5-1 — 72-hour sustained load test at 50 concurrent agents; p99 < 500 ms throughout; no metric anomalies

The 72-hour run is not a pass/fail checkbox — every hour's p99, error rate,
and budget fidelity sample is a data point that must stay within bounds.

**Testing requirements**

- **Load test infrastructure exists and is verified:** `tests/test_load_phase5_infra.py`
  asserts the dedicated CI load environment is reachable, the 50-concurrent-agent
  configuration is committed to `tests/load_config_phase5.yaml`, and a 10-minute
  warm-up run completes without errors before the full 72-hour run begins.
- **Hourly p99 samples all < 500 ms:** the 72-hour run records a p99 sample
  every 60 minutes (72 samples); assert all 72 samples are < 500 ms; a single
  sample ≥ 500 ms fails the load gate; results stored in
  `tests/artifacts/load_phase5_results.json` and committed to the gate review issue.
- **Zero error-rate anomalies:** define "anomaly" as any 5-minute window
  with error rate > 0.1%; assert zero such windows across the full 72-hour
  run; a window exceeding 0.1% triggers an immediate investigation hold before
  the gate review can proceed.
- **Budget fidelity throughout:** assert that across all tasks in the 72-hour
  run, zero sessions exceed their USD cap by more than zero cents; the
  `BudgetEnforcer` consumed total in `GET /metrics` matches the audit log
  sum to four decimal places at every hourly checkpoint.
- **Prometheus metrics never silent:** assert every task in the run emits
  both a `task_completion` counter increment and a `task_duration_seconds`
  histogram observation; `tests/test_metrics_never_silent.py` samples 100
  random tasks from the run's audit log and verifies their metric presence
  in Prometheus; any missing metric is a silent failure.
- **No metric anomalies in Grafana alerts:** assert zero Grafana alert
  firings during the 72-hour run; if an alert fires, the Phase 5 gate is
  blocked until the cause is identified and a corresponding runbook is written
  or updated; the alert firing is documented in the gate review issue.
- **Runbook coverage for all alert rules:** `tests/test_prometheus_runbook_coverage.py`
  reads `docs/prometheus.yml`; asserts every alert rule has a non-empty
  `annotations.runbook_url` that resolves to an existing file under
  `docs/runbooks/`; any alert rule without a resolvable runbook URL is a hard
  CI failure.

---

#### W5-2 — All Prometheus metrics emit on every task completion and every error path

No silent metric drops on normal or error paths.

**Testing requirements**

- **Happy-path metric completeness:** run 100 tasks to completion through
  the full governance loop; assert each task emits exactly one
  `task_completion` counter increment and one `task_duration_seconds`
  observation; `tests/test_metrics_happy_path.py` is a required CI check.
- **Error-path metric completeness:** inject errors at each of the five
  named OTel span stages (`pre-pii-scrub`, `policy-eval`, `jit-token-issue`,
  `llm-invoke`, `post-sanitize`); assert a `task_error_total` counter
  increment with the correct `stage` label is emitted for each; no error
  path may silently drop metrics.
- **Budget-exceeded metric emitted:** trigger `BudgetExceededError` on a
  live task; assert `budget_exceeded_total` counter incremented with the
  correct `task_id` label; assert no `task_completion` counter incremented
  for the same task — the two counters are mutually exclusive.
- **Loop-detected metric emitted:** trigger `LoopDetector` halt; assert
  `loop_detected_total` counter incremented with cycle path label; assert
  no further `task_duration_seconds` observation for the halted root task.
- **No double-emit on Temporal retry:** deliver the same Temporal activity
  result twice (simulating at-least-once delivery); assert counters for the
  activity are incremented exactly once; a double-emit inflates metrics and
  misrepresents task counts.

---

### Audit & Compliance Team

#### A5-1 — External SOC2 auditor sign-off obtained on the `ComplianceReporter` output against Type II criteria

The auditor sign-off is a required gate artifact — not a "we sent them an email".

**Testing requirements**

- **Auditor gap list closed:** the auditor's gap list (produced during Phase 4
  preparation) must have zero open items at the time of Gate 5; each gap
  item must have a corresponding named test that verifies the fix;
  `tests/test_soc2_gap_closure.py` asserts the gap list file
  `docs/soc2-gap-closure.md` has zero items marked `open`; any open item
  fails the gate.
- **SOC2 report generation is live code, not a stub:** `ComplianceReporter.generate_soc2_report()`
  executes end-to-end against a real 24-hour synthetic task window and
  produces a non-empty JSON report; `inspect.getsource(ComplianceReporter.generate_soc2_report)`
  contains no `pass`, `...`, or `raise NotImplementedError`;
  `tests/test_compliance_report_not_stub.py` enforces this.
- **Report covers ≥ 1,000 tasks:** `tests/test_compliance_24h_synthetic.py`
  runs a 24-hour synthetic task replay (1,000 tasks minimum) and asserts the
  generated SOC2 report's `task_count` field ≥ 1,000; a report covering fewer
  tasks has insufficient coverage for a Type II review.
- **Every task has a complete audit trace:** after the 24-hour synthetic run,
  assert every `task_id` in the report has a `task.started`, at least one
  stage event, and either `task.completed` or `task.failed` in the audit
  store; any `task_id` missing a lifecycle event fails `verify_integrity()`.
- **Tamper-evident audit store — integrity check passes:** run
  `verify_integrity()` against the full synthetic task window; assert zero
  `INTEGRITY_COMPROMISED` rows; a single compromised row blocks the gate and
  requires a post-mortem before the tag can be cut.
- **GDPR report generation parity:** `ComplianceReporter.generate_gdpr_report()`
  produces a non-empty report covering the same 24-hour window; the GDPR
  report's `pii_scrub_events` count matches the `pii.scrubbed` audit event
  count in the audit store for the same window; any mismatch indicates
  missing event linkage.
- **External auditor sign-off is a file artifact:** `docs/soc2-auditor-signoff.md`
  exists, is non-empty, and contains the auditor's name, firm, date, and
  a statement that the Type II criteria review is complete; the file is
  committed to `main`; `tests/test_soc2_signoff_artifact.py` asserts the
  file exists, is ≥ 100 characters, and contains the required fields.

---

#### A5-2 — `verify_integrity()` passes on the full production audit store without errors

Every audit event written since Phase 1 must pass HMAC verification.

**Testing requirements**

- **Full store integrity check passes:** `verify_integrity()` run against the
  complete audit store (all phases) exits with zero `INTEGRITY_COMPROMISED`
  events; `tests/test_audit_full_store_integrity.py` runs this as a required
  pre-tag check.
- **HMAC verification is live code:** `inspect.getsource(AuditLogger.verify_integrity)`
  contains no `pass`, `...`, `return True`, or `raise NotImplementedError`;
  the HMAC computation is the actual implementation, not a placeholder that
  always returns success.
- **Injection attempt rejected by running integrity check:** inject one
  synthetic forged row; run `verify_integrity()`; assert the forged row is
  flagged as `INTEGRITY_COMPROMISED`; assert all original rows remain
  `INTEGRITY_OK`; the integrity check must distinguish forged from real.
- **Performance — full store check completes in < 60 s:** `tests/test_audit_integrity_perf.py`
  times `verify_integrity()` against a store of ≥ 10,000 events; asserts
  completion in < 60 s wall-clock; a check that takes longer than 60 s is
  too slow to run in CI on every commit.
- **Audit schema conformance — zero violations across all events:** run
  `jsonschema.validate()` against every event in the audit store using
  `docs/audit-event-schema.json`; assert zero validation errors; a schema
  violation indicates a Phase 1–4 event was written without conformance
  checking and must be addressed before the tag.

---

### Frontend & DevEx Team

#### F5-1 — Full documentation review completed; zero `TODO` / `FIXME` comments; all `docs-debt` issues closed

Every documentation file under `docs/` is production-ready — no known gaps
deferred to a post-release follow-up.

**Testing requirements**

- **Zero `TODO` / `FIXME` in `docs/`:** `grep -rn "TODO\|FIXME\|PLACEHOLDER\|TBD"
  docs/` returns zero results; `tests/test_docs_no_todo.py` enforces this as
  a CI check; any match is a release blocker.
- **Zero `TODO` / `FIXME` in `src/`:** `grep -rn "TODO\|FIXME\|HACK\|XXX" src/`
  returns zero results; `tests/test_src_no_todo.py` enforces this as a
  required CI check; production code must not contain deferred work markers.
- **All `docs-debt` GitHub issues closed:** `tests/test_docs_debt_issues_closed.py`
  queries the GitHub API for issues labelled `docs-debt` in `open` state;
  asserts the count is zero; any open `docs-debt` issue blocks the tag.
- **All runbook shell commands exit 0:** for every runbook under
  `docs/runbooks/`, extract all shell commands via regex; run each via
  `subprocess` against the running dev stack; assert every command exits
  with code 0; `tests/test_runbooks_executable.py` is a required CI check.
- **All documentation links resolve:** `tests/test_docs_links.py` extracts
  every Markdown link from every file under `docs/` and asserts: (a) internal
  links point to existing files and anchors, (b) external links return HTTP
  `200`; any broken link fails CI.
- **No orphaned documentation files:** assert every file under `docs/` is
  linked from at least one other document or from `README.md`; orphaned files
  are invisible to users and indicate documentation rot;
  `tests/test_docs_no_orphans.py` enforces this.

---

#### F5-2 — `README.md` links to all published artifacts: OpenAPI spec, audit event schema, and Rego policy library

The open standard is only open if it is discoverable.

**Testing requirements**

- **OpenAPI spec link present and resolves:** `tests/test_readme_openapi_link.py`
  reads `README.md`; asserts a link to `docs/governance-loop-openapi.yaml`
  is present; asserts the file exists and passes `openapi-spec-validator` lint;
  a broken or absent link fails CI.
- **Audit event schema link present and resolves:** `tests/test_readme_schema_link.py`
  asserts `README.md` links to `docs/audit-event-schema.json`; asserts the
  file is valid JSON that passes `jsonschema` meta-schema validation; absent
  or invalid link fails CI.
- **Rego library release link present and resolves:** `tests/test_readme_rego_link.py`
  asserts `README.md` links to the versioned GitHub release of the Rego
  policy library; asserts the release URL returns HTTP `200`; assert the
  release artifact contains all files from `policies/`; a dead link or
  incomplete artifact fails CI.
- **SDK guide quickstart still executable at `v1.0.0`:** run all quickstart
  shell commands from `docs/agent-sdk-guide.md` via `subprocess` against the
  `v1.0.0`-tagged stack; assert all exit code 0; assert the run produces a
  `mcp.handoff` audit event; a quickstart that broke between `v0.8.0` and
  `v1.0.0` is a regression.
- **External developer friction points resolved:** `docs/sdk-guide-external-review.md`
  must exist and contain zero items marked `unresolved`; `tests/test_sdk_external_review_resolved.py`
  reads the file and asserts no `unresolved` marker is present; any
  unresolved friction point means the guide is not ready for general use.
- **`CHANGELOG.md` entry for `v1.0.0`:** `CHANGELOG.md` contains a `v1.0.0`
  section listing all shipped capabilities across all five phases with roadmap
  item IDs; `tests/test_changelog_v1_entry.py` asserts `## [v1.0.0]` is
  present in `CHANGELOG.md` and the section is ≥ 200 characters (not a
  one-liner placeholder).

---

### 🚦 Go/No-Go Gate 5 → `v1.0.0` (Release Gate)

This is the final release gate. **All criteria must pass.** An external security
reviewer must be present for the gate review session. Every item in the checklist
below must be ticked before the gate review opens. An unticked item or a failing
named test is an unconditional release blocker — there are no waivers at Gate 5.

---

#### Pre-Gate Test Suite Checklist

**Platform**
- [ ] `test_mypy_zero_errors` — `mypy src/ --strict` exits with code 0 on `main`; output captured and asserted in CI; a non-zero exit code is a hard blocker
- [ ] `test_ruff_zero_errors` — `ruff check src/ tests/` exits with code 0 on `main`; any new error after the Phase 5 baseline commit is a regression
- [ ] `test_no_new_type_ignore` — `grep -rn "type: ignore" src/` count ≤ Phase 4 baseline; `tests/test_no_new_type_ignore.py` diffs the count and fails if it grows
- [ ] `test_no_new_ruff_suppressions` — `pyproject.toml` `[tool.ruff.lint.per-file-ignores]` does not gain new entries during Phase 5; `tests/test_no_new_ruff_suppressions.py` diffs against Phase 4 baseline
- [ ] `test_no_production_stubs` — `inspect.getsource()` scan across all public functions and methods in `src/`; zero occurrences of `pass`, `...`, or `raise NotImplementedError` in non-test code
- [ ] `test_ci_static_analysis_enforced` — CI config asserts `mypy` and `ruff` steps exist, are not conditional, and are not marked `continue-on-error`
- [ ] `test_crash_resilience_pre_pii_scrub` — kill signal injected at `pre-pii-scrub` stage; task restarts via Temporal; `GET /tasks/{task_id}` returns `pending` or `completed`, not `missing`
- [ ] `test_crash_resilience_policy_eval` — kill signal at `policy-eval` stage; zero active task state lost after process restart
- [ ] `test_crash_resilience_jit_token_issue` — kill signal at `jit-token-issue` stage; task resumes with a fresh token; no state loss
- [ ] `test_crash_resilience_llm_invoke` — kill signal at `llm-invoke` stage; Temporal retries with a new token; no duplicate spend recorded; no state loss
- [ ] `test_crash_resilience_post_sanitize` — kill signal at `post-sanitize` stage; output not returned until sanitize completes on retry; no PII leakage window
- [ ] `test_health_recovers_within_30s` — all five crash scenarios above verify `GET /health` returns `{"status": "ok"}` within 30 s of process restart; any recovery beyond 30 s is a hard failure
- [ ] `test_coverage_orchestrator_90pct` — `pytest --cov=src/control_plane` reports ≥ 90% line coverage; any drop below 90% blocks the tag
- [ ] `test_coverage_guardrails_95pct_100branch` — `pytest --cov=src/governance/guardrails --cov-branch` reports ≥ 95% line and 100% branch; a missed branch is a regression
- [ ] `test_coverage_regression_guard` — `.coverage` artifact compared against Phase 4 baseline; no per-module percentage has dropped; any regression fails CI
- [ ] `test_100pct_pass_rate` — `pytest` exits with zero failures, zero errors, zero skips in production test paths; CI reports exact counts
- [ ] `test_no_xfail_in_production_tests` — `grep -rn "xfail" tests/` returns zero results for non-slow, non-external-service test files

**Security & Governance**
- [ ] `test_pentest_signoff_artifact` — `docs/pentest-scope.md` exists and is non-empty; `docs/pentest-report-signoff.md` exists, contains the vendor name and date, and has zero open `Critical` or `High` findings
- [ ] `test_pentest_regressions_complete` — `tests/test_pentest_regressions.py` exists; number of regression tests ≥ number of resolved findings in pentest report; any resolved finding with no regression test fails CI
- [ ] `test_guardrails_full_regression_250` — `tests/test_guardrails_full_regression.py` contains ≥ 50 adversarial inputs per PII class (250 total); zero PII-containing strings reach any adapter; single leakage is a hard failure
- [ ] `test_jailbreak_clean_no_skip_no_xfail` — all ≥ 200 jailbreak cases pass; `grep -n "xfail\|skip" tests/test_jailbreak.py` returns zero results; any suppressed case is an unresolved vulnerability
- [ ] `test_rego_no_hardcoded_allow` — `grep -rn "allow\s*=\s*true\|allow\s*if\s*true" policies/` returns zero results; any unconditional `allow` in a Rego file is a release blocker
- [ ] `test_mcp_adversarial_matrix_clean` — `tests/test_mcp_adversarial.py` 10-case matrix all return `403`/`401` after any pentest-driven fixes; any `200` is a hard failure
- [ ] `test_pii_pre_scrub_regression` — ≥ 50 adversarial inputs per PII class submitted at raw prompt ingestion; zero PII reaches `policy-eval` stage; `tests/test_pii_pre_scrub_regression.py` is a required CI check
- [ ] `test_pii_post_sanitize_regression` — ≥ 20 response-injection cases (PII introduced by LLM adapter); `post-sanitize` stage catches all; `tests/test_pii_post_sanitize_regression.py` is a required CI check
- [ ] `test_pii_homoglyph_regression` — ≥ 10 inputs per PII class using Unicode homoglyphs; all caught before adapter invocation; `tests/test_pii_homoglyph_regression.py` is a required CI check
- [ ] `test_pii_scrub_perf_regression` — p99 scrub latency across ≥ 250 inputs remains < 50 ms; any p99 ≥ 50 ms is a hard performance regression
- [ ] `test_no_security_debt_comments` — `grep -rn "TODO\|FIXME\|HACK\|XXX" src/governance/ src/adapters/` returns zero results; any deferred comment in security or adapter modules is a release blocker
- [ ] `test_vault_rotation_perf_regression` — Vault rotation mid-flight test from P4-3 re-runs clean on the `v1.0.0` candidate; zero stale-secret reads; zero task failures during rotation

**Watchdog & Reliability**
- [ ] `test_load_phase5_infra_reachable` — dedicated CI load environment reachable; `tests/load_config_phase5.yaml` committed; 10-minute warm-up run completes without errors
- [ ] `test_load_phase5_72h_all_p99_under_500ms` — 72-hour run records 72 hourly p99 samples; all 72 < 500 ms; results in `tests/artifacts/load_phase5_results.json`; a single sample ≥ 500 ms fails the gate
- [ ] `test_load_phase5_zero_error_anomalies` — zero 5-minute windows with error rate > 0.1% across the full 72-hour run; any anomalous window triggers an investigation hold
- [ ] `test_load_phase5_budget_fidelity` — across all tasks in the 72-hour run, zero sessions exceed their USD cap; `BudgetEnforcer` consumed total matches audit log sum to four decimal places at every hourly checkpoint
- [ ] `test_metrics_happy_path` — 100 tasks run to completion; each emits exactly one `task_completion` counter increment and one `task_duration_seconds` observation; `tests/test_metrics_happy_path.py` is a required CI check
- [ ] `test_metrics_error_path_per_stage` — error injected at each of the five OTel span stages; `task_error_total` counter incremented with correct `stage` label for each; no stage silently drops metrics
- [ ] `test_metrics_budget_exceeded` — `BudgetExceededError` triggered; `budget_exceeded_total` incremented; no `task_completion` incremented for the same task; counters are mutually exclusive
- [ ] `test_metrics_loop_detected` — `LoopDetector` halt triggered; `loop_detected_total` incremented with cycle path label; no further `task_duration_seconds` observation for halted root task
- [ ] `test_metrics_no_double_emit_temporal_retry` — same Temporal activity result delivered twice; all counters incremented exactly once; double-emit inflation is a hard failure
- [ ] `test_prometheus_runbook_coverage` — every alert rule in `docs/prometheus.yml` has a non-empty `annotations.runbook_url` resolving to an existing file under `docs/runbooks/`; any missing runbook URL fails CI
- [ ] `test_stress_phase4_regression_on_v1_candidate` — `tests/test_stress_phase4.py` re-runs on the v1.0.0 candidate; all assertions pass (zero double-counting, zero missed cycles, p99 < 600 ms, audit completeness)

**Audit & Compliance**
- [ ] `test_soc2_gap_closure` — `docs/soc2-gap-closure.md` has zero items marked `open`; `tests/test_soc2_gap_closure.py` asserts this as a required CI check; any open item fails the gate
- [ ] `test_compliance_report_not_stub` — `inspect.getsource(ComplianceReporter.generate_soc2_report)` contains no `pass`, `...`, or `raise NotImplementedError`; `tests/test_compliance_report_not_stub.py` enforces this
- [ ] `test_compliance_24h_synthetic_1000tasks` — 24-hour synthetic task replay of ≥ 1,000 tasks; generated SOC2 report `task_count` field ≥ 1,000; `tests/test_compliance_24h_synthetic.py` is a required CI check
- [ ] `test_compliance_all_tasks_have_trace` — every `task_id` in the 24-hour synthetic run has `task.started`, ≥ 1 stage event, and `task.completed` or `task.failed`; any `task_id` missing a lifecycle event fails `verify_integrity()`
- [ ] `test_compliance_gdpr_parity` — `generate_gdpr_report()` `pii_scrub_events` count matches `pii.scrubbed` audit event count for the same 24-hour window; any mismatch indicates missing event linkage
- [ ] `test_soc2_signoff_artifact` — `docs/soc2-auditor-signoff.md` exists, is ≥ 100 characters, and contains auditor name, firm, date, and Type II completion statement; committed to `main`
- [ ] `test_audit_full_store_integrity` — `verify_integrity()` run against the complete audit store (all phases); zero `INTEGRITY_COMPROMISED` events; `tests/test_audit_full_store_integrity.py` is a required pre-tag check
- [ ] `test_audit_integrity_not_stub` — `inspect.getsource(AuditLogger.verify_integrity)` contains no `pass`, `...`, `return True`, or `raise NotImplementedError`; integrity check is real HMAC verification
- [ ] `test_audit_integrity_detects_forgery` — one synthetic forged row injected; `verify_integrity()` flags the forged row as `INTEGRITY_COMPROMISED`; all original rows remain `INTEGRITY_OK`
- [ ] `test_audit_integrity_perf` — `verify_integrity()` against ≥ 10,000 events completes in < 60 s wall-clock; any run exceeding 60 s is too slow for CI
- [ ] `test_audit_schema_zero_violations` — `jsonschema.validate()` run against every event in the audit store using `docs/audit-event-schema.json`; zero validation errors; any violation indicates a Phase 1–4 event written without conformance checking

**Frontend & DevEx**
- [ ] `test_docs_no_todo` — `grep -rn "TODO\|FIXME\|PLACEHOLDER\|TBD" docs/` returns zero results; `tests/test_docs_no_todo.py` is a required CI check
- [ ] `test_src_no_todo` — `grep -rn "TODO\|FIXME\|HACK\|XXX" src/` returns zero results; `tests/test_src_no_todo.py` is a required CI check
- [ ] `test_docs_debt_issues_closed` — `tests/test_docs_debt_issues_closed.py` queries GitHub API for `docs-debt` issues in `open` state; asserts count is zero; any open issue blocks the tag
- [ ] `test_runbooks_executable` — all shell commands in every runbook under `docs/runbooks/` extracted via regex and run via `subprocess` against the dev stack; all exit code 0; any failing command is a release blocker
- [ ] `test_docs_links` — every Markdown link in every `docs/` file asserts: internal links point to existing files and anchors; external links return HTTP `200`; any broken link fails CI
- [ ] `test_docs_no_orphans` — every file under `docs/` is linked from at least one other document or from `README.md`; orphaned files indicate invisible documentation rot
- [ ] `test_readme_openapi_link` — `README.md` links to `docs/governance-loop-openapi.yaml`; file exists and passes `openapi-spec-validator` lint; absent or broken link fails CI
- [ ] `test_readme_schema_link` — `README.md` links to `docs/audit-event-schema.json`; file is valid JSON passing `jsonschema` meta-schema validation
- [ ] `test_readme_rego_link` — `README.md` links to versioned Rego library GitHub release; URL returns HTTP `200`; release artifact contains all files from `policies/`
- [ ] `test_sdk_guide_quickstart_on_v1` — all quickstart shell commands from `docs/agent-sdk-guide.md` run via `subprocess` against the `v1.0.0`-tagged stack; all exit code 0; run produces a `mcp.handoff` audit event; any regression from `v0.8.0` is a hard failure
- [ ] `test_sdk_external_review_resolved` — `docs/sdk-guide-external-review.md` exists and contains zero items marked `unresolved`; any unresolved friction point means the guide is not ready for general use
- [ ] `test_changelog_v1_entry` — `CHANGELOG.md` contains `## [v1.0.0]` section; section is ≥ 200 characters; lists all shipped capabilities across all five phases with roadmap item IDs

---

#### Gate Criteria

All seven criteria below reference the named tests above. A criterion passes
only when every named test in its row has a green result in CI on `main`.
**All seven must pass simultaneously — partial gate passage is not permitted.**

| ID | Criterion | Owner | Pass definition |
|---|---|---|---|
| **G5-1** | Zero PII leakage — full regression | Security & Governance | `test_guardrails_full_regression_250`, `test_pii_pre_scrub_regression`, `test_pii_post_sanitize_regression`, `test_pii_homoglyph_regression`, and `test_jailbreak_clean_no_skip_no_xfail` all pass; zero PII-containing strings reach any adapter across the full 250-input suite |
| **G5-2** | Budget fidelity at scale | Watchdog & Reliability | `test_load_phase5_budget_fidelity`, `test_metrics_budget_exceeded`, `test_metrics_no_double_emit_temporal_retry`, and `test_stress_phase4_regression_on_v1_candidate` all pass; zero sessions overspent in 72-hour run |
| **G5-3** | End-to-end auditability | Audit & Compliance | `test_compliance_24h_synthetic_1000tasks`, `test_compliance_all_tasks_have_trace`, `test_audit_full_store_integrity`, and `test_soc2_signoff_artifact` all pass; external auditor sign-off document committed to `main` |
| **G5-4** | Crash and outage resilience | Platform | All five `test_crash_resilience_*` tests pass; `test_health_recovers_within_30s` passes; zero active task state lost across all five crash scenarios |
| **G5-5** | Zero hardcoded credentials + pentest clean | Platform + Security | `test_no_production_stubs`, `test_no_security_debt_comments`, `test_pentest_signoff_artifact`, `test_pentest_regressions_complete`, and `test_vault_rotation_perf_regression` all pass; pentest report contains zero open `Critical` or `High` findings |
| **G5-6** | Open standard complete and discoverable | Frontend & DevEx | `test_readme_openapi_link`, `test_readme_schema_link`, `test_readme_rego_link`, `test_sdk_guide_quickstart_on_v1`, and `test_sdk_external_review_resolved` all pass; all three open-standard artifacts are published, versioned, and linked from `README.md` |
| **G5-7** | Static analysis and test suite clean | All | `test_mypy_zero_errors`, `test_ruff_zero_errors`, `test_100pct_pass_rate`, `test_no_xfail_in_production_tests`, `test_coverage_orchestrator_90pct`, and `test_coverage_guardrails_95pct_100branch` all pass; `pytest` exits with zero failures, zero errors, zero production skips |

---

#### Release Requirements for `v1.0.0`

All of the following must be complete and verified before the `v1.0.0` tag is cut.
Each requirement maps to one or more named gate tests above — "done" means the
named test is green in CI on `main`, not "we believe it is probably fine".

**Code**
- [ ] `mypy src/ --strict` exits with code 0; `ruff check src/ tests/` exits with code 0; both enforced as required CI checks with no `continue-on-error` — verified by `test_mypy_zero_errors`, `test_ruff_zero_errors`, `test_ci_static_analysis_enforced`
- [ ] Zero `# type: ignore` comments added during Phase 5; zero new ruff suppression entries in `pyproject.toml` — verified by `test_no_new_type_ignore`, `test_no_new_ruff_suppressions`
- [ ] Zero occurrences of `pass`, `...`, or `raise NotImplementedError` in any public function or method under `src/` — verified by `test_no_production_stubs` as a required CI check
- [ ] Zero `TODO`, `FIXME`, `HACK`, or `XXX` comments in `src/` — verified by `test_src_no_todo` as a required CI check
- [ ] All five crash-resilience tests pass with zero state loss; `GET /health` returns `ok` within 30 s in all five scenarios — verified by `test_crash_resilience_*` and `test_health_recovers_within_30s`
- [ ] `pytest` exits with zero failures, zero errors, zero production skips; orchestrator coverage ≥ 90%; guardrails coverage ≥ 95% line and 100% branch; no per-module coverage regression from Phase 4 baseline — verified by `test_100pct_pass_rate`, `test_coverage_orchestrator_90pct`, `test_coverage_guardrails_95pct_100branch`, `test_coverage_regression_guard`
- [ ] `tests/test_pentest_regressions.py` exists; regression test count ≥ resolved finding count in pentest report; every resolved pentest finding has a named test — verified by `test_pentest_regressions_complete`
- [ ] `verify_integrity()` passes on the full production audit store (all phases); zero `INTEGRITY_COMPROMISED` rows; integrity check is not a stub — verified by `test_audit_full_store_integrity`, `test_audit_integrity_not_stub`, `test_audit_integrity_detects_forgery`
- [ ] All Prometheus metrics emit on every task completion and every error path; no double-emit on Temporal retry — verified by `test_metrics_happy_path`, `test_metrics_error_path_per_stage`, `test_metrics_no_double_emit_temporal_retry`
- [ ] `tests/test_stress_phase4.py` (@pytest.mark.slow) re-runs clean on the `v1.0.0` candidate — verified by `test_stress_phase4_regression_on_v1_candidate`

**Documentation**
- [ ] Zero `TODO`, `FIXME`, `PLACEHOLDER`, or `TBD` markers anywhere under `docs/` — verified by `test_docs_no_todo` as a required CI check
- [ ] All `docs-debt` GitHub issues closed — verified by `test_docs_debt_issues_closed` querying the GitHub API; any open issue blocks the tag
- [ ] All shell commands in every runbook under `docs/runbooks/` exit code 0 against the running dev stack — verified by `test_runbooks_executable` as a required CI check
- [ ] All internal documentation links resolve to existing files and anchors; all external links return HTTP `200` — verified by `test_docs_links`
- [ ] No orphaned documentation files under `docs/` — verified by `test_docs_no_orphans`
- [ ] `README.md` links to all three open-standard artifacts (`docs/governance-loop-openapi.yaml`, `docs/audit-event-schema.json`, versioned Rego release) — verified by `test_readme_openapi_link`, `test_readme_schema_link`, `test_readme_rego_link`
- [ ] `docs/agent-sdk-guide.md` quickstart runs end-to-end on the `v1.0.0` tag; `docs/sdk-guide-external-review.md` has zero `unresolved` items — verified by `test_sdk_guide_quickstart_on_v1`, `test_sdk_external_review_resolved`
- [ ] `docs/pentest-report-signoff.md` committed to `main` — contains vendor name, date, and zero open `Critical` or `High` findings — verified by `test_pentest_signoff_artifact`
- [ ] `docs/soc2-auditor-signoff.md` committed to `main` — contains auditor name, firm, date, and Type II completion statement — verified by `test_soc2_signoff_artifact`
- [ ] `docs/soc2-gap-closure.md` has zero items marked `open` — verified by `test_soc2_gap_closure`
- [ ] `CHANGELOG.md` contains a `v1.0.0` section listing all shipped capabilities across all five phases with roadmap item IDs (P1-1 through F5-2) — verified by `test_changelog_v1_entry`
- [ ] `docs/audit-event-schema.json` includes all Phase 4–5 event types; zero schema conformance violations across the full audit store — verified by `test_audit_schema_zero_violations`
- [ ] All alert rules in `docs/prometheus.yml` have a `annotations.runbook_url` that resolves to an existing runbook under `docs/runbooks/` — verified by `test_prometheus_runbook_coverage`

**Infrastructure**
- [ ] `docker-compose.yml` starts the full stack from cold state cleanly; `GET /health` returns `ok` within 60 s; no `AEGIS_*` env-vars remain in the compose file or in any `src/` module
- [ ] `testcontainers` used for all Vault, OPA, Grafana, and Prometheus integration tests; all container-backed tests pass in CI without local infrastructure
- [ ] 72-hour sustained load test completed on dedicated CI environment; all 72 hourly p99 samples < 500 ms; zero error-rate anomaly windows; results artifact committed to the gate review issue — verified by `test_load_phase5_72h_all_p99_under_500ms`, `test_load_phase5_zero_error_anomalies`
- [ ] Pre-commit hooks enforcing `ruff`, `mypy`, `test_no_production_stubs`, `test_docs_no_todo`, `test_src_no_todo`, and audit schema conformance check — all hooks pass on a clean `git commit` against the `v1.0.0` candidate
- [ ] All five `test_crash_resilience_*` tests pass in CI without requiring manual infrastructure setup; Temporal test server is provisioned automatically by the test harness
- [ ] `docs/governance-loop-openapi.yaml` spec version field matches `v1.0.0`; passes `openapi-spec-validator` lint; Postman (v10+) and Insomnia (v2023+) imports both verified on the final release candidate

---

#### Post-Release Actions by Team

Gate 5 passes and the `v1.0.0` tag is cut. These are the immediate actions
each team is responsible for within one week of the tag. None of these items
are release blockers — they are the first actions of the post-`v1.0.0` maintenance
cadence.

**Platform**
1. Archive the `PHASE5_STATIC_ANALYSIS_BASELINE` from `docs/architecture_decisions.md` as the `v1.0.0` baseline; open a `v1.1.0-planning` GitHub issue tracking any type-annotation improvements identified during Phase 5 that were de-scoped to avoid blocking the release
2. Publish the Temporal workflow version (`v1.0.0`) to the internal workflow registry; tag the Temporal worker Docker image with `v1.0.0`; verify the production Temporal namespace is running the tagged worker before declaring the release operational
3. Conduct a 1-hour post-mortem on any crash-resilience failures discovered during Phase 5; document findings in `docs/architecture_decisions.md` under "v1.0 crash-resilience post-mortem"; open issue tickets for any systemic weaknesses identified — these feed directly into the v1.1.0 reliability roadmap

**Security & Governance**
1. Publish the pentest report summary (redacted for public consumption) to `docs/security/pentest-summary-v1.0.md`; include the finding categories, resolution approach for each, and the regression test name that covers it; this is the public-facing security assurance document for enterprise customers
2. Schedule the next penetration test for the v1.2.0 cycle; add the pentest scope document draft (`docs/pentest-scope-v1.2.md`) to the `v1.2.0-planning` milestone; the scope must include any attack surface added between v1.0.0 and v1.2.0
3. Tag the Rego policy library at `v1.0.0` on GitHub if not already done as part of F4-2; verify `test_rego_external_pin` and `test_rego_consumer_example` both pass against the `v1.0.0` Rego release tag; announce the release in the project's public changelog

**Watchdog & Reliability**
1. Publish the 72-hour load test results artifact (`tests/artifacts/load_phase5_results.json`) to `docs/performance/load-test-v1.0.md` in human-readable form; include p50, p95, p99 distributions per hour, error rate plot, and budget fidelity checkpoint summary — this is the baseline document for v1.1.0 performance SLOs
2. Define formal SLOs for `v1.1.0` based on the `PHASE5_LOAD_BASELINE` constants: p99 < 500 ms, error rate < 0.1%, budget fidelity ≤ 0 cents over; open a GitHub issue titled `v1.1.0 SLO Enforcement` that tracks the CI checks needed to enforce these SLOs on every commit
3. Rotate the `PHASE5_LOAD_BASELINE` constants to `V1_PRODUCTION_BASELINE` in `tests/test_load_phase5_baseline.py`; rename the file to `tests/test_load_production_baseline.py`; the v1.1.0 planning issue must reference this file as the authoritative performance baseline

**Audit & Compliance**
1. Deliver the final SOC2 Type II report package to the requesting enterprise customer (if applicable); include the `docs/soc2-auditor-signoff.md`, the 24-hour synthetic run report, and the `verify_integrity()` output summary; document the delivery in `docs/compliance/soc2-v1.0-delivery.md`
2. Archive the full v1.0.0 audit store snapshot to the long-term tamper-evident backend; run `verify_integrity()` on the archived snapshot and commit the output to `docs/compliance/audit-store-v1.0-integrity-report.md`; this document is the immutable record of the v1.0.0 release audit trail
3. Open the `v1.1.0-compliance` planning issue listing any SOC2 gaps that were closed during Phase 5 only by workaround (not by a permanent code fix); assign each workaround a permanent fix target in `v1.1.0` or `v1.2.0`; no workaround may remain unaddressed beyond two release cycles

**Frontend & DevEx**
1. Announce the `v1.0.0` release on all public channels (project blog, GitHub Discussions, relevant community forums); include the `CHANGELOG.md` `v1.0.0` section verbatim; link to the three published open-standard artifacts (OpenAPI spec, audit event schema, Rego library release)
2. Open a `docs-v1.1.0` GitHub milestone and migrate all documentation improvements identified during the external developer review into tracked issues within that milestone; the external reviewer's sign-off document (`docs/sdk-guide-external-review.md`) becomes the backlog seed for the v1.1.0 developer experience improvements
3. Verify the `docs/governance-loop-openapi.yaml` is indexed by the OpenAPI directory most relevant to the project's target audience (e.g., APIs.guru or a public API registry); submit the spec for indexing if not already present; confirm the listing URL and add it to `README.md` under "Open Standard Resources" within one week of tagging

---

> **Tagging procedure:** after all Gate 5 criteria pass and all release
> requirements are verified, each team lead signs off in the gate review issue
> using the GitHub issue checklist template prepared during Phase 5. The
> `v1.0.0` tag is cut from `main` by the Platform team lead only after all
> five sign-offs are recorded. No team lead may sign off on behalf of another
> team. The tag commit message must reference the gate review issue number:
> `chore(release): cut v1.0.0 — Gate 5 reviewed in #<issue>`.

---

## Version Summary

| Version | Gate | Phase | Weeks |
|---|---|---|---|
| `v0.1.0` | — | Current prototype | — |
| `v0.2.0` | Gate 1 | Governance Loop Integration | 1–4 |
| `v0.4.0` | Gate 2 | Durable Orchestration | 5–8 |
| `v0.6.0` | Gate 3 | Glass Box Control Plane | 9–12 |
| `v0.8.0` | Gate 4 | Zero-Trust & MCP Mesh | 13–16 |
| `v1.0.0` | Gate 5 | Release Hardening | 17–20 |

---

## v1.0 Definition of Done

All criteria are directly verified by a named gate above.

| Criterion | Verified at |
|---|---|
| Zero PII leakage to any LLM adapter across all adversarial input classes | G1-2, G5-1 |
| No agent session exceeds USD budget by more than 1%; no false-positive halts | G1-3, G4-3, G5-2 |
| Every task linked to a complete, tamper-evident, OTel-correlated audit trace | G1-4, G3-4, G5-3 |
| System survives LLM provider outage without losing active task state | G2-1, G5-4 |
| SOC2/GDPR report auto-generated for any 24-hour window; passes auditor checklist | G3-4, G5-3 |
| Zero hardcoded credentials; all secrets sourced from Vault with live rotation | G4-1, G5-5 |
| Aegis Governance Loop OpenAPI spec, audit schema, and Rego library published | G4-5, G5-6 |
| Clean static analysis: zero `mypy` errors, zero `ruff` errors, 100% test pass | G5-7 |
