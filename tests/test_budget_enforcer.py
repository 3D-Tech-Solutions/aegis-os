"""Tests for the Watchdog Budget Enforcer."""

from uuid import uuid4

import pytest

from src.watchdog.budget_enforcer import BudgetEnforcer, BudgetExceededError, BudgetSession


@pytest.fixture()
def enforcer() -> BudgetEnforcer:
    return BudgetEnforcer()


def test_create_session_returns_budget_session(enforcer: BudgetEnforcer) -> None:
    sid = uuid4()
    session = enforcer.create_session(sid, agent_type="finance", budget_limit_usd=5.0)
    assert isinstance(session, BudgetSession)
    assert session.budget_limit_usd == 5.0
    assert session.tokens_used == 0
    assert session.cost_usd == 0.0


def test_record_tokens_increments_usage(enforcer: BudgetEnforcer) -> None:
    sid = uuid4()
    enforcer.create_session(sid, agent_type="hr", budget_limit_usd=1.0)
    session = enforcer.record_tokens(sid, tokens=100)
    assert session.tokens_used == 100
    assert session.cost_usd > 0.0


def test_budget_exceeded_raises_error(enforcer: BudgetEnforcer) -> None:
    sid = uuid4()
    # Very small budget - 1 token will exceed it with any nonzero cost_per_token
    enforcer.create_session(sid, agent_type="it", budget_limit_usd=0.000_001)
    with pytest.raises(BudgetExceededError):
        enforcer.record_tokens(sid, tokens=1000)


def test_budget_not_exceeded_within_limit(enforcer: BudgetEnforcer) -> None:
    sid = uuid4()
    enforcer.create_session(sid, agent_type="general", budget_limit_usd=100.0)
    session = enforcer.record_tokens(sid, tokens=100)
    assert not session.alerts


def test_get_session_returns_none_for_unknown(enforcer: BudgetEnforcer) -> None:
    assert enforcer.get_session(uuid4()) is None


def test_record_tokens_unknown_session_raises(enforcer: BudgetEnforcer) -> None:
    with pytest.raises(KeyError):
        enforcer.record_tokens(uuid4(), tokens=10)


def test_multiple_token_recordings_accumulate(enforcer: BudgetEnforcer) -> None:
    sid = uuid4()
    enforcer.create_session(sid, agent_type="finance", budget_limit_usd=50.0)
    enforcer.record_tokens(sid, tokens=100)
    session = enforcer.record_tokens(sid, tokens=200)
    assert session.tokens_used == 300
