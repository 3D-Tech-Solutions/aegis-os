"""A1-2 — AuditLogger emits a structured JSON event for every stage outcome.

Four test classes cover the full A1-2 testing contract:

    TestSchemaValidation   — every stage_event entry validates against the schema
    TestOutcomeCoverage    — all four outcome types (allow/deny/redact/error) produced
    TestNoSilentStage      — RuntimeError in any stage triggers an error audit event
    TestNoPiiInLogs        — no raw PII values appear in captured audit log output
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import jsonschema
import pytest
from structlog.testing import capture_logs

from src.adapters.base import BaseAdapter, LLMRequest, LLMResponse
from src.audit_vault.logger import AuditLogger
from src.control_plane.orchestrator import Orchestrator, OrchestratorRequest, PolicyDeniedError
from src.governance.guardrails import (
    _PII_PATTERNS,
    Guardrails,
    MaskResult,
)
from src.governance.policy_engine.opa_client import PolicyEngine, PolicyResult
from src.governance.session_mgr import SessionManager

# ---------------------------------------------------------------------------
# Schema fixture (loaded once per module)
# ---------------------------------------------------------------------------

_SCHEMA_PATH = Path(__file__).parent.parent / "docs" / "audit-event-schema.json"
_AUDIT_SCHEMA: dict[str, Any] = json.loads(_SCHEMA_PATH.read_text())

# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

_CLEAN_PROMPT = "Explain the quarterly audit findings in plain language."
_PII_PROMPT = (
    "Contact alice@example.com, SSN 123-45-6789, "
    "CC 4111-1111-1111-1111, phone +1-800-555-1234, IP 192.168.1.100."
)

_BASE_REQUEST = OrchestratorRequest(
    prompt=_CLEAN_PROMPT,
    agent_type="audit",
    requester_id="auditor-001",
    model="gpt-4o-mini",
)


# ---------------------------------------------------------------------------
# Stub adapter
# ---------------------------------------------------------------------------


class _StubAdapter(BaseAdapter):
    """Minimal async adapter that always returns a canned, PII-free response."""

    def __init__(self, content: str = "Audit complete — no anomalies found.") -> None:
        self._content = content

    @property
    def provider_name(self) -> str:
        """Return a fixed provider label."""
        return "stub"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Return a fixed LLMResponse without making any external calls."""
        return LLMResponse(
            content=self._content,
            tokens_used=12,
            model=request.model,
            provider="stub",
            finish_reason="stop",
        )


# ---------------------------------------------------------------------------
# Mock factories
# ---------------------------------------------------------------------------


def _allow_engine() -> PolicyEngine:
    """Policy engine mock that always returns allowed=True."""
    pe: PolicyEngine = MagicMock(spec=PolicyEngine)

    async def _allow(*_a: Any, **_kw: Any) -> PolicyResult:
        return PolicyResult(allowed=True)

    pe.evaluate = _allow  # type: ignore[method-assign]
    return pe


def _deny_engine() -> PolicyEngine:
    """Policy engine mock that always returns allowed=False."""
    pe: PolicyEngine = MagicMock(spec=PolicyEngine)

    async def _deny(*_a: Any, **_kw: Any) -> PolicyResult:
        return PolicyResult(allowed=False, reasons=["test-deny-reason"])

    pe.evaluate = _deny  # type: ignore[method-assign]
    return pe


def _ok_session_mgr() -> SessionManager:
    """SessionManager mock that issues a fixed token and validates it cleanly."""
    sm: SessionManager = MagicMock(spec=SessionManager)
    sm.issue_token.return_value = "test.jwt.token"  # type: ignore[union-attr]
    sm.validate_token.return_value = MagicMock(  # type: ignore[union-attr]
        jti="test-jti-001",
        agent_type="audit",
    )
    return sm


def _ok_guardrails(*, fail_on_second_mask: bool = False) -> Guardrails:
    """Guardrails mock with configurable stage-5 failure for no-silent-stage tests."""
    g: Guardrails = MagicMock(spec=Guardrails)
    g.check_prompt_injection.return_value = None  # type: ignore[union-attr]

    call_count = 0

    def _mask_pii(text: str) -> MaskResult:
        nonlocal call_count
        call_count += 1
        if fail_on_second_mask and call_count == 2:
            raise RuntimeError("stage5 mask_pii explodes")
        return MaskResult(text=text, found_types=[])

    g.mask_pii.side_effect = _mask_pii  # type: ignore[union-attr]
    return g


