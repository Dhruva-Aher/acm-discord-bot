"""Runtime configuration, sourced from the environment only.

Secrets are never read from a committed file and never logged: ``DISCORD_TOKEN``
and ``GITHUB_TOKEN`` are :class:`~pydantic.SecretStr`, so an accidental repr or
structured-log field renders as ``**********``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated process configuration.

    Raises :class:`pydantic.ValidationError` at startup when a required value is
    missing or malformed, so the bot fails loudly instead of connecting to
    Discord and then misbehaving.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    discord_token: SecretStr
    discord_guild_id: int
    opportunities_channel_id: int
    officer_review_channel_id: int
    research_channel_id: int | None = None
    officer_role_id: int | None = None

    database_url: str

    github_token: SecretStr | None = None

    log_level: str = "INFO"
    log_format: Literal["text", "console"] = "console"

    scheduler_enabled: bool = True
    internship_collection_interval_minutes: int = Field(default=180, ge=5)
    review_queue_batch_limit: int = Field(default=10, ge=1, le=50)

    http_timeout_seconds: float = Field(default=30.0, gt=0)
    http_max_retries: int = Field(default=3, ge=0, le=10)

    sig_score_threshold: float = Field(default=0.25, ge=0.0, le=1.0)
    sig_max_matches: int = Field(default=3, ge=1, le=9)

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        """Reject sync drivers, which would block the Discord event loop."""
        allowed_prefixes = ("postgresql+asyncpg://", "sqlite+aiosqlite://")
        if not value.startswith(allowed_prefixes):
            raise ValueError(
                "DATABASE_URL must use an async driver "
                "(postgresql+asyncpg://... or sqlite+aiosqlite://... for tests), "
                f"got {value.split('://', 1)[0]!r}"
            )
        return value

    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"LOG_LEVEL must be a standard logging level, got {value!r}")
        return level


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, constructed once."""
    return Settings()
