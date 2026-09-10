"""Command registration and the commands' database-backed behaviour.

No gateway connection is made. What these tests catch is the class of mistake
that otherwise only shows up in a live server: a cog that fails to load, a
command that is registered under the wrong name, an admin command missing its
permission gate, or a legacy prefix command sneaking back in.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from acm_hub.bot.commands.acm import AcmCommands
from acm_hub.bot.commands.admin import AdminCommands
from acm_hub.db.repositories.opportunities import OpportunityRepository
from acm_hub.db.session import Database
from acm_hub.domain.enums import ReviewDecision
from acm_hub.pipeline.process import OpportunityPipeline
from tests.conftest import make_raw
from tests.integration.test_approval_buttons import OFFICER_ROLE_ID, fake_interaction
from tests.integration.test_pipeline import StubCollector
from tests.integration.test_review_workflow import OFFICER_ONE, seed


@pytest.fixture
def bot(database: Database) -> MagicMock:
    from acm_hub.bot.client import AcmHubBot

    fake = MagicMock(spec=AcmHubBot)
    fake.database = database
    fake.settings = MagicMock(officer_role_id=OFFICER_ROLE_ID, review_queue_batch_limit=10)
    fake.publisher = AsyncMock()
    fake.scheduler = AsyncMock()
    fake.latency = 0.042
    return fake


async def invoke(cog: commands.Cog, command: Any, *args: Any) -> None:
    """Call a slash command's body directly, bypassing Discord's dispatcher.

    ``Command.callback`` is typed as an unbound function, so the cog instance is
    passed explicitly here.
    """
    callback = cast("Callable[..., Awaitable[None]]", command.callback)
    await callback(cog, *args)


def command_names(cog: AcmCommands | AdminCommands) -> set[str]:
    """Every invocable command path the cog registers, subgroups included."""

    def walk(command: Any, prefix: str) -> set[str]:
        path = f"{prefix} {command.name}".strip()
        if isinstance(command, app_commands.Group):
            return {name for child in command.commands for name in walk(child, path)}
        return {path}

    return {name for command in cog.__cog_app_commands__ for name in walk(command, "")}


class TestCommandRegistration:
    def test_member_commands(self, bot: MagicMock) -> None:
        assert command_names(AcmCommands(bot)) == {"acm status"}

    def test_officer_commands(self, bot: MagicMock) -> None:
        assert command_names(AdminCommands(bot)) == {
            "acm-admin queue",
            "acm-admin approve",
            "acm-admin bulk-approve",
            "acm-admin reject",
            "acm-admin rejected",
            "acm-admin reconsider",
            "acm-admin sync",
            "acm-admin health",
            "acm-admin requeue",
            "acm-admin source list",
            "acm-admin source policy",
            "acm-admin source release-baseline",
            "acm-admin source reset-initialization",
        }

    def test_admin_group_is_hidden_from_ordinary_members(self, bot: MagicMock) -> None:
        group = next(
            command
            for command in AdminCommands(bot).__cog_app_commands__
            if isinstance(command, app_commands.Group)
        )
        assert group.default_permissions is not None
        assert group.default_permissions.manage_guild

    def test_there_are_no_legacy_prefix_commands(self, bot: MagicMock) -> None:
        """ACM Hub is slash-command only; `!opportunities` must not exist."""
        assert AcmCommands(bot).get_commands() == []
        assert AdminCommands(bot).get_commands() == []


class TestStatusCommand:
    async def test_reports_an_empty_hub_without_error(
        self, bot: MagicMock, database: Database
    ) -> None:
        interaction = fake_interaction(bot)
        cog = AcmCommands(bot)
        await invoke(cog, cog.status, interaction)

        embed = interaction.followup.send.await_args.kwargs["embed"]
        assert embed.title == "ACM Hub status"
        assert interaction.followup.send.await_args.kwargs["ephemeral"] is True

    async def test_reports_counts_after_a_collection_run(
        self, bot: MagicMock, database: Database
    ) -> None:
        await OpportunityPipeline(database).run(StubCollector([make_raw()]))

        interaction = fake_interaction(bot)
        cog = AcmCommands(bot)
        await invoke(cog, cog.status, interaction)

        embed = interaction.followup.send.await_args.kwargs["embed"]
        awaiting = next(f.value for f in embed.fields if f.name == "Awaiting officer review")
        sources = next(f.value for f in embed.fields if f.name == "Sources")
        assert awaiting == "1"
        assert "stub" in sources


class TestAdminGuard:
    async def test_a_non_officer_is_blocked_before_the_command_runs(self, bot: MagicMock) -> None:
        cog = AdminCommands(bot)
        interaction = fake_interaction(bot, is_officer=False)

        assert await cog.interaction_check(interaction) is False
        interaction.response.send_message.assert_awaited_once()

    async def test_an_officer_passes_the_guard(self, bot: MagicMock) -> None:
        assert await AdminCommands(bot).interaction_check(fake_interaction(bot)) is True


class TestQueueCommand:
    async def test_an_empty_queue_is_reported_plainly(
        self, bot: MagicMock, database: Database
    ) -> None:
        cog = AdminCommands(bot)
        interaction = fake_interaction(bot)
        await invoke(cog, cog.queue, interaction)
        assert "No opportunities awaiting review" in interaction.followup.send.await_args.args[0]

    async def test_pending_opportunities_are_listed_with_their_ids(
        self, bot: MagicMock, database: Database
    ) -> None:
        row = await seed(database)
        async with database.session() as session:
            await OpportunityRepository(session).mark_queued_for_review(
                row.id, channel_id=1, message_id=2
            )

        cog = AdminCommands(bot)
        interaction = fake_interaction(bot)
        await invoke(cog, cog.queue, interaction)

        embed = interaction.followup.send.await_args.kwargs["embed"]
        assert str(row.id) in embed.description


class TestApproveAndRejectCommands:
    @pytest.mark.parametrize("bad_id", ["not-a-uuid", "", "12345"])
    async def test_a_malformed_id_is_rejected_before_touching_the_database(
        self, bot: MagicMock, database: Database, bad_id: str
    ) -> None:
        cog = AdminCommands(bot)
        interaction = fake_interaction(bot)
        await invoke(cog, cog.approve, interaction, bad_id)

        interaction.response.send_message.assert_awaited_once()
        assert "not a valid" in interaction.response.send_message.await_args.args[0]
        bot.publisher.publish_approved.assert_not_awaited()

    async def test_approving_by_id_publishes(self, bot: MagicMock, database: Database) -> None:
        row = await seed(database)
        cog = AdminCommands(bot)
        await invoke(cog, cog.approve, fake_interaction(bot), str(row.id))
        bot.publisher.publish_approved.assert_awaited_once_with(row.id)

    async def test_approving_an_unknown_id_is_reported(
        self, bot: MagicMock, database: Database
    ) -> None:
        from uuid import uuid4

        cog = AdminCommands(bot)
        interaction = fake_interaction(bot)
        await invoke(cog, cog.approve, interaction, str(uuid4()))
        assert "already reviewed" in interaction.followup.send.await_args.args[0]

    async def test_rejecting_by_id_records_the_reason(
        self, bot: MagicMock, database: Database
    ) -> None:
        row = await seed(database)
        cog = AdminCommands(bot)
        await invoke(cog, cog.reject, fake_interaction(bot), str(row.id), "off topic")

        async with database.session() as session:
            refreshed = await OpportunityRepository(session).get(row.id)
        assert refreshed is not None
        assert refreshed.reviews[0].decision is ReviewDecision.REJECTED
        assert refreshed.reviews[0].reason == "off topic"
        assert refreshed.reviews[0].reviewer_discord_id == OFFICER_ONE


class TestSyncAndHealthCommands:
    async def test_sync_reports_each_collector_result(
        self, bot: MagicMock, database: Database
    ) -> None:
        from acm_hub.pipeline.process import PipelineStats

        bot.scheduler.run_all_collectors = AsyncMock(
            return_value=[
                PipelineStats(collector="stub", fetched=3, new=2, duration_seconds=1.25),
                PipelineStats(collector="broken", error="upstream 500"),
            ]
        )
        cog = AdminCommands(bot)
        interaction = fake_interaction(bot)
        await invoke(cog, cog.sync, interaction)

        embed = interaction.followup.send.await_args.kwargs["embed"]
        values = {field.name: field.value for field in embed.fields}
        assert "new 2" in values["stub"]
        assert "upstream 500" in values["broken"]

    async def test_health_reports_a_reachable_database(
        self, bot: MagicMock, database: Database
    ) -> None:
        cog = AdminCommands(bot)
        interaction = fake_interaction(bot)
        await invoke(cog, cog.health, interaction)

        embed = interaction.followup.send.await_args.kwargs["embed"]
        values = {field.name: field.value for field in embed.fields}
        assert values["Database"] == "ok"
        assert embed.colour == discord.Colour.green()

    async def test_health_reports_an_unreachable_database_without_crashing(
        self, bot: MagicMock
    ) -> None:
        """The one moment an officer runs /health is when something is broken."""
        from acm_hub.db.session import Database as RealDatabase

        broken = RealDatabase("sqlite+aiosqlite:////nonexistent/path/acm.db")
        bot.database = broken
        cog = AdminCommands(bot)
        interaction = fake_interaction(bot)
        try:
            await invoke(cog, cog.health, interaction)
        finally:
            await broken.dispose()

        embed = interaction.followup.send.await_args.kwargs["embed"]
        values = {field.name: field.value for field in embed.fields}
        assert values["Database"] == "unreachable"
        assert values["Opportunities"] == "none stored"
        assert embed.colour == discord.Colour.red()

    async def test_requeue_reports_a_missing_channel_instead_of_crashing(
        self, bot: MagicMock, database: Database
    ) -> None:
        from acm_hub.publishing.discord import ChannelUnavailableError

        bot.publisher.queue_pending_reviews.side_effect = ChannelUnavailableError("channel 9 gone")
        cog = AdminCommands(bot)
        interaction = fake_interaction(bot)
        await invoke(cog, cog.requeue, interaction)

        assert "not reachable" in interaction.followup.send.await_args.args[0]
