"""Process startup, wiring and shutdown.

No gateway connection is made, but the bot object is really constructed, which
is what catches wiring mistakes (a collector that cannot be built, a publisher
given the wrong channel, a cog that fails to import) without a Discord token.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from acm_hub.config.settings import Settings
from acm_hub.db.session import Database

TEST_SETTINGS_ENV = {
    "discord_token": SecretStr("not-a-real-token"),
    "discord_guild_id": 123456789012345678,
    "opportunities_channel_id": 223456789012345678,
    "officer_review_channel_id": 323456789012345678,
    "database_url": "sqlite+aiosqlite:///./startup-test.db",
    "scheduler_enabled": False,
}


@pytest.fixture
def settings() -> Settings:
    return Settings(**TEST_SETTINGS_ENV)  # type: ignore[arg-type]


class TestBotConstruction:
    async def test_the_bot_wires_its_components_together(
        self, settings: Settings, database: Database
    ) -> None:
        from acm_hub.bot.client import AcmHubBot

        bot = AcmHubBot(settings, database)
        try:
            assert [collector.name for collector in bot.collectors] == ["simplify_internships"]
            assert bot.publisher is not None
            assert bot.scheduler is not None
            assert bot.guild_object.id == settings.discord_guild_id
        finally:
            await bot.close()

    async def test_intents_are_minimal(self, settings: Settings, database: Database) -> None:
        """ACM Hub must not request privileged intents it has no use for."""
        from acm_hub.bot.client import AcmHubBot, build_intents

        intents = build_intents()
        assert intents.guilds is True
        assert intents.message_content is False
        assert intents.members is False
        assert intents.presences is False

        bot = AcmHubBot(settings, database)
        try:
            assert bot.intents.value == intents.value
        finally:
            await bot.close()

    async def test_closing_releases_every_resource(
        self, settings: Settings, database: Database
    ) -> None:
        from acm_hub.bot.client import AcmHubBot

        bot = AcmHubBot(settings, database)
        bot.scheduler.start(internship_interval_minutes=180)
        assert bot.scheduler.running

        await bot.close()

        assert not bot.scheduler.running
        assert bot.is_closed()

    async def test_closing_twice_is_safe(self, settings: Settings, database: Database) -> None:
        """SIGTERM during an already-running shutdown must not raise."""
        from acm_hub.bot.client import AcmHubBot

        bot = AcmHubBot(settings, database)
        await bot.close()
        await bot.close()


class TestSettingsLoading:
    def test_invalid_configuration_exits_with_a_clear_code(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from acm_hub.config.settings import get_settings
        from acm_hub.main import load_settings

        for key in ("DISCORD_TOKEN", "DISCORD_GUILD_ID", "DATABASE_URL"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.chdir(tmp_path)
        get_settings.cache_clear()

        with pytest.raises(SystemExit) as exit_info:
            load_settings()
        assert exit_info.value.code == 2
        get_settings.cache_clear()


class TestHealthcheck:
    async def test_reports_healthy_when_the_database_answers(
        self, monkeypatch: pytest.MonkeyPatch, database: Database, tmp_path: Path
    ) -> None:
        from acm_hub.config.settings import get_settings
        from acm_hub.healthcheck import check

        monkeypatch.setenv("DISCORD_TOKEN", "not-a-real-token")
        monkeypatch.setenv("DISCORD_GUILD_ID", "1")
        monkeypatch.setenv("OPPORTUNITIES_CHANNEL_ID", "2")
        monkeypatch.setenv("OFFICER_REVIEW_CHANNEL_ID", "3")
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/health.db")
        get_settings.cache_clear()
        try:
            assert await check() is True
        finally:
            get_settings.cache_clear()

    async def test_reports_unhealthy_without_configuration(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from acm_hub.config.settings import get_settings
        from acm_hub.healthcheck import check

        for key in ("DISCORD_TOKEN", "DISCORD_GUILD_ID", "DATABASE_URL"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.chdir(tmp_path)
        get_settings.cache_clear()
        try:
            assert await check() is False
        finally:
            get_settings.cache_clear()


class TestLoggingSetup:
    @pytest.mark.parametrize("log_format", ["console", "text"])
    def test_a_structured_event_is_emitted(
        self, log_format: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Regression guard: a mismatched logger factory broke every log call."""
        from acm_hub.observability.logging import configure_logging, get_logger

        configure_logging(level="INFO", log_format=log_format)
        get_logger("acm_hub.test").info("startup.test_event", collector="stub", records=3)

        captured = capsys.readouterr().err
        assert "startup.test_event" in captured
        assert "stub" in captured
        assert "acm_hub.test" in captured

    def test_an_exception_is_rendered_with_its_traceback(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from acm_hub.observability.logging import configure_logging, get_logger

        configure_logging(level="INFO", log_format="text")
        try:
            raise ValueError("upstream exploded")
        except ValueError:
            get_logger("acm_hub.test").error("collector.failed", exc_info=True)

        captured = capsys.readouterr().err
        assert "collector.failed" in captured
        assert "upstream exploded" in captured

    def test_a_secret_is_not_rendered_when_logged_by_accident(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """SecretStr must stay masked even if someone logs the settings object."""
        from acm_hub.observability.logging import configure_logging, get_logger

        configure_logging(level="INFO", log_format="text")
        get_logger("acm_hub.test").info("bot.starting", token=SecretStr("super-secret-token"))

        assert "super-secret-token" not in capsys.readouterr().err

    def test_debug_level_events_are_emitted(self, capsys: pytest.CaptureFixture[str]) -> None:
        from acm_hub.observability.logging import configure_logging, get_logger

        configure_logging(level="DEBUG", log_format="text")
        get_logger("acm_hub.test").debug("simplify.record_skipped", reason="missing url")

        assert "simplify.record_skipped" in capsys.readouterr().err

    def test_events_below_the_configured_level_are_suppressed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from acm_hub.observability.logging import configure_logging, get_logger

        configure_logging(level="WARNING", log_format="text")
        get_logger("acm_hub.test").info("should.not.appear")

        assert "should.not.appear" not in capsys.readouterr().err

    def test_the_gateway_logger_stays_quiet_at_debug(self) -> None:
        """discord.gateway at DEBUG echoes session identifiers."""
        import logging

        from acm_hub.observability.logging import configure_logging

        configure_logging(level="DEBUG", log_format="text")
        assert logging.getLogger("discord.gateway").level >= logging.WARNING