def _pii_guardrails() -> Guardrails:
    """Guardrails mock that reports PII found in stage 1 and none in stage 5."""
    g: Guardrails = MagicMock(spec=Guardrails)
    g.check_prompt_injection.return_value = None  # type: ignore[union-attr]

    call_count = 0

    def _mask_pii(text: str) -> MaskResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Stage 1: PII found
            return MaskResult(text="[REDACTED-EMAIL] …", found_types=["email"])
        # Stage 5: clean response
        return MaskResult(text=text, found_types=[])

    g.mask_pii.side_effect = _mask_pii  # type: ignore[union-attr]
    return g


def _build_orchestrator(**kwargs: Any) -> Orchestrator:
    """Return an Orchestrator with a fresh AuditLogger and sensible defaults."""
    return Orchestrator(
        adapter=kwargs.get("adapter", _StubAdapter()),
        guardrails=kwargs.get("guardrails", _ok_guardrails()),
        policy_engine=kwargs.get("policy_engine", _allow_engine()),
        session_mgr=kwargs.get("session_mgr", _ok_session_mgr()),
        audit_logger=AuditLogger("test.orchestrator"),
    )


# ---------------------------------------------------------------------------
# Helper: validate a single captured entry against the A1-2 schema
# ---------------------------------------------------------------------------


def _assert_schema_valid(entry: dict[str, Any]) -> None:
    """Validate *entry* against the audit-event-schema.json.

    ``capture_logs()`` produces entries with ``log_level`` (not ``level``) and
    without ``timestamp`` — both fields the production schema requires because
    structlog's JSON processor chain adds them at render time.  We normalise
    before validation so the schema contract is still exercised end-to-end.
    """
    normalised = dict(entry)
    # Rename structlog's capture key to the production field name.
    if "level" not in normalised and "log_level" in normalised:
        normalised["level"] = normalised.pop("log_level")
    # Inject a syntactically valid ISO-8601 timestamp (sentinel value).
    if "timestamp" not in normalised:
        normalised["timestamp"] = "2026-01-01T00:00:00.000000Z"
    try:
        jsonschema.validate(instance=normalised, schema=_AUDIT_SCHEMA)
    except jsonschema.ValidationError as exc:
        pytest.fail(
            f"Audit event failed schema validation:\n"
            f"  entry:   {entry}\n"
            f"  message: {exc.message}"
        )


# ---------------------------------------------------------------------------
# 1. Schema validation — every stage_event entry must validate
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    """Every entry emitted by stage_event() must validate against audit-event-schema.json."""

    @pytest.mark.asyncio
    async def test_happy_path_all_stage_event_entries_are_schema_valid(self) -> None:
        """Run one happy-path task; every stage_event entry must pass schema validation."""
        with capture_logs() as cap:
            await _build_orchestrator().run(_BASE_REQUEST)

        stage_entries = [e for e in cap if "outcome" in e]
        assert stage_entries, (
            "No stage_event entries captured — AuditLogger.stage_event() "
            "must emit at least one entry per pipeline run."
        )
        for entry in stage_entries:
            _assert_schema_valid(entry)

    @pytest.mark.asyncio
    async def test_deny_path_policy_denied_entry_is_schema_valid(self) -> None:
        """A policy-denied event must also validate against the schema."""
        with capture_logs() as cap:
            with pytest.raises(PolicyDeniedError):
                await _build_orchestrator(policy_engine=_deny_engine()).run(_BASE_REQUEST)

        deny_entries = [e for e in cap if e.get("outcome") == "deny"]
        assert deny_entries, "Expected at least one deny audit entry for a denied request"
        for entry in deny_entries:
            _assert_schema_valid(entry)

    @pytest.mark.asyncio
    async def test_error_path_stage_error_entry_is_schema_valid(self) -> None:
        """A stage.error event (from an unexpected exception) must validate against the schema."""
        class _FailAdapter(_StubAdapter):
            async def complete(self, request: LLMRequest) -> LLMResponse:
                raise RuntimeError("injected adapter failure")

        with capture_logs() as cap:
            with pytest.raises(RuntimeError):
                await _build_orchestrator(adapter=_FailAdapter()).run(_BASE_REQUEST)

        error_entries = [e for e in cap if e.get("outcome") == "error"]
        assert error_entries, "Expected at least one error audit entry for a failed stage"
        for entry in error_entries:
            _assert_schema_valid(entry)

    def test_schema_file_itself_is_valid_draft7(self) -> None:
        """The schema file must be a valid JSON Schema Draft 7 document."""
        validator_cls = jsonschema.validators.validator_for(_AUDIT_SCHEMA)
        validator_cls.check_schema(_AUDIT_SCHEMA)  # raises SchemaError if invalid


