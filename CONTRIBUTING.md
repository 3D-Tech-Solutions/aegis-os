# Contributing to Aegis-OS

Thank you for your interest in contributing to Aegis-OS. This document covers
the essentials: licensing, development setup, and the quality bar your
contributions must meet before merging.

---

## License & Patent Grant

Aegis-OS is licensed under the **Apache License, Version 2.0**. By submitting a
pull request or any other contribution to this repository you agree that:

1. Your contribution is licensed to the project under the Apache License 2.0.
2. You grant the **explicit patent licence** described in Section 3 of the Apache
   License — meaning you grant each recipient of the Work a perpetual,
   worldwide, royalty-free patent licence covering your contribution.
3. You have the legal right to make the contribution (i.e. it is your own work
   or you have permission from your employer/client).

You do **not** need to sign a Contributor Licence Agreement (CLA). The Apache
2.0 patent grant is the mechanism.

---

## Development Setup

```bash
# Clone and enter the repo
git clone https://github.com/3D-Tech-Solutions/aegis-os.git
cd aegis-os

# Create and activate the conda environment (sole sanctioned environment)
conda env create -f environment.yml
conda activate aegis

# Install the project in editable mode with dev dependencies
pip install -e ".[dev]"

# Install pre-commit hooks
pre-commit install

# Verify the full suite is green before making changes
pytest
ruff check src/ tests/
mypy src/
```

---

## Quality Requirements

Every pull request must pass all of the following before it is eligible for
review. CI enforces these automatically, but running them locally first saves
round-trips.

| Check | Command | Requirement |
|---|---|---|
| Tests | `pytest` | Zero failures, zero skips on production paths |
| Lint | `ruff check src/ tests/` | Zero errors |
| Types | `mypy src/` | Zero errors (strict mode) |
| Coverage — orchestrator | `pytest --cov=src/control_plane/orchestrator` | ≥ 90% line |
| Coverage — guardrails | `pytest --cov=src/governance/guardrails` | ≥ 95% line, 100% branch |

Performance thresholds are hard failures, not warnings:

- PII scrub: < 50 ms for a 10,000-character prompt with 50 PII instances
- 500-task stress test: < 60 seconds on CI

---

## Code Style

- **Type-annotate everything.** All function signatures must have complete
  parameter and return type annotations compatible with `mypy --strict`.
- **Structured logging only.** Use `structlog`. No `print()` or bare
  `logging.info()` in production paths under `src/`.
- **Decimal arithmetic for money.** Use `decimal.Decimal`. Never `float` for
  token cost or budget values.
- **Apache 2.0 header on every new source file.** Copy the header from any
  existing file in `src/`.
- **No stubs in production paths.** If a feature is not yet implemented, raise
  `NotImplementedError` with a descriptive message. Do not use `pass` or `...`
  silently.

---

## Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(P2-1): scaffold AgentTaskWorkflow Temporal activities
fix(S1-2): handle Unicode homoglyph variants in PII scrubber
test(W1-3): add metric drop assertion on OPA timeout path
docs(F1-3): add token renewal section to agent-sdk-guide
chore: bump httpx to 0.28.0
```

Reference the roadmap item ID (e.g. `P2-1`) when implementing a documented
feature so commits trace back to gate requirements.

---

## What We Are Looking For

Contributions that directly advance a roadmap gate item are the highest
priority. Check `docs/roadmap.md` for the current phase and open gate items.

Good first contributions:

- Additional PII adversarial test cases in `tests/pii_regression.json`
- Rego policy extensions in `policies/`
- Runbook improvements in `docs/runbooks/`
- Security bug reports via [SECURITY.md](SECURITY.md)

---

## Open-Core Model

The core runtime (Aegis Governance Loop, adapters, watchdog, audit vault) is
fully open source under Apache 2.0. Enterprise features — hosted control plane,
advanced multi-agent mesh UI, commercial SLA support — are developed separately
under commercial licensing by [3D Tech Solutions](https://github.com/3D-Tech-Solutions).

If you build something on top of Aegis-OS under a commercial arrangement, you
are not required to open-source your additions. The Apache 2.0 licence is
deliberately permissive on this point.
