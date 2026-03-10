"""Tests for F1-3: link validity and schema field accuracy in docs/agent-sdk-guide.md.

Test coverage:
  1. Internal file links — all relative file paths in the guide resolve to files
     that exist in the repository.
  2. External URL links — all http(s) URLs return HTTP 200 within 10 seconds
     with one retry.  Marked ``@pytest.mark.integration`` since they require
     outbound network access.
  3. Schema field accuracy — every audit event field name referenced in the
     ``## Audit Event Schema Reference`` section of the guide must exist as a
     property in ``docs/audit-event-schema.json``.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
GUIDE_PATH = REPO_ROOT / "docs" / "agent-sdk-guide.md"
SCHEMA_PATH = REPO_ROOT / "docs" / "audit-event-schema.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _extract_links(text: str) -> list[tuple[str, str]]:
    """Return [(label, url), ...] for every Markdown link in *text*."""
    return _MARKDOWN_LINK_RE.findall(text)


def _is_external(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def _is_anchor(url: str) -> bool:
    return url.startswith("#")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def guide_content() -> str:
    """Raw text of the agent SDK guide."""
    return GUIDE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def schema() -> dict[str, object]:
    """Parsed audit event JSON Schema."""
    with SCHEMA_PATH.open() as fh:
        data: dict[str, object] = json.load(fh)
    return data


@pytest.fixture(scope="module")
def internal_file_links(guide_content: str) -> list[tuple[str, str]]:
    """All non-anchor, non-external Markdown links in the guide."""
    return [
        (label, url)
        for label, url in _extract_links(guide_content)
        if not _is_anchor(url) and not _is_external(url)
    ]


@pytest.fixture(scope="module")
def external_links(guide_content: str) -> list[tuple[str, str]]:
    """All external http(s) Markdown links in the guide."""
    return [
        (label, url)
        for label, url in _extract_links(guide_content)
        if _is_external(url)
    ]


# ---------------------------------------------------------------------------
# 1 – Internal file link resolution
# ---------------------------------------------------------------------------


class TestInternalLinks:
    """All relative file links in the guide must resolve to existing files."""

    def test_at_least_one_internal_link(self, internal_file_links: list[tuple[str, str]]) -> None:
        """The guide must contain at least one internal file link."""
        assert internal_file_links, (
            "No internal file links found in docs/agent-sdk-guide.md. "
            "The guide must link to docs/audit-event-schema.json."
        )

    def test_all_internal_links_resolve(
        self, internal_file_links: list[tuple[str, str]]
    ) -> None:
        """Bulk: every internal file link must resolve to an existing file."""
        broken: list[str] = []
        for label, url in internal_file_links:
            # Strip any fragment identifier (#section) before resolving.
            file_part = url.split("#")[0]
            if not file_part:
                continue  # pure anchor — handled by TestInternalLinks separately
            target = (GUIDE_PATH.parent / file_part).resolve()
            if not target.exists():
                broken.append(f"[{label}]({url}) → {target}")
        assert broken == [], (
            "The following internal links do not resolve:\n"
            + "\n".join(f"  - {b}" for b in broken)
        )

    def test_schema_link_present(self, guide_content: str) -> None:
        """The guide must link to docs/audit-event-schema.json."""
        assert "audit-event-schema.json" in guide_content, (
            "docs/agent-sdk-guide.md does not link to audit-event-schema.json. "
            "Add a link in the Audit Event Schema Reference section."
        )


# ---------------------------------------------------------------------------
# 2 – External URL check (integration — requires outbound network)
# ---------------------------------------------------------------------------


def _check_url(url: str, timeout: float = 10.0, retries: int = 1) -> tuple[bool, str]:
    """Return (ok, reason) — tries up to ``retries + 1`` times."""
    import httpx  # import here so missing httpx doesn't break unit tests

    for attempt in range(retries + 1):
        try:
            resp = httpx.get(url, timeout=timeout, follow_redirects=True)
            if resp.status_code == 200:
                return True, "OK"
            reason = f"HTTP {resp.status_code}"
        except httpx.HTTPError as exc:
            reason = str(exc)
        if attempt < retries:
            time.sleep(1.0)
    return False, reason


@pytest.mark.integration
class TestExternalLinks:
    """All external URLs in the guide must return HTTP 200."""

    def test_all_external_urls_return_200(
        self, external_links: list[tuple[str, str]]
    ) -> None:
        """Bulk: every external URL must return 200."""
        if not external_links:
            return
        failures: list[str] = []
        for label, url in external_links:
            ok, reason = _check_url(url)
            if not ok:
                failures.append(f"[{label}]({url}): {reason}")
        assert failures == [], (
            "The following external URLs did not return HTTP 200:\n"
            + "\n".join(f"  - {f}" for f in failures)
        )


# ---------------------------------------------------------------------------
# 3 – Schema field accuracy
# ---------------------------------------------------------------------------

# Sentinel marking: the test looks for field names inside the
# "## Audit Event Schema Reference" section of the guide.
_SCHEMA_REF_SECTION_RE = re.compile(
    r"## Audit Event Schema Reference\n(.*?)(?=\n## |\Z)",
    re.DOTALL,
)
# Match backtick field names in the *first column* of Markdown table rows only.
# A first-column cell looks like:  | `field_name` |
_TABLE_FIRST_COL_RE = re.compile(r"^\| `([a-z][a-z0-9_]*)` \|", re.MULTILINE)


def _extract_schema_ref_fields(text: str) -> list[str]:
    """Return field names from the first column of the schema ref table."""
    m = _SCHEMA_REF_SECTION_RE.search(text)
    if not m:
        return []
    section_text = m.group(1)
    return _TABLE_FIRST_COL_RE.findall(section_text)


class TestSchemaFieldAccuracy:
    """Every audit event field name referenced in the guide must exist in the schema."""

    @pytest.fixture(scope="class")
    def referenced_fields(self, guide_content: str) -> list[str]:
        """Field names extracted from the Audit Event Schema Reference section."""
        fields = _extract_schema_ref_fields(guide_content)
        assert fields, (
            "Could not extract any field names from the "
            "'## Audit Event Schema Reference' section of the guide. "
            "Ensure the section exists and uses backtick-enclosed field names."
        )
        return fields

    def test_schema_ref_section_exists(self, guide_content: str) -> None:
        """The guide must contain a '## Audit Event Schema Reference' section."""
        assert "## Audit Event Schema Reference" in guide_content, (
            "Section '## Audit Event Schema Reference' not found in "
            "docs/agent-sdk-guide.md — add it so the schema accuracy test can run."
        )

    def test_referenced_fields_non_empty(self, referenced_fields: list[str]) -> None:
        """At least one field name must be extracted from the schema ref section."""
        assert len(referenced_fields) >= 1

    @pytest.mark.parametrize(
        "field",
        [
            "event", "level", "timestamp", "task_id", "agent_type", "requester_id",
            "jti", "stage", "outcome", "error", "reasons", "sequence_number",
            "budget_session_id", "metadata",
        ],
    )
    def test_known_field_in_schema(self, field: str, schema: dict[str, object]) -> None:
        """Each known audit event field must be a property in the schema."""
        properties = schema.get("properties", {})
        assert isinstance(properties, dict)
        assert field in properties, (
            f"Field '{field}' is referenced in the guide but not found in "
            f"docs/audit-event-schema.json properties. "
            f"Available fields: {sorted(properties.keys())}"
        )

    def test_all_referenced_fields_in_schema(
        self,
        referenced_fields: list[str],
        schema: dict[str, object],
    ) -> None:
        """Every field name extracted from the guide must exist in the schema."""
        properties = schema.get("properties", {})
        assert isinstance(properties, dict)
        schema_keys = set(properties.keys())

        missing: list[str] = [f for f in referenced_fields if f not in schema_keys]
        assert missing == [], (
            "The following field names appear in the guide's schema reference section "
            "but are not properties in docs/audit-event-schema.json:\n"
            + "\n".join(f"  - {f}" for f in missing)
            + f"\nAvailable schema properties: {sorted(schema_keys)}"
        )