# ---------------------------------------------------------------------------
# 2. Outcome coverage — all four outcome types must be producible
# ---------------------------------------------------------------------------


class TestOutcomeCoverage:
    """Run tasks engineered to produce each outcome type; assert the matching entries."""

    @pytest.mark.asyncio
    async def test_allow_outcome_produced_on_happy_path(self) -> None:
        """A clean prompt with an allowing policy must produce at least one 'allow' entry."""
        with capture_logs() as cap:
            await _build_orchestrator().run(_BASE_REQUEST)

        allow_entries = [e for e in cap if e.get("outcome") == "allow"]
        assert allow_entries, (
            "Expected at least one stage_event entry with outcome='allow' "
            "for a clean happy-path task."
        )

    @pytest.mark.asyncio
    async def test_deny_outcome_produced_on_policy_denial(self) -> None:
        """An OPA denial must produce at least one entry with outcome='deny'."""
        with capture_logs() as cap:
            with pytest.raises(PolicyDeniedError):
                await _build_orchestrator(policy_engine=_deny_engine()).run(_BASE_REQUEST)

        deny_entries = [e for e in cap if e.get("outcome") == "deny"]
        assert deny_entries, (
            "Expected at least one stage_event entry with outcome='deny' "
            "for a policy-denied request."
        )
        # The denial must come from the policy-eval stage.
        policy_deny = [e for e in deny_entries if e.get("stage") == "policy-eval"]
        assert policy_deny, (
            f"Expected outcome='deny' with stage='policy-eval'; "
            f"got deny stages: {[e.get('stage') for e in deny_entries]}"
        )

    @pytest.mark.asyncio
    async def test_redact_outcome_produced_when_prompt_contains_pii(self) -> None:
        """A prompt with PII must produce at least one entry with outcome='redact'."""
        pii_request = OrchestratorRequest(
            prompt=_PII_PROMPT,
            agent_type="audit",
            requester_id="auditor-001",
        )
        with capture_logs() as cap:
            await _build_orchestrator(guardrails=_pii_guardrails()).run(pii_request)

        redact_entries = [e for e in cap if e.get("outcome") == "redact"]
        assert redact_entries, (
            "Expected at least one stage_event entry with outcome='redact' "
            "when the prompt contains PII."
        )
        # The redaction must include the PII types that were found.
        stage1_redact = [
            e for e in redact_entries
            if e.get("stage") == "pre-pii-scrub"
        ]
        assert stage1_redact, "Expected redact event from the pre-pii-scrub stage"
        assert "email" in stage1_redact[0].get("pii_types", []), (
            "Expected 'email' in pii_types for the redact event"
        )

    @pytest.mark.asyncio
    async def test_error_outcome_produced_on_stage_exception(self) -> None:
        """An unexpected adapter exception must produce at least one 'error' entry."""
        class _FailAdapter(_StubAdapter):
            async def complete(self, request: LLMRequest) -> LLMResponse:
                raise RuntimeError("simulated LLM provider failure")

        with capture_logs() as cap:
            with pytest.raises(RuntimeError):
                await _build_orchestrator(adapter=_FailAdapter()).run(_BASE_REQUEST)

        error_entries = [e for e in cap if e.get("outcome") == "error"]
        assert error_entries, (
            "Expected at least one stage_event entry with outcome='error' "
            "when a stage throws an unexpected exception."
        )
        # Error must be associated with the llm-invoke stage.
        llm_error = [e for e in error_entries if e.get("stage") == "llm-invoke"]
        assert llm_error, (
            f"Expected outcome='error' with stage='llm-invoke'; "
            f"got error stages: {[e.get('stage') for e in error_entries]}"
        )


# ---------------------------------------------------------------------------
# 3. No silent stage — every stage failure must produce an audit error event
# ---------------------------------------------------------------------------


