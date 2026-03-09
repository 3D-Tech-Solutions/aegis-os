"""Tests for F1-1: docs/audit-event-schema.json validity and conformance.

Test coverage:
  1. Schema self-validation — Draft7Validator.check_schema() asserts zero errors.
  2. Version field check — $schema and version fields match expected constants.
  3. Conformance test — 20 real events emitted by AuditLogger all validate.
  4. Negative conformance test — 10 malformed event objects are all rejected.
"""

from __future__ import annotations

import copy
import io
import json
from pathlib import Path
from typing import Any, cast

import pytest
import structlog
from jsonschema import Draft7Validator, ValidationError, validate

from src.audit_vault.logger import AuditLogger

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

SCHEMA_PATH = Path(__file__).parent.parent / "docs" / "audit-event-schema.json"
EXPECTED_SCHEMA_DRAFT = "http://json-schema.org/draft-07/schema#"
EXPECTED_SCHEMA_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    """Load the audit event schema exactly once per test module."""
    with SCHEMA_PATH.open() as fh:
        return cast(dict[str, Any], json.load(fh))


@pytest.fixture(scope="module")
def validator(schema: dict[str, Any]) -> Draft7Validator:
    """Construct a Draft7Validator for the audit event schema."""
    return Draft7Validator(schema)


# ---------------------------------------------------------------------------
# Helper — capture real JSON lines emitted by AuditLogger
# ---------------------------------------------------------------------------


