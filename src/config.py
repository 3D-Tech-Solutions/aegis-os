"""Aegis-OS configuration settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(env_prefix="AEGIS_", case_sensitive=False)

    aegis_env: str = "development"
    vault_addr: str = "http://localhost:8200"
    vault_token: str = "aegis-dev-root-token"
    temporal_host: str = "localhost:7233"
    opa_url: str = "http://localhost:8181"

    # Token settings
    token_expiry_seconds: int = 900  # 15 minutes
    token_secret_key: str = "change-me-in-production-use-a-strong-random-key"
    token_algorithm: str = "HS256"

    # Watchdog settings
    max_agent_steps: int = 10
    max_token_velocity: int = 10_000  # tokens per step
    budget_limit_usd: float = 10.0


settings = Settings()
