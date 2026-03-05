# Copyright 2026 Tim Escolopio / 3D Tech Solutions
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Prometheus metric singletons for the Aegis-OS Watchdog subsystem.

All metric objects are module-level singletons registered in the default
``prometheus_client.REGISTRY``.  **Only this module may define these
metrics.**  All other modules import from here to prevent duplicate-
registration errors on import.

Exported metrics
----------------
tokens_consumed
    A :class:`~prometheus_client.Counter` labelled ``agent_type``.
    Incremented by :class:`~src.watchdog.budget_enforcer.BudgetEnforcer`
    on every successful :meth:`~src.watchdog.budget_enforcer.BudgetEnforcer.record_tokens` call.

budget_remaining
    A :class:`~prometheus_client.Gauge` labelled ``session_id``.
    Set to ``(limit_usd - spent_usd)`` by
    :class:`~src.watchdog.budget_enforcer.BudgetEnforcer` after every spend
    event (including session creation).

orchestrator_errors
    A :class:`~prometheus_client.Counter` labelled ``stage`` and
    ``agent_type``.  Incremented by the orchestrator's
    ``_stage_error_guard`` context manager whenever an exception propagates
    out of a pipeline stage.  This counter fires for **every** error path —
    including stages that execute before the LLM adapter is called — so
    callers can detect silent metric drops even when no tokens are consumed.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge

# ---------------------------------------------------------------------------
# Token consumption counter
# ---------------------------------------------------------------------------
tokens_consumed: Counter = Counter(
    "aegis_tokens_consumed_total",
    "Total tokens consumed by agent sessions, labelled by agent type.",
    ["agent_type"],
)

# ---------------------------------------------------------------------------
# Remaining budget gauge
# ---------------------------------------------------------------------------
budget_remaining: Gauge = Gauge(
    "aegis_budget_remaining_usd",
    "Remaining budget in USD for each active budget session.",
    ["session_id"],
)

# ---------------------------------------------------------------------------
# Per-stage orchestrator error counter
# ---------------------------------------------------------------------------
orchestrator_errors: Counter = Counter(
    "aegis_orchestrator_errors_total",
    "Count of errors raised at each orchestrator pipeline stage, "
    "labelled by stage name and agent type.",
    ["stage", "agent_type"],
)
