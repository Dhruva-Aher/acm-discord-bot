"""Configuration validation.

The point of these tests is that a misconfigured deployment fails at startup
with a clear message, rather than connecting to Discord and then misbehaving.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from acm_hub.config.settings import Settings

VALID_ENV = {
    "DISCORD_TOKEN": "not-a-real-token",
    "DISCORD_GUILD_ID": "123456789012345678",
    "OPPORTUNITIES_CHANNEL_ID": "223456789012345678",
    "OFFICER_REVIEW_CHANNEL_ID": "323456789012345678",
    "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/acm_hub",
}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear inherited configuration so tests do not read a developer's .env."""
    for key in (
        *VALID_ENV,
        "RESEARCH_CHANNEL_ID",
        "OFFICER_ROLE_ID",
        "GITHUB_TOKEN",
        "LOG_LEVEL",
        "LOG_FORMAT",
        "SCHEDULER_ENABLED",
        "PUBLISH_BATCH_LIMIT",
        "CLOSURE_DETECTION_ENABLED",
        "CLOSURE_MIN_VOLUME_RATIO",
        "CLOSURE_MAX_CLOSE_RATIO",
    ):
        monkeypatch.delenv(key, raising=False)


def build(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # _env_file=None stops pydantic-settings from reading a local .env.
    return Settings(_env_file=None)


def test_valid_configuration_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build(VALID_ENV, monkeypatch)
    assert settings.discord_guild_id == 123456789012345678
    assert settings.research_channel_id is None


def test_token_is_not_exposed_by_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stray log line or traceback must never leak the bot token."""
    settings = build(VALID_ENV, monkeypatch)
    assert "not-a-real-token" not in repr(settings)
    assert "not-a-real-token" not in str(settings.discord_token)
    assert settings.discord_token.get_secret_value() == "not-a-real-token"


@pytest.mark.parametrize(
    "missing",
    ["DISCORD_TOKEN", "DISCORD_GUILD_ID", "OPPORTUNITIES_CHANNEL_ID", "OFFICER_REVIEW_CHANNEL_ID"],
)
def test_missing_required_value_fails(missing: str, monkeypatch: pytest.MonkeyPatch) -> None:
    env = {key: value for key, value in VALID_ENV.items() if key != missing}
    with pytest.raises(ValidationError):
        build(env, monkeypatch)


def test_synchronous_database_driver_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """psycopg2 here would block the Discord event loop on every query."""
    env = {**VALID_ENV, "DATABASE_URL": "postgresql://user:pass@localhost/acm_hub"}
    with pytest.raises(ValidationError, match="async driver"):
        build(env, monkeypatch)


def test_sqlite_async_driver_is_allowed_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build({**VALID_ENV, "DATABASE_URL": "sqlite+aiosqlite:///./test.db"}, monkeypatch)
    assert settings.database_url.startswith("sqlite+aiosqlite")


def test_non_numeric_channel_id_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        build({**VALID_ENV, "OPPORTUNITIES_CHANNEL_ID": "#opportunities"}, monkeypatch)


def test_invalid_log_level_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="LOG_LEVEL"):
        build({**VALID_ENV, "LOG_LEVEL": "chatty"}, monkeypatch)


def test_log_level_is_normalized_to_upper_case(monkeypatch: pytest.MonkeyPatch) -> None:
    assert build({**VALID_ENV, "LOG_LEVEL": "debug"}, monkeypatch).log_level == "DEBUG"


def test_collection_interval_must_be_sane(monkeypatch: pytest.MonkeyPatch) -> None:
    """A one-minute interval would hammer the upstream source."""
    with pytest.raises(ValidationError):
        build({**VALID_ENV, "INTERNSHIP_COLLECTION_INTERVAL_MINUTES": "1"}, monkeypatch)


def test_optional_officer_role_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build({**VALID_ENV, "OFFICER_ROLE_ID": "999888777666555444"}, monkeypatch)
    assert settings.officer_role_id == 999888777666555444


def test_optional_research_channel_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build({**VALID_ENV, "RESEARCH_CHANNEL_ID": "423456789012345678"}, monkeypatch)
    assert settings.research_channel_id == 423456789012345678
