"""Member-facing ``/acm`` commands.

Milestone 4 ships ``/acm status`` only. ``/acm opportunities``, ``/acm research``
and ``/acm subscribe`` arrive with later milestones; stubbing them now would put
commands in the server's command list that do nothing useful.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from acm_hub.db.repositories.collector_runs import CollectorRunRepository
from acm_hub.db.repositories.opportunities import OpportunityRepository
from acm_hub.domain.enums import OpportunityStatus
from acm_hub.observability.logging import get_logger

if TYPE_CHECKING:
    from acm_hub.bot.client import AcmHubBot

logger = get_logger(__name__)


class AcmCommands(commands.Cog):
    """The ``/acm`` command group."""

    def __init__(self, bot: AcmHubBot) -> None:
        self.bot = bot

    acm = app_commands.Group(name="acm", description="ACM at ASU opportunity hub")

    @acm.command(name="status", description="Show what ACM Hub is tracking right now")
    async def status(self, interaction: discord.Interaction) -> None:
        """Report published counts and when each source last ran.

        Ephemeral so checking status never adds noise to a busy channel.
        """
        await interaction.response.defer(ephemeral=True, thinking=True)

        async with self.bot.database.session() as session:
            counts = await OpportunityRepository(session).count_by_status()
            runs = await CollectorRunRepository(session).latest_per_collector()

        embed = discord.Embed(
            title="ACM Hub status",
            colour=discord.Colour(0xFFC627),
            description="Automatically discovered technical opportunities for ACM at ASU.",
        )
        embed.add_field(
            name="Published",
            value=str(counts.get(OpportunityStatus.PUBLISHED, 0)),
            inline=True,
        )
        embed.add_field(
            name="Awaiting officer review",
            value=str(
                counts.get(OpportunityStatus.PENDING_REVIEW, 0)
                + counts.get(OpportunityStatus.DISCOVERED, 0)
            ),
            inline=True,
        )
        embed.add_field(name="Tracked overall", value=str(sum(counts.values())), inline=True)

        if runs:
            embed.add_field(
                name="Sources",
                value="\n".join(
                    f"**{run.collector}** \u2014 {run.status.value}"
                    + (
                        f", last run <t:{int(run.started_at.timestamp())}:R>"
                        if run.started_at
                        else ""
                    )
                    for run in sorted(runs, key=lambda item: item.collector)
                ),
                inline=False,
            )
        else:
            embed.add_field(
                name="Sources",
                value="No collection has run yet.",
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)