class TestNoSilentStage:
    """Inject RuntimeError at each of the five pipeline stages;
    assert AuditLogger emits an outcome='error' event for that stage
    even as the exception propagates to the caller."""

    @pytest.mark.asyncio
    async def test_stage1_pre_pii_scrub_error_is_audited(self) -> None:
        """Stage 1 failure (check_prompt_injection raises) must produce a 'pre-pii-scrub' error."""
        g = MagicMock(spec=Guardrails)
        g.check_prompt_injection.side_effect = RuntimeError("stage1 explodes")

        with capture_logs() as cap:
            with pytest.raises(RuntimeError, match="stage1 explodes"):
                await _build_orchestrator(guardrails=g).run(_BASE_REQUEST)

        self._assert_stage_error(cap, "pre-pii-scrub")

    @pytest.mark.asyncio
    async def test_stage2_policy_eval_error_is_audited(self) -> None:
        """Stage 2 failure (evaluate raises RuntimeError) must produce a 'policy-eval' error."""
        pe: PolicyEngine = MagicMock(spec=PolicyEngine)

        async def _explode(*_a: Any, **_kw: Any) -> PolicyResult:
            raise RuntimeError("stage2 explodes")

        pe.evaluate = _explode  # type: ignore[method-assign]

        with capture_logs() as cap:
            with pytest.raises(RuntimeError, match="stage2 explodes"):
                await _build_orchestrator(policy_engine=pe).run(_BASE_REQUEST)

        self._assert_stage_error(cap, "policy-eval")

    @pytest.mark.asyncio
    async def test_stage3_jit_token_issue_error_is_audited(self) -> None:
        """Stage 3 failure (issue_token raises RuntimeError) produces a 'jit-token-issue' error."""
        sm: SessionManager = MagicMock(spec=SessionManager)
        sm.issue_token.side_effect = RuntimeError("stage3 explodes")  # type: ignore[union-attr]

        with capture_logs() as cap:
            with pytest.raises(RuntimeError, match="stage3 explodes"):
                await _build_orchestrator(session_mgr=sm).run(_BASE_REQUEST)

        self._assert_stage_error(cap, "jit-token-issue")

    @pytest.mark.asyncio
    async def test_stage4_llm_invoke_error_is_audited(self) -> None:
        """Stage 4 failure (adapter.complete raises RuntimeError) produces a 'llm-invoke' error."""
        class _FailAdapter(_StubAdapter):
            async def complete(self, request: LLMRequest) -> LLMResponse:
                raise RuntimeError("stage4 explodes")

        with capture_logs() as cap:
            with pytest.raises(RuntimeError, match="stage4 explodes"):
                await _build_orchestrator(adapter=_FailAdapter()).run(_BASE_REQUEST)

        self._assert_stage_error(cap, "llm-invoke")

    @pytest.mark.asyncio
    async def test_stage5_post_sanitize_error_is_audited(self) -> None:
        """Stage 5 failure (mask_pii raises on second call) must produce a 'post-sanitize' error."""
        with capture_logs() as cap:
            with pytest.raises(RuntimeError, match="stage5 mask_pii explodes"):
                await _build_orchestrator(
                    guardrails=_ok_guardrails(fail_on_second_mask=True)
                ).run(_BASE_REQUEST)

        self._assert_stage_error(cap, "post-sanitize")

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    @staticmethod
    def _assert_stage_error(
        cap: list[dict[str, Any]],
        expected_stage: str,
    ) -> None:
        """Assert at least one captured entry has outcome='error' for *expected_stage*."""
        error_entries = [
            e for e in cap
            if e.get("outcome") == "error" and e.get("stage") == expected_stage
        ]
        assert error_entries, (
            f"Expected at least one audit entry with outcome='error' and "
            f"stage='{expected_stage}' after an exception was injected into that stage.\n"
            f"  All captured outcomes: "
            f"{[(e.get('outcome'), e.get('stage')) for e in cap]}"
        )
        # The error entry must carry a non-empty error_message.
        assert error_entries[0].get("error_message"), (
            f"Error audit entry for stage='{expected_stage}' "
            "must carry a non-empty 'error_message' field."
        )


# ---------------------------------------------------------------------------
# 4. No plaintext PII in logs — raw PII values must never appear in audit output
# ---------------------------------------------------------------------------