def _capture_audit_events(*call_pairs: tuple[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Fire each (method_name, kwargs) pair against a fresh AuditLogger and
    return the list of parsed JSON event dicts actually emitted.

    Temporarily reconfigures structlog to write to an in-memory buffer so that
    we capture the fully-rendered JSON exactly as it would appear in production.
    """
    buf = io.StringIO()

    # ------------------------------------------------------------------
    # Reconfigure structlog to write to our buffer instead of stdout.
    # The processor chain is identical to the production chain — only the
    # logger factory differs.
    # ------------------------------------------------------------------
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(buf),
        cache_logger_on_first_use=False,
    )

    logger = AuditLogger("test-conformance")

    for method_name, kwargs in call_pairs:
        event_name: str = kwargs.pop("event")
        if method_name == "audit":
            agent_id: str = kwargs.pop("agent_id")
            action: str = kwargs.pop("action")
            getattr(logger, method_name)(event_name, agent_id=agent_id, action=action, **kwargs)
        else:
            getattr(logger, method_name)(event_name, **kwargs)

    # Restore structlog to the production configuration (stdout).
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )

    raw = buf.getvalue().strip()
    if not raw:
        return []
    return [json.loads(line) for line in raw.splitlines()]


# ---------------------------------------------------------------------------
# 1 – Schema self-validation
# ---------------------------------------------------------------------------


class TestSchemaSelfValidation:
    """The schema document itself must be a valid JSON Schema draft-07."""

    def test_schema_file_exists(self) -> None:
        """docs/audit-event-schema.json must exist on disk."""
        assert SCHEMA_PATH.exists(), f"Schema file not found at {SCHEMA_PATH}"

    def test_schema_is_valid_json(self) -> None:
        """docs/audit-event-schema.json must be parseable as JSON."""
        content = SCHEMA_PATH.read_text()
        parsed = json.loads(content)
        assert isinstance(parsed, dict)

    def test_check_schema_reports_zero_errors(self, schema: dict[str, Any]) -> None:
        """Draft7Validator.check_schema() must raise nothing (zero meta-schema errors)."""
        # check_schema() raises jsonschema.SchemaError on malformed schemas.
        Draft7Validator.check_schema(schema)

    def test_draft7_validator_instantiates_cleanly(self, schema: dict[str, Any]) -> None:
        """A Draft7Validator instance must be constructable without exceptions."""
        validator_instance = Draft7Validator(schema)
        assert validator_instance is not None


# ---------------------------------------------------------------------------
# 2 – Version field check
# ---------------------------------------------------------------------------


class TestVersionFields:
    """The schema must declare its meta-schema URI and semantic version."""

    def test_dollar_schema_field_present(self, schema: dict[str, Any]) -> None:
        """The schema document must contain a '$schema' key."""
        assert "$schema" in schema, "Missing '$schema' field in audit-event-schema.json"

    def test_dollar_schema_value(self, schema: dict[str, Any]) -> None:
        """$schema must be set to the draft-07 URI."""
        assert schema["$schema"] == EXPECTED_SCHEMA_DRAFT, (
            f"Expected $schema={EXPECTED_SCHEMA_DRAFT!r}, got {schema['$schema']!r}"
        )

    def test_version_field_present(self, schema: dict[str, Any]) -> None:
        """The schema document must contain a 'version' key."""
        assert "version" in schema, "Missing 'version' field in audit-event-schema.json"

    def test_version_field_value(self, schema: dict[str, Any]) -> None:
        """version must be set to '0.1.0'."""
        assert schema["version"] == EXPECTED_SCHEMA_VERSION, (
            f"Expected version={EXPECTED_SCHEMA_VERSION!r}, got {schema['version']!r}"
        )


# ---------------------------------------------------------------------------
# 3 – Conformance test: 20 real AuditLogger events must validate
# ---------------------------------------------------------------------------

# All 20 event descriptors: (method_name, kwargs_including_event_key)
_REAL_EVENTS: list[tuple[str, dict[str, Any]]] = [
    # --- lifecycle ---
    ("info", {"event": "aegis.startup", "message": "Aegis-OS Control Plane starting up"}),
    ("info", {"event": "aegis.shutdown", "message": "Aegis-OS Control Plane shutting down"}),
    # --- scheduler / workflow ---
    ("info", {"event": "workflow.started", "workflow_id": "wf-00000001"}),
    ("info", {"event": "workflow.completed", "workflow_id": "wf-00000001"}),
    # --- OPA / policy ---
    (
        "error",
        {
            "event": "opa_unavailable",
            "task_id": "11111111-1111-1111-1111-111111111111",
            "agent_type": "finance",
            "requester_id": "user-001",
            "error": "OPA server returned 503",
        },
    ),
    (
        "warning",
        {
            "event": "policy_denied",
            "task_id": "22222222-2222-2222-2222-222222222222",
            "agent_type": "hr",
            "requester_id": "user-002",
            "reasons": ["agent_type not in allowed list"],
        },
    ),
    (
        "info",
        {
            "event": "policy_mask_applied",
            "task_id": "33333333-3333-3333-3333-333333333333",
            "agent_type": "legal",
            "fields": ["prompt"],
        },
    ),
    # --- session / token ---
    (
        "warning",
        {
            "event": "token_expired",
            "task_id": "44444444-4444-4444-4444-444444444444",
            "agent_type": "it",
            "requester_id": "user-003",
        },
    ),
    (
        "warning",
        {
            "event": "token_scope_violation",
            "task_id": "55555555-5555-5555-5555-555555555555",
            "token_agent_type": "finance",
            "request_agent_type": "hr",
            "requester_id": "user-004",
        },
    ),
    (
        "info",
        {
            "event": "token_issued",
            "task_id": "66666666-6666-6666-6666-666666666666",
            "jti": "jti-unique-001",
            "agent_type": "general",
            "requester_id": "user-005",
        },
    ),
    (
        "info",
        {
            "event": "token.sender_constrained_issued",
            "task_id": "66666666-6666-6666-6666-666666666667",
            "jti": "jti-unique-002",
            "agent_type": "general",
            "requester_id": "user-005",
            "stage": "jit-token-issue",
        },
    ),
    (
        "info",
        {
            "event": "dpop.proof.validated",
            "task_id": "66666666-6666-6666-6666-666666666668",
            "jti": "jti-unique-003",
            "agent_type": "general",
            "stage": "llm-invoke",
            "model": "gpt-4o-mini",
        },
    ),
    (
        "warning",
        {
            "event": "dpop.proof.replayed",
            "task_id": "66666666-6666-6666-6666-666666666669",
            "agent_type": "general",
            "stage": "llm-invoke",
            "error_message": "DPoP proof jti='proof-123' has already been used",
        },
    ),
    (
        "warning",
        {
            "event": "audit.clock_skew_warning",
            "task_id": "66666666-6666-6666-6666-666666666670",
            "agent_type": "general",
            "stage": "llm-invoke",
            "sequence_number": 7,
            "previous_timestamp": "2026-03-09T12:00:02.000000Z",
            "attempted_timestamp": "2026-03-09T12:00:01.500000Z",
            "adjusted_timestamp": "2026-03-09T12:00:02.000000Z",
            "skew_ms": 500,
        },
    ),
    # --- budget ---
    (
        "info",
        {
            "event": "budget.pre_check",
            "task_id": "77777777-7777-7777-7777-777777777777",
            "budget_session_id": "88888888-8888-8888-8888-888888888888",
        },
    ),
    # --- audit() security events ---
    (
        "audit",
        {
            "event": "pii.scrubbed",
            "agent_id": "agent-finance-001",
            "action": "pii.scrub",
            "resource": "prompt",
            "outcome": "success",
        },
    ),
    (
        "audit",
        {
            "event": "llm.complete",
            "agent_id": "agent-hr-001",
            "action": "llm.complete",
            "resource": "model:gpt-4o-mini",
            "outcome": "success",
        },
    ),
    (
        "audit",
        {
            "event": "policy.evaluated",
            "agent_id": "agent-it-001",
            "action": "policy.eval",
            "resource": "opa:agent_access",
            "outcome": "allow",
        },
    ),
    # --- warning-level events ---
    (
        "warning",
        {
            "event": "pii.detected_in_prompt",
            "task_id": "99999999-9999-9999-9999-999999999999",
            "agent_type": "finance",
            "requester_id": "user-006",
        },
    ),
    (
        "warning",
        {
            "event": "pii.detected_in_response",
            "task_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "agent_type": "legal",
            "requester_id": "user-007",
        },
    ),
    # --- error-level events ---
    (
        "error",
        {
            "event": "llm.adapter_error",
            "task_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "agent_type": "general",
            "requester_id": "user-008",
            "error": "Upstream timeout after 30s",
        },
    ),
    (
        "error",
        {
            "event": "budget.exceeded",
            "task_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            "budget_session_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
            "error": "Session cap of $0.50 exceeded",
        },
    ),
    # --- metadata-bearing events ---
    (
        "info",
        {
            "event": "task.started",
            "task_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            "agent_type": "finance",
            "requester_id": "user-009",
            "metadata": {"tenant": "acme", "env": "prod"},
        },
    ),
    (
        "info",
        {
            "event": "task.completed",
            "task_id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
            "agent_type": "finance",
            "requester_id": "user-009",
            "stage": "post-sanitize",
            "sequence_number": 5,
        },
    ),
]

assert len(_REAL_EVENTS) == 24, f"Expected exactly 24 real events, got {len(_REAL_EVENTS)}"


class TestConformance:
    """All 20 real AuditLogger events must validate against the schema."""

    @pytest.fixture(scope="class")
    def captured_events(self) -> list[dict[str, Any]]:
        """Fire all fixture events and return the parsed JSON dicts."""
        # Deep-copy the dicts so the pop() calls in _capture_audit_events
        # do not mutate the module-level constant.
        pairs = [(method, copy.deepcopy(kwargs)) for method, kwargs in _REAL_EVENTS]
        events = _capture_audit_events(*pairs)
        assert len(events) == 24, (
            f"Expected 24 captured events from AuditLogger, got {len(events)}. "
            "This indicates a logger reconfiguration bug in the test helper."
        )
        return events

    def test_all_20_events_captured(self, captured_events: list[dict[str, Any]]) -> None:
        """Exactly 24 events must be emitted — no more, no fewer."""
        assert len(captured_events) == 24

    @pytest.mark.parametrize("idx", list(range(24)))
    def test_event_n_validates(
        self,
        idx: int,
        captured_events: list[dict[str, Any]],
        validator: Draft7Validator,
    ) -> None:
        """Each captured event must validate against the schema with zero errors."""
        event = captured_events[idx]
        errors = list(validator.iter_errors(event))
        assert errors == [], (
            f"Event #{idx} failed schema validation:\n"
            + "\n".join(f"  - {e.message} (path: {list(e.absolute_path)})" for e in errors)
            + f"\nEvent dict: {json.dumps(event, indent=2)}"
        )


# ---------------------------------------------------------------------------
# 4 – Negative conformance test: 10 malformed events must ALL be rejected
# ---------------------------------------------------------------------------

_MALFORMED_EVENTS: list[tuple[str, dict[str, Any]]] = [
    # 1. Missing required 'event' field
    ("missing_event", {"level": "info", "timestamp": "2026-03-04T10:00:00Z"}),
    # 2. Missing required 'level' field
    ("missing_level", {"event": "token_issued", "timestamp": "2026-03-04T10:00:00Z"}),
    # 3. Missing required 'timestamp' field
    ("missing_timestamp", {"event": "token_issued", "level": "info"}),
    # 4. Empty object — all required fields missing
    ("empty_object", {}),
    # 5. 'event' is an integer, not a string
    ("event_wrong_type", {"event": 12345, "level": "info", "timestamp": "2026-03-04T10:00:00Z"}),
    # 6. 'level' is not in the allowed enum
    (
        "level_invalid_enum",
        {"event": "token_issued", "level": "critical", "timestamp": "2026-03-04T10:00:00Z"},
    ),
    # 7. 'timestamp' is an integer, not a date-time string
    ("timestamp_wrong_type", {"event": "token_issued", "level": "info", "timestamp": 1709546400}),
    # 8. 'level' is null
    ("level_null", {"event": "token_issued", "level": None, "timestamp": "2026-03-04T10:00:00Z"}),
    # 9. Extra field 'password' not in the schema (additionalProperties: false)
    (
        "extra_forbidden_field_password",
        {
            "event": "token_issued",
            "level": "info",
            "timestamp": "2026-03-04T10:00:00Z",
            "password": "s3cr3t",
        },
    ),
    # 10. Extra field 'api_key' not in the schema (additionalProperties: false)
    (
        "extra_forbidden_field_api_key",
        {
            "event": "aegis.startup",
            "level": "info",
            "timestamp": "2026-03-04T10:00:00Z",
            "api_key": "sk-leaked-key-1234",
        },
    ),
]

assert len(_MALFORMED_EVENTS) == 10, (
    f"Expected exactly 10 malformed events, got {len(_MALFORMED_EVENTS)}"
)


class TestNegativeConformance:
    """All 10 intentionally malformed event objects must be rejected by the schema."""

    @pytest.mark.parametrize("label,bad_event", _MALFORMED_EVENTS)
    def test_malformed_event_is_rejected(
        self,
        label: str,
        bad_event: dict[str, Any],
        validator: Draft7Validator,
    ) -> None:
        """The schema must report at least one validation error for this malformed event."""
        errors = list(validator.iter_errors(bad_event))
        assert errors, (
            f"Malformed event '{label}' was incorrectly accepted by the schema — "
            "the schema is too loose. Event: "
            + json.dumps(bad_event)
        )

    def test_all_10_malformed_events_rejected(self, validator: Draft7Validator) -> None:
        """Bulk assertion: every malformed event in the catalogue must fail validation."""
        accepted: list[str] = []
        for label, bad_event in _MALFORMED_EVENTS:
            errors = list(validator.iter_errors(bad_event))
            if not errors:
                accepted.append(label)
        assert accepted == [], (
            f"The following malformed events were incorrectly accepted: {accepted}"
        )

    def test_validate_raises_for_malformed_event(self, schema: dict[str, Any]) -> None:
        """jsonschema.validate() must raise ValidationError for a missing-required event."""
        with pytest.raises(ValidationError):
            validate(instance={"level": "info", "timestamp": "2026-03-04T10:00:00Z"}, schema=schema)
