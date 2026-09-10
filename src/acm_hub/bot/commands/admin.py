"""Officer-only ``/acm-admin`` commands.

Every command in this group is gated twice: a Discord-side default permission so
the commands are hidden from ordinary members, and a runtime check against
``OFFICER_ROLE_ID``. The Discord-side gate alone is not enough, because server
administrators can re-expose commands to any role.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import discord
from discord import app_commands
from discord.ext import commands

from acm_hub.bot.embeds.opportunity import truncate
from acm_hub.bot.permissions import is_officer
from acm_hub.db.repositories.collector_runs import CollectorRunRepository
from acm_hub.db.repositories.opportunities import OpportunityRepository
from acm_hub.domain.enums import ReviewDecision
from acm_hub.observability.logging import get_logger
from acm_hub.publishing.discord import ChannelUnavailableError

if TYPE_CHECKING:
    from acm_hub.bot.client import AcmHubBot

logger = get_logger(__name__)

QUEUE_PAGE_SIZE = 10


def _parse_opportunity_id(value: str) -> UUID | None:
    try:
        return UUID(value.strip())
    except ValueError:
        return None


class AdminCommands(commands.Cog):
    """The ``/acm-admin`` command group."""

    def __init__(self, bot: AcmHubBot) -> None:
        self.bot = bot

    admin = app_commands.Group(
        name="acm-admin",
        description="ACM Hub officer controls",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Block non-officers before any command body runs."""
        if is_officer(interaction.user, self.bot.settings.officer_role_id):
            return True
        await interaction.response.send_message(
            "This command is limited to ACM officers.", ephemeral=True
        )
        return False

    @admin.command(name="queue", description="List opportunities awaiting officer review")
    async def queue(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        async with self.bot.database.session() as session:
            pending = await OpportunityRepository(session).list_pending_review(QUEUE_PAGE_SIZE)

        if not pending:
            await interaction.followup.send("The review queue is empty.", ephemeral=True)
            return

        lines = [
            f"`{row.id}`\n**{truncate(row.title, 80)}** \u2014 {truncate(row.organization, 40)}"
            for row in pending
        ]
        embed = discord.Embed(
            title=f"Review queue ({len(pending)} shown)",
            description="\n\n".join(lines),
            colour=discord.Colour(0x8C1D40),
        )
        embed.set_footer(text="Use the buttons on the review cards, or /acm-admin approve <id>.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @admin.command(name="approve", description="Approve and publish an opportunity by ID")
    @app_commands.describe(opportunity_id="The opportunity UUID shown in /acm-admin queue")
    async def approve(self, interaction: discord.Interaction, opportunity_id: str) -> None:
        parsed = _parse_opportunity_id(opportunity_id)
        if parsed is None:
            await interaction.response.send_message(
                "That is not a valid opportunity ID.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        async with self.bot.database.session() as session:
            accepted = await OpportunityRepository(session).record_decision(
                parsed,
                decision=ReviewDecision.APPROVED,
                reviewer_discord_id=interaction.user.id,
            )

        if not accepted:
            await interaction.followup.send(
                "That opportunity does not exist or was already reviewed.", ephemeral=True
            )
            return

        try:
            published = await self.bot.publisher.publish_approved(parsed)
        except (discord.HTTPException, ChannelUnavailableError) as exc:
            logger.error(
                "admin.publish_failed",
                opportunity_id=str(parsed),
                error=f"{type(exc).__name__}: {exc}",
                exc_info=True,
            )
            await interaction.followup.send(
                "Approved, but publishing failed. Check the opportunities channel configuration.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            "Approved and published." if published else "Approved; it was already published.",
            ephemeral=True,
        )

    @admin.command(name="reject", description="Reject an opportunity by ID")
    @app_commands.describe(
        opportunity_id="The opportunity UUID shown in /acm-admin queue",
        reason="Optional reason, stored for auditing",
    )
    async def reject(
        self, interaction: discord.Interaction, opportunity_id: str, reason: str | None = None
    ) -> None:
        parsed = _parse_opportunity_id(opportunity_id)
        if parsed is None:
            await interaction.response.send_message(
                "That is not a valid opportunity ID.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        async with self.bot.database.session() as session:
            accepted = await OpportunityRepository(session).record_decision(
                parsed,
                decision=ReviewDecision.REJECTED,
                reviewer_discord_id=interaction.user.id,
                reason=reason,
            )

        await interaction.followup.send(
            "Rejected. It will not be re-queued."
            if accepted
            else "That opportunity does not exist or was already reviewed.",
            ephemeral=True,
        )

    @admin.command(name="sync", description="Run every collector now and refresh the review queue")
    async def sync(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        results = await self.bot.scheduler.run_all_collectors()

        embed = discord.Embed(title="Collection run", colour=discord.Colour(0xFFC627))
        for stats in results:
            value = (
                f"error: {truncate(stats.error, 200)}"
                if stats.error
                else (
                    f"fetched {stats.fetched} \u00b7 new {stats.new} \u00b7 "
                    f"updated {stats.updated} \u00b7 duplicates {stats.duplicates} \u00b7 "
                    f"rejected {stats.rejected} \u00b7 {stats.duration_seconds:.1f}s"
                )
            )
            embed.add_field(name=stats.collector, value=value, inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @admin.command(name="health", description="Check database, scheduler and collector health")
    async def health(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        database_ok = await self.bot.database.check_connection()
        async with self.bot.database.session() as session:
            runs = await CollectorRunRepository(session).latest_per_collector()
            counts = await OpportunityRepository(session).count_by_status()

        embed = discord.Embed(
            title="ACM Hub health",
            colour=discord.Colour.green() if database_ok else discord.Colour.red(),
        )
        embed.add_field(name="Database", value="ok" if database_ok else "unreachable", inline=True)
        embed.add_field(
            name="Scheduler",
            value="running" if self.bot.scheduler.running else "stopped",
            inline=True,
        )
        embed.add_field(
            name="Gateway latency",
            value=f"{self.bot.latency * 1000:.0f} ms" if self.bot.latency else "unknown",
            inline=True,
        )
        embed.add_field(
            name="Opportunities",
            value="\n".join(f"{status.value}: {count}" for status, count in sorted(counts.items()))
            or "none stored",
            inline=False,
        )
        embed.add_field(
            name="Last collector runs",
            value="\n".join(
                f"**{run.collector}**: {run.status.value}"
                + (f" \u2014 {truncate(run.error, 100)}" if run.error else "")
                for run in sorted(runs, key=lambda item: item.collector)
            )
            or "no runs recorded",
            inline=False,
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @admin.command(
        name="requeue", description="Re-post review cards for opportunities awaiting review"
    )
    async def requeue(self, interaction: discord.Interaction) -> None:
        """Recover from a review channel that was unavailable during collection."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            posted = await self.bot.publisher.queue_pending_reviews(
                self.bot.settings.review_queue_batch_limit
            )
        except ChannelUnavailableError as exc:
            await interaction.followup.send(
                f"The officer review channel is not reachable: {exc}", ephemeral=True
            )
            return
        await interaction.followup.send(f"Posted {posted} review card(s).", ephemeral=True)
