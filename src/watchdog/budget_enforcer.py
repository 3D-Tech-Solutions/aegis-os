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

"""Budget Enforcer - tracks token spend and enforces hard cost limits per agent session.

All monetary arithmetic uses ``decimal.Decimal`` to guarantee exact representation.
Floating-point values are accepted at API boundaries for convenience but are
immediately converted to ``Decimal`` to prevent representation error from
accumulating across many small spend recordings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import ClassVar
from uuid import UUID

from src.audit_vault.logger import AuditLogger
from src.config import settings
from src.watchdog.metrics import budget_remaining as _budget_remaining
from src.watchdog.metrics import tokens_consumed as _tokens_consumed

_logger = AuditLogger(component="watchdog.budget")


@dataclass
class BudgetSession:
    """Per-session accounting record carrying Decimal cost tallies."""

    session_id: UUID
    agent_type: str
    budget_limit_usd: Decimal
    tokens_used: int = 0
    cost_usd: Decimal = field(default_factory=lambda: Decimal("0"))
    alerts: list[str] = field(default_factory=list)


class BudgetExceededError(Exception):
    """Raised synchronously when an agent session exceeds its allocated budget.

    The exception is always raised within the same call frame as
    :meth:`BudgetEnforcer.record_spend` — enforcement is never deferred to a
    background thread or callback.
    """


class BudgetEnforcer:
    """Tracks token velocity and enforces hard cost limits for agent sessions.

    All cost arithmetic uses :class:`decimal.Decimal` to guarantee exact
    boundary enforcement (e.g. $0.999999 is within a $1.00 cap; $1.000001 is
    not). Prometheus metrics and the Prometheus Gauge accept ``float`` values;
    conversion is applied only at the boundary.

    A simple cost model is used: ``cost_per_token`` can be overridden per call.
    In production this would integrate with LLM provider pricing APIs.
    """

    # Default cost: ~$0.002 per 1 000 tokens (rough GPT-3.5 equivalent)
    DEFAULT_COST_PER_TOKEN: ClassVar[Decimal] = Decimal("0.000002")

    def __init__(self, audit_logger: AuditLogger | None = None) -> None:
        """Initialise a new BudgetEnforcer.

        Parameters
        ----------
        audit_logger:
            Optional :class:`~src.audit_vault.logger.AuditLogger` instance.
            If omitted the module-level ``_logger`` (component
            ``watchdog.budget``) is used.  Pass a custom logger in tests to
            capture emitted audit events without relying on stdout parsing.
        """
        self._sessions: dict[UUID, BudgetSession] = {}
        self._audit: AuditLogger = audit_logger if audit_logger is not None else _logger

    def create_session(
        self,
        session_id: UUID,
        agent_type: str,
        budget_limit_usd: Decimal | float | None = None,
    ) -> BudgetSession:
        """Register a new budget session for an agent.

        Parameters
        ----------
        session_id:
            Unique identifier for this budget session.
        agent_type:
            The agent classification (e.g. ``"finance"``, ``"hr"``).
        budget_limit_usd:
            Maximum allowed spend in USD.
            Accepts :class:`~decimal.Decimal` or ``float``; floats are
            converted via ``Decimal(str(value))`` to avoid representation error.
            Defaults to ``settings.budget_limit_usd`` when omitted.
        """
        if budget_limit_usd is None:
            limit = Decimal(str(settings.budget_limit_usd))
        elif isinstance(budget_limit_usd, float):
            limit = Decimal(str(budget_limit_usd))
        else:
            limit = budget_limit_usd

        session = BudgetSession(
            session_id=session_id,
            agent_type=agent_type,
            budget_limit_usd=limit,
        )
        self._sessions[session_id] = session
        _budget_remaining.labels(session_id=str(session_id)).set(float(limit))
        self._audit.info(
            "budget.session_created",
            session_id=str(session_id),
            agent_type=agent_type,
            budget_limit_usd=str(limit),
        )
        return session

    def record_spend(
        self,
        session_id: UUID,
        amount_usd: Decimal,
    ) -> BudgetSession:
        """Record a USD spend amount and raise :exc:`BudgetExceededError` synchronously.

        This method is the **single source of truth** for cost accounting.  The
        raise is synchronous — it occurs within the same call frame as this
        method, never deferred to a background thread or future.

        Parameters
        ----------
        session_id:
            The budget session to debit.
        amount_usd:
            The exact USD spend to record, as a :class:`~decimal.Decimal`.

        Raises
        ------
        BudgetExceededError
            Raised immediately when
            ``session.cost_usd + amount_usd > session.budget_limit_usd``.
        KeyError
            If no session with *session_id* exists.
        """
        session = self._get_session(session_id)
        session.cost_usd += amount_usd

        remaining = session.budget_limit_usd - session.cost_usd
        _budget_remaining.labels(session_id=str(session_id)).set(
            float(max(remaining, Decimal("0")))
        )

        if session.cost_usd > session.budget_limit_usd:
            msg = (
                f"Session {session_id} exceeded budget "
                f"(${session.cost_usd} > ${session.budget_limit_usd})"
            )
            session.alerts.append(msg)
            self._audit.warning(
                "budget.exceeded",
                session_id=str(session_id),
                agent_type=session.agent_type,
                spent_usd=str(session.cost_usd),
                limit_usd=str(session.budget_limit_usd),
            )
            raise BudgetExceededError(msg)

        return session

    def check_budget(self, session_id: UUID) -> None:
        """Raise :exc:`BudgetExceededError` if the session budget is already exhausted.

        This is the **pre-LLM-call guard**: the orchestrator calls this before
        forwarding a request to any LLM adapter.  If the session has already
        reached or exceeded its cap the exception is raised immediately and the
        adapter is never invoked.

        Raises
        ------
        BudgetExceededError
            Raised when ``session.cost_usd >= session.budget_limit_usd``.
        KeyError
            If no session with *session_id* exists.
        """
        session = self._get_session(session_id)
        if session.cost_usd >= session.budget_limit_usd:
            msg = (
                f"Session {session_id} budget already exhausted "
                f"(${session.cost_usd} >= ${session.budget_limit_usd})"
            )
            self._audit.warning(
                "budget.exceeded",
                session_id=str(session_id),
                agent_type=session.agent_type,
                spent_usd=str(session.cost_usd),
                limit_usd=str(session.budget_limit_usd),
            )
            raise BudgetExceededError(msg)

    def record_tokens(
        self,
        session_id: UUID,
        tokens: int,
        cost_per_token: Decimal = DEFAULT_COST_PER_TOKEN,
    ) -> BudgetSession:
        """Record token usage and convert to USD spend via ``cost_per_token``.

        Increments ``tokens_used`` on the session and updates the Prometheus
        token counter, then delegates to :meth:`record_spend` for the monetary
        accounting and budget enforcement.

        Parameters
        ----------
        session_id:
            The budget session to debit.
        tokens:
            Number of tokens consumed.
        cost_per_token:
            USD cost per token as a :class:`~decimal.Decimal`.
            Defaults to :attr:`DEFAULT_COST_PER_TOKEN`.

        Raises
        ------
        BudgetExceededError
            Propagated from :meth:`record_spend` when the limit is breached.
        KeyError
            If no session with *session_id* exists.
        """
        session = self._get_session(session_id)
        session.tokens_used += tokens
        _tokens_consumed.labels(agent_type=session.agent_type).inc(tokens)
        amount_usd = Decimal(tokens) * cost_per_token
        return self.record_spend(session_id, amount_usd)

    def get_session(self, session_id: UUID) -> BudgetSession | None:
        """Return the budget session for the given ID, or ``None`` if not found."""
        return self._sessions.get(session_id)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_session(self, session_id: UUID) -> BudgetSession:
        """Return the budget session or raise ``KeyError`` if absent."""
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Budget session {session_id} not found")
        return session