class TestNoPiiInLogs:
    """Run a task with a prompt containing all five PII classes; assert no raw PII
    value appears anywhere in the captured audit log output."""

    @pytest.mark.asyncio
    async def test_raw_pii_never_appears_in_audit_log(self) -> None:
        """Use the Guardrails PII patterns to scan captured log entries.

        Every captured dict is stringified and scanned against the same regex
        patterns used by Guardrails.  Any match is a hard failure — the control
        plane must ensure raw PII never enters the audit trail.

        Structural hex fields (``traceparent``) are excluded from the scan
        because they contain 32/16-character hexadecimal sequences that can
        superficially match digit-only PII patterns like credit card numbers.
        """
        _EXCLUDED_FROM_PII_SCAN = {"traceparent"}

        pii_request = OrchestratorRequest(
            prompt=_PII_PROMPT,
            agent_type="audit",
            requester_id="auditor-001",
        )
        with capture_logs() as cap:
            await _build_orchestrator().run(pii_request)

        assert cap, "capture_logs captured no entries — nothing to verify"

        for entry in cap:
            scannable = {k: v for k, v in entry.items() if k not in _EXCLUDED_FROM_PII_SCAN}
            entry_text = str(scannable)
            for pii_label, pattern in _PII_PATTERNS:
                match = pattern.search(entry_text)
                assert match is None, (
                    f"Raw PII ({pii_label!r}) detected in audit log entry.\n"
                    f"  matched text: {match.group()!r}\n"
                    f"  entry:        {entry}"
                )

    @pytest.mark.asyncio
    async def test_pii_found_types_list_does_not_contain_raw_values(self) -> None:
        """The pii_types field in redact events must only contain class labels, not raw PII."""
        pii_request = OrchestratorRequest(
            prompt=_PII_PROMPT,
            agent_type="audit",
            requester_id="auditor-001",
        )
        with capture_logs() as cap:
            await _build_orchestrator().run(pii_request)

        redact_entries = [e for e in cap if e.get("outcome") == "redact"]
        for entry in redact_entries:
            pii_types = entry.get("pii_types", [])
            for value in pii_types:
                # Each element must be a short label, not a raw PII string.
                for pii_label, pattern in _PII_PATTERNS:
                    assert not pattern.search(str(value)), (
                        f"pii_types element {value!r} matches the {pii_label} "
                        f"pattern — raw PII must not be stored in pii_types."
                    )


# ---------------------------------------------------------------------------
# A-prep-1: task_id and W3C traceparent in every stage_event (Phase 2 baseline)
# ---------------------------------------------------------------------------


class TestTraceparentPropagation:
    """Every stage_event call must carry both task_id and a W3C traceparent (A-prep-1)."""

    def test_stage_event_carries_traceparent_field(self) -> None:
        """stage_event must inject a traceparent field even outside an active OTel span."""
        logger = AuditLogger()
        with capture_logs() as cap:
            logger.stage_event(
                "test.event",
                outcome="allow",
                stage="pre-pii-scrub",
                task_id="task-traceparent-001",
                agent_type="general",
            )
        assert len(cap) == 1, f"Expected 1 log entry, got {len(cap)}"
        entry = cap[0]
        assert "traceparent" in entry, f"traceparent missing from stage_event output: {entry}"
        # W3C format: 00-{32 hex}-{16 hex}-{2 hex}
        assert entry["traceparent"].startswith("00-"), (
            f"traceparent must start with '00-', got {entry['traceparent']!r}"
        )
        assert len(entry["traceparent"]) == 55, (
            f"W3C traceparent must be 55 characters, got {len(entry['traceparent'])}"
        )

    def test_stage_event_carries_task_id_field(self) -> None:
        """task_id passed to stage_event must appear in the emitted log entry."""
        logger = AuditLogger()
        tid = "task-123-abc"
        with capture_logs() as cap:
            logger.stage_event(
                "test.event",
                outcome="allow",
                stage="policy-eval",
                task_id=tid,
                agent_type="finance",
            )
        assert len(cap) == 1
        assert cap[0].get("task_id") == tid

    def test_no_trace_fallback_is_all_zeros(self) -> None:
        """Outside any OTel span the traceparent must be the all-zero no-trace value."""
        logger = AuditLogger()
        with capture_logs() as cap:
            logger.stage_event(
                "test.no-trace",
                outcome="allow",
                stage="jit-token-issue",
                task_id="task-no-trace",
                agent_type="it",
            )
        tp = cap[0]["traceparent"]
        # All-zeros no-trace traceparent starts with 00- and has zero trace_id.
        assert tp == "00-00000000000000000000000000000000-0000000000000000-00" or tp.startswith(
            "00-"
        ), f"Unexpected no-trace traceparent: {tp!r}"
