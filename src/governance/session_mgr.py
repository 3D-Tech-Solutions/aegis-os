"""Session Manager - generates short-lived, scoped Just-In-Time agent tokens."""

import time
from datetime import UTC, datetime
from uuid import uuid4

from jose import jwt
from pydantic import BaseModel

from src.config import settings


class TokenClaims(BaseModel):
    """Claims embedded in a JIT agent session token."""

    jti: str  # unique token ID
    sub: str  # requester / agent identity
    agent_type: str
    issued_at: float
    expires_at: float
    metadata: dict[str, str]


class SessionManager:
    """Issues and validates short-lived scoped tokens for AI agents.

    Tokens expire after ``settings.token_expiry_seconds`` (default 15 minutes).
    Each token is scoped to a specific agent type, limiting the blast radius
    if a token is compromised.
    """

    def issue_token(
        self,
        agent_type: str,
        requester_id: str,
        metadata: dict[str, str] | None = None,
    ) -> str:
        """Create a signed JIT token for an agent session."""
        if not agent_type or not requester_id:
            raise ValueError("agent_type and requester_id must not be empty")

        now = time.time()
        claims = {
            "jti": str(uuid4()),
            "sub": requester_id,
            "agent_type": agent_type,
            "iat": now,
            "exp": now + settings.token_expiry_seconds,
            "metadata": metadata or {},
        }
        token: str = jwt.encode(
            claims,
            settings.token_secret_key,
            algorithm=settings.token_algorithm,
        )
        return token

    def validate_token(self, token: str) -> TokenClaims:
        """Decode and validate a JIT token, raising an error if invalid or expired."""
        payload = jwt.decode(
            token,
            settings.token_secret_key,
            algorithms=[settings.token_algorithm],
        )
        return TokenClaims(
            jti=payload["jti"],
            sub=payload["sub"],
            agent_type=payload["agent_type"],
            issued_at=payload["iat"],
            expires_at=payload["exp"],
            metadata=payload.get("metadata", {}),
        )

    def is_expired(self, claims: TokenClaims) -> bool:
        """Return True if the token has passed its expiry time."""
        return time.time() > claims.expires_at

    def time_remaining(self, claims: TokenClaims) -> float:
        """Return seconds until token expiry (negative if already expired)."""
        return claims.expires_at - time.time()

    def issued_at_utc(self, claims: TokenClaims) -> datetime:
        """Return the issuance time as a UTC datetime."""
        return datetime.fromtimestamp(claims.issued_at, tz=UTC)
