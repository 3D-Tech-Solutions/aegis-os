"""Budget Enforcer - tracks token spend and enforces cost limits per agent session."""

from dataclasses import dataclass, field
from uuid import UUID

from prometheus_client import Counter, Gauge

from src.audit_vault.logger import AuditLogger
from src.config import settings

_logger = AuditLogger(component="watchdog.budget")

# Prometheus metrics
_tokens_consumed = Counter(
    "aegis_tokens_consumed_total",
    "Total tokens consumed by agents",
    ["agent_type"],
)
_budget_remaining = Gauge(
    "aegis_budget_remaining_usd",
    "Remaining budget in USD",
    ["session_id"],
)


@dataclass
class BudgetSession:
    session_id: UUID
    agent_type: str
    budget_limit_usd: float
    tokens_used: int = 0
    cost_usd: float = 0.0
    alerts: list[str] = field(default_factory=list)


class BudgetExceededError(Exception):
    """Raised when an agent session exceeds its allocated budget."""


class BudgetEnforcer:
    """Tracks token velocity and enforces hard cost limits for agent sessions.

    A simple cost model is used: cost_per_token can be overridden per session.
    In production this would integrate with LLM provider pricing APIs.
    """

    # Default cost: ~$0.002 per 1000 tokens (rough GPT-3.5 equivalent)
    DEFAULT_COST_PER_TOKEN = 0.000_002

    def __init__(self) -> None:
        self._sessions: dict[UUID, BudgetSession] = {}

    def create_session(
        self,
        session_id: UUID,
        agent_type: str,
        budget_limit_usd: float | None = None,
    ) -> BudgetSession:
        """Register a new budget session for an agent."""
        limit = budget_limit_usd if budget_limit_usd is not None else settings.budget_limit_usd
        session = BudgetSession(
            session_id=session_id,
            agent_type=agent_type,
            budget_limit_usd=limit,
        )
        self._sessions[session_id] = session
        _budget_remaining.labels(session_id=str(session_id)).set(limit)
        _logger.info(
            "budget.session_created",
            session_id=str(session_id),
            agent_type=agent_type,
            budget_limit_usd=limit,
        )
        return session

    def record_tokens(
        self,
        session_id: UUID,
        tokens: int,
        cost_per_token: float = DEFAULT_COST_PER_TOKEN,
    ) -> BudgetSession:
        """Record token usage and raise BudgetExceededError if limit is hit."""
        session = self._get_session(session_id)
        cost = tokens * cost_per_token
        session.tokens_used += tokens
        session.cost_usd += cost

        _tokens_consumed.labels(agent_type=session.agent_type).inc(tokens)
        remaining = session.budget_limit_usd - session.cost_usd
        _budget_remaining.labels(session_id=str(session_id)).set(max(remaining, 0.0))

        if session.cost_usd > session.budget_limit_usd:
            msg = (
                f"Session {session_id} exceeded budget "
                f"(${session.cost_usd:.4f} > ${session.budget_limit_usd:.4f})"
            )
            session.alerts.append(msg)
            _logger.warning("budget.exceeded", session_id=str(session_id), message=msg)
            raise BudgetExceededError(msg)

        return session

    def get_session(self, session_id: UUID) -> BudgetSession | None:
        """Return the budget session for the given ID, or None if not found."""
        return self._sessions.get(session_id)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_session(self, session_id: UUID) -> BudgetSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Budget session {session_id} not found")
        return session
