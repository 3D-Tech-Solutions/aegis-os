# ADR-001: Governance Architecture - Policy-as-Code with OPA

**Status**: Accepted  
**Date**: 2026-03-03  
**Deciders**: Aegis-OS Core Team

## Context

AI agents executing autonomous tasks need clearly defined, auditable permissions. Static API keys and hard-coded rules do not scale and cannot be changed without a code deployment.

## Decision

Use Open Policy Agent (OPA) with Rego policies as the Policy-as-Code engine for all agent access decisions. Policies live in `policies/` and are loaded into the OPA server at startup.

## Consequences

- **Positive**: Policies can be updated independently of application code.  
- **Positive**: Every access decision is logged and auditable.  
- **Negative**: Adds an OPA server dependency; mitigated by the Docker Compose setup.

---

# ADR-002: Just-In-Time Agent Tokens

**Status**: Accepted  
**Date**: 2026-03-03

## Context

Long-lived API keys have a large blast radius if compromised.

## Decision

Issue short-lived (15-minute) JWT tokens scoped to a specific agent type and requester via `src/governance/session_mgr.py`. Tokens are signed with a secret stored in HashiCorp Vault.

## Consequences

- **Positive**: Compromised tokens expire quickly.  
- **Positive**: Token scope limits lateral movement.  
- **Negative**: Agents must renew tokens; handled transparently by the Control Plane.

---

# ADR-003: Circuit Breaker for Agent Loop Detection

**Status**: Accepted  
**Date**: 2026-03-03

## Context

LLM agents can enter infinite loops, burning tokens and incurring unexpected costs.

## Decision

Implement a `LoopDetector` watchdog (`src/watchdog/loop_detector.py`) that raises a `LoopDetectedError` when an agent exceeds `MAX_AGENT_STEPS` without a `PROGRESS` signal or exceeds `MAX_TOKEN_VELOCITY`. This triggers a `Human_Intervention_Required` event in Temporal.

## Consequences

- **Positive**: Hard cap on runaway agent costs.  
- **Positive**: Humans remain in the loop for stuck workflows.  
- **Negative**: Legitimate long-running tasks must emit progress signals; documented in the agent SDK guide.
