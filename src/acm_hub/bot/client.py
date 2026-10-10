"""The Discord client and its wiring.

Intents are deliberately minimal: ``guilds`` only. ACM Hub reads no message
content, tracks no presences and needs no member list - Sapphire owns
moderation, verification and roles, and duplicating those permissions here would
be an unnecessary risk. Officer role information arrives on the interaction
itself, so even the review buttons work without privileged intents.
"""

from __future__ import annotations

import discord
from discord.ext import commands

from acm_hub.collectors.acm.conferences import (
    DEFAULT_CONFERENCES_URL,
    AcmOfficialConferencesCollector,
)
from acm_hub.collectors.asu.ready_to_mentor import AsuReadyToMentorCollector
from acm_hub.collectors.base import Collector
from acm_hub.collectors.github.feeds_config import (
    parse_discover_orgs,
    parse_internship_github_feeds,
)
from acm_hub.collectors.github.simplify import SimplifyCollector
from acm_hub.config.settings import Settings
from acm_hub.db.session import Database
from acm_hub.observability.logging import get_logger
from acm_hub.pipeline.process import ClosureGuard, OpportunityPipeline
from acm_hub.publishing.approval import ApproveButton, RejectButton
from acm_hub.publishing.discord import OpportunityPublisher
from acm_hub.scheduler.jobs import CollectionScheduler

logger = get_logger(__name__)


def build_intents() -> discord.Intents:
    """The smallest intent set ACM Hub can operate with."""
    intents = discord.Intents.none()
    intents.guilds = True
    return intents


class AcmHubBot(commands.Bot):
    """ACM Hub's Discord client.

    Owns the long-lived resources - database engine, HTTP-backed collectors and
    the scheduler - so that :meth:`close` can shut all of them down in order.
    """

    def __init__(self, settings: Settings, database: Database) -> None:
        super().__init__(command_prefix=commands.when_mentioned, intents=build_intents())
        self.settings = settings
        self.database = database

        timeout = settings.http_timeout_seconds
        retries = settings.http_max_retries
        self.collectors: list[Collector] = [
            SimplifyCollector(
                feeds=parse_internship_github_feeds(settings.internship_github_feeds),
                discover_orgs=parse_discover_orgs(settings.internship_github_discover_orgs),
                github_token=(
                    settings.github_token.get_secret_value() if settings.github_token else None
                ),
                timeout_seconds=timeout,
                max_retries=retries,
            ),
            AsuReadyToMentorCollector(timeout_seconds=timeout, max_retries=retries),
        ]
        if settings.acm_conferences_enabled:
            self.collectors.append(
                AcmOfficialConferencesCollector(
                    conferences_url=settings.acm_conferences_url or DEFAULT_CONFERENCES_URL,
                    timeout_seconds=timeout,
                    max_retries=retries,
                )
            )

        self.pipeline = OpportunityPipeline(
            database,
            sig_threshold=settings.sig_score_threshold,
            sig_max_matches=settings.sig_max_matches,
            closure_detection_enabled=settings.closure_detection_enabled,
            closure_guard=ClosureGuard(
                min_volume_ratio=settings.closure_min_volume_ratio,
                max_close_ratio=settings.closure_max_close_ratio,
            ),
        )

        self.publisher = OpportunityPublisher(
            self,
            database,
            opportunities_channel_id=settings.opportunities_channel_id,
            officer_review_channel_id=settings.officer_review_channel_id,
            research_channel_id=settings.research_channel_id,
            source_display_names={
                collector.name: collector.display_name for collector in self.collectors
            },
        )

        self.scheduler = CollectionScheduler(
            pipeline=self.pipeline,
            publisher=self.publisher,
            collectors=self.collectors,
            review_batch_limit=settings.review_queue_batch_limit,
            publish_batch_limit=settings.publish_batch_limit,
        )

    @property
    def guild_object(self) -> discord.Object:
        """The configured guild, for guild-scoped command syncing."""
        return discord.Object(id=self.settings.discord_guild_id)

    async def setup_hook(self) -> None:
        """Register commands, restore persistent views and start the scheduler.

        Commands are synced to the configured guild rather than globally: guild
        syncs take effect immediately, which matters when officers are iterating
        on the bot in a live server.
        """
        # Registered before any interaction arrives so review cards posted by a
        # previous process keep working after a restart.
        self.add_dynamic_items(ApproveButton, RejectButton)

        from acm_hub.bot.commands.acm import AcmCommands
        from acm_hub.bot.commands.admin import AdminCommands

        await self.add_cog(AcmCommands(self))
        await self.add_cog(AdminCommands(self))

        self.tree.copy_global_to(guild=self.guild_object)
        synced = await self.tree.sync(guild=self.guild_object)
        logger.info(
            "bot.commands_synced",
            guild_id=self.settings.discord_guild_id,
            command_count=len(synced),
        )

        if self.settings.scheduler_enabled:
            self.scheduler.start(
                internship_interval_minutes=self.settings.internship_collection_interval_minutes
            )

    async def on_ready(self) -> None:
        """Log a successful gateway connection."""
        user = self.user
        logger.info(
            "bot.ready",
            bot_user=str(user) if user else "unknown",
            guild_count=len(self.guilds),
        )

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        """Surface command failures to the user instead of failing silently."""
        logger.error(
            "command.failed",
            command=interaction.command.qualified_name if interaction.command else None,
            error=f"{type(error).__name__}: {error}",
            exc_info=True,
        )
        message = "Something went wrong running that command. An officer has been notified."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def close(self) -> None:
        """Shut down in dependency order: scheduler, gateway, HTTP, database."""
        logger.info("bot.shutdown_started")
        await self.scheduler.shutdown()
        await super().close()
        for collector in self.collectors:
            await collector.aclose()
        await self.database.dispose()
        logger.info("bot.shutdown_complete")
