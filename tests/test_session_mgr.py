"""Tests for the Governance Session Manager."""


import pytest
from jose import JWTError, jwt

from src.config import settings
from src.governance.session_mgr import SessionManager, TokenClaims


@pytest.fixture()
def mgr() -> SessionManager:
    return SessionManager()


def test_issue_token_returns_string(mgr: SessionManager) -> None:
    token = mgr.issue_token(agent_type="finance", requester_id="user-123")
    assert isinstance(token, str)
    assert len(token) > 0


def test_issued_token_contains_expected_claims(mgr: SessionManager) -> None:
    token = mgr.issue_token(agent_type="hr", requester_id="user-456")
    payload = jwt.decode(token, settings.token_secret_key, algorithms=[settings.token_algorithm])
    assert payload["agent_type"] == "hr"
    assert payload["sub"] == "user-456"
    assert "jti" in payload
    assert "exp" in payload
    assert "iat" in payload


def test_validate_token_returns_claims(mgr: SessionManager) -> None:
    token = mgr.issue_token(agent_type="it", requester_id="user-789", metadata={"foo": "bar"})
    claims = mgr.validate_token(token)
    assert isinstance(claims, TokenClaims)
    assert claims.agent_type == "it"
    assert claims.sub == "user-789"
    assert claims.metadata == {"foo": "bar"}


def test_token_not_expired_immediately(mgr: SessionManager) -> None:
    token = mgr.issue_token(agent_type="general", requester_id="user-111")
    claims = mgr.validate_token(token)
    assert not mgr.is_expired(claims)


def test_time_remaining_positive(mgr: SessionManager) -> None:
    token = mgr.issue_token(agent_type="general", requester_id="user-222")
    claims = mgr.validate_token(token)
    assert mgr.time_remaining(claims) > 0


def test_issue_token_raises_on_empty_agent_type(mgr: SessionManager) -> None:
    with pytest.raises(ValueError):
        mgr.issue_token(agent_type="", requester_id="user-333")


def test_issue_token_raises_on_empty_requester(mgr: SessionManager) -> None:
    with pytest.raises(ValueError):
        mgr.issue_token(agent_type="finance", requester_id="")


def test_invalid_token_raises_error(mgr: SessionManager) -> None:
    with pytest.raises(JWTError):
        mgr.validate_token("this.is.not.a.valid.token")
