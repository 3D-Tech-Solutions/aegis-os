"""Executable command checks for Phase 2 HITL runbooks."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
HITL_RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "hitl-stuck-approval.md"
BUDGET_RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "budget-exceeded.md"

_BASH_BLOCK_RE = re.compile(r"```bash\n(.*?)\n```", re.DOTALL)
_PYTHON_BLOCK_RE = re.compile(r"```python\n(.*?)\n```", re.DOTALL)

_TASK_ID = "11111111-2222-3333-4444-555555555555"
_SESSION_ID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
_ADMIN_TOKEN = "demo-admin-token"


def _extract_blocks(path: Path, language_regex: re.Pattern[str]) -> list[str]:
    return [block.strip() for block in language_regex.findall(path.read_text(encoding="utf-8"))]


def _write_stub(directory: Path, name: str, content: str) -> None:
    path = directory / name
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _build_stub_toolchain(directory: Path) -> None:
    _write_stub(
        directory,
        "curl",
        """#!/usr/bin/env bash
set -e
args="$*"
if [[ "$args" == *"/health"* ]]; then
  printf '{"status":"ok","service":"aegis-os"}\n'
  exit 0
fi
if [[ "$args" == *"/api/v1/rules"* ]]; then
    printf '%s\n' \
        '{"status":"success","data":{"groups":[{"name":"aegis_hitl","rules":[{"name":"aegis_hitl_stuck"}]}]}}'
  exit 0
fi
if [[ "$args" == *"aegis_budget_remaining_usd"* ]]; then
  printf '{"status":"success","data":{"result":[]}}\n'
  exit 0
fi
if [[ "$args" == *"/api/v1/tasks/"*"/approve"* ]]; then
    printf '%s\n' \
        '{"task_id":"11111111-2222-3333-4444-555555555555","status":"approved","actor_id":"admin-user"}'
  exit 0
fi
if [[ "$args" == *"/api/v1/tasks/"*"/deny"* ]]; then
    printf '%s\n' \
        '{"task_id":"11111111-2222-3333-4444-555555555555","status":"denied","actor_id":"admin-user"}'
  exit 0
fi
printf '{}\n'
""",
    )
    _write_stub(
        directory,
        "jq",
        """#!/usr/bin/env bash
cat >/dev/null
printf '{}\n'
""",
    )
    _write_stub(
        directory,
        "docker",
        f"""#!/usr/bin/env bash
set -e
args=\"$*\"
if [[ \"$args\" == *\"logs aegis-api\"* ]]; then
    printf '%s%s\\n' \
        '{{\"event\":\"budget.exceeded\",\"session_id\":\"{_SESSION_ID}\",\"message\":\"' \
        'Session {_SESSION_ID} exceeded budget ($10.0042 > $10.0000)\",\"agent_type\":\"general\"}}'
    printf '%s\\n' \
        '{{\"session_id\":\"{_SESSION_ID}\",\"agent_type\":\"general\",\"event\":\"budget.review_required\"}}'
  exit 0
fi
exit 0
""",
    )


def _normalized_shell_block(block: str) -> str:
    return (
        block.replace("${TASK_ID}", _TASK_ID)
        .replace("$TASK_ID", _TASK_ID)
        .replace("<session_id>", _SESSION_ID)
        .replace("<task_id>", _TASK_ID)
    )


def _run_shell_block(block: str, toolchain_dir: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = f"{toolchain_dir}:{env['PATH']}"
    env["ADMIN_JIT_TOKEN"] = _ADMIN_TOKEN
    env["TASK_ID"] = _TASK_ID
    return subprocess.run(
        ["bash", "-lc", _normalized_shell_block(block)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_hitl_runbook_bash_blocks_execute() -> None:
    blocks = _extract_blocks(HITL_RUNBOOK, _BASH_BLOCK_RE)
    assert len(blocks) >= 4

    toolchain_dir = Path(subprocess.check_output(["mktemp", "-d"], text=True).strip())
    try:
        _build_stub_toolchain(toolchain_dir)
        for block in blocks:
            result = _run_shell_block(block, toolchain_dir)
            assert result.returncode == 0, (
                "HITL runbook command block failed:\n"
                f"STDOUT:\n{result.stdout}\n"
                f"STDERR:\n{result.stderr}"
            )
    finally:
        subprocess.run(["rm", "-rf", str(toolchain_dir)], check=False)


def test_budget_runbook_bash_blocks_execute() -> None:
    blocks = _extract_blocks(BUDGET_RUNBOOK, _BASH_BLOCK_RE)
    assert len(blocks) >= 5

    toolchain_dir = Path(subprocess.check_output(["mktemp", "-d"], text=True).strip())
    try:
        _build_stub_toolchain(toolchain_dir)
        for block in blocks:
            result = _run_shell_block(block, toolchain_dir)
            assert result.returncode == 0, (
                "Budget runbook command block failed:\n"
                f"STDOUT:\n{result.stdout}\n"
                f"STDERR:\n{result.stderr}"
            )
    finally:
        subprocess.run(["rm", "-rf", str(toolchain_dir)], check=False)


def test_budget_runbook_python_snippet_compiles() -> None:
    blocks = _extract_blocks(BUDGET_RUNBOOK, _PYTHON_BLOCK_RE)
    assert blocks, "Expected at least one python snippet in the budget runbook"
    for block in blocks:
        normalized = _normalized_shell_block(block)
        compile(normalized, str(BUDGET_RUNBOOK), "exec")
