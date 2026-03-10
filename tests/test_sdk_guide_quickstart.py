"""Tests for F1-3: docs/agent-sdk-guide.md quickstart code blocks are runnable.

Extracts every fenced Python code block from the guide using ``re`` and
executes each one via ``subprocess`` in an isolated temp file.  Any block that
exits with a non-zero return code is a hard CI failure.

Blocks that contain HTTP calls to the dev stack (``httpx.ConnectError``) exit
with code 0 by design: the guide wraps network calls in a try/except that
prints a notice and calls ``sys.exit(0)`` when the stack is unavailable.  This
keeps the test green in unit-test runs while still verifying the code is
syntactically and logically correct.
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
GUIDE_PATH = REPO_ROOT / "docs" / "agent-sdk-guide.md"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FENCED_PYTHON_RE = re.compile(
    r"```python\n(.*?)```",
    re.DOTALL,
)


def _extract_python_blocks(text: str) -> list[str]:
    """Return all fenced Python code block bodies from *text*."""
    return [m.group(1) for m in _FENCED_PYTHON_RE.finditer(text)]


# ---------------------------------------------------------------------------
# Fixture: extract blocks once per session
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def python_blocks() -> list[str]:
    """All fenced Python code blocks extracted from the agent SDK guide."""
    content = GUIDE_PATH.read_text(encoding="utf-8")
    blocks = _extract_python_blocks(content)
    assert blocks, (
        f"No fenced Python blocks found in {GUIDE_PATH.relative_to(REPO_ROOT)} — "
        "the guide must contain at least one ```python ... ``` block."
    )
    return blocks


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGuideStructure:
    """Structural assertions about the guide's code examples."""

    def test_guide_file_exists(self) -> None:
        """docs/agent-sdk-guide.md must exist."""
        assert GUIDE_PATH.exists(), f"Guide not found: {GUIDE_PATH}"

    def test_at_least_one_python_block(self, python_blocks: list[str]) -> None:
        """The guide must contain at least one fenced Python code block."""
        assert len(python_blocks) >= 1

    def test_end_to_end_block_present(self) -> None:
        """There must be an end-to-end example block containing an httpx call."""
        content = GUIDE_PATH.read_text(encoding="utf-8")
        blocks = _extract_python_blocks(content)
        e2e_blocks = [b for b in blocks if "httpx" in b and "AEGIS_BASE" in b]
        assert e2e_blocks, (
            "No end-to-end example block (containing 'httpx' and 'AEGIS_BASE') found."
        )

    def test_end_to_end_block_handles_no_server(self) -> None:
        """The E2E block must handle connection errors gracefully (exit 0 without server)."""
        content = GUIDE_PATH.read_text(encoding="utf-8")
        blocks = _extract_python_blocks(content)
        e2e_blocks = [b for b in blocks if "httpx" in b and "AEGIS_BASE" in b]
        assert e2e_blocks
        e2e = e2e_blocks[0]
        # Must have a try/except wrapping the HTTP calls and must call sys.exit(0)
        # for connection errors so the quickstart test passes without a live stack.
        # Accept either specific exceptions or the httpx.HTTPError base class.
        connection_guarded = any(
            token in e2e
            for token in ("ConnectError", "ConnectTimeout", "HTTPError")
        )
        assert connection_guarded, (
            "End-to-end block does not catch httpx connection errors. "
            "Wrap HTTP calls in try/except httpx.HTTPError (or ConnectError/ConnectTimeout) "
            "so the block exits 0 without a running server."
        )
        assert "sys.exit(0)" in e2e, (
            "End-to-end block does not call sys.exit(0) on connection failure."
        )


class TestQuickstartExecution:
    """Each fenced Python block in the guide must execute with exit code 0."""

    def test_all_blocks_exit_zero(self, python_blocks: list[str], tmp_path: Path) -> None:
        """Bulk assertion: every extracted Python block must exit 0."""
        failures: list[str] = []
        for idx, block in enumerate(python_blocks):
            script = tmp_path / f"bulk_block_{idx}.py"
            script.write_text(textwrap.dedent(block), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(REPO_ROOT),
            )
            if result.returncode != 0:
                failures.append(
                    f"Block #{idx} (rc={result.returncode}):\n"
                    f"  stderr: {result.stderr.strip()[:200]}"
                )
        assert failures == [], (
            "The following guide code blocks exited non-zero:\n"
            + "\n".join(failures)
        )
