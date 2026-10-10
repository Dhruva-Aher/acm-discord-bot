"""Member reports of invalid application links.

Postings publish without officer approval. When a link is dead, any member can
**Hide for me** (personal) or **Remove for everyone** (closes the opportunity
and marks published Discord messages ``[CLOSED]``).
"""

from __future__ import annotations

import contextlib
import re
from typing import TYPE_CHECKING, Any
from uuid import UUID

import discord

from acm_hub.bot.embeds.opportunity import ACM_GOLD, truncate
from acm_hub.db.repositories.hides import MemberHideRepository
from acm_hub.db.repositories.opportunities import OpportunityRepository
from acm_hub.domain.enums import OpportunityStatus
from acm_hub.observability.logging import get_logger

if TYPE_CHECKING:
    from acm_hub.bot.client import AcmHubBot

logger = get_logger(__name__)

REPORT_PREFIX = "acmhub:member:report:"
_UUID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def _bot(interaction: discord.Interaction) -> AcmHubBot:
    from acm_hub.bot.client import AcmHubBot

    client = interaction.client
    assert isinstance(client, AcmHubBot)
    return client


class ReportResolutionView(discord.ui.View):
    """Ephemeral follow-up: hide personally or remove for the whole chapter."""

    def __init__(self, opportunity_id: UUID) -> None:
        super().__init__(timeout=300)
        self.opportunity_id = opportunity_id

    @discord.ui.button(label="Hide for me", style=discord.ButtonStyle.secondary)
    async def hide_for_me(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        bot = _bot(interaction)
        async with bot.database.session() as session:
            created = await MemberHideRepository(session).hide(
                interaction.user.id,
                self.opportunity_id,
                reason="reported_invalid_link",
            )
        if created:
            await interaction.followup.send(
                "Hidden for you. It stays in the catalogue for everyone else.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "Already hidden from your results.", ephemeral=True
            )
        self.stop()

    @discord.ui.button(label="Remove for everyone", style=discord.ButtonStyle.danger)
    async def remove_for_everyone(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        bot = _bot(interaction)
        async with bot.database.session() as session:
            repository = OpportunityRepository(session)
            row = await repository.get(self.opportunity_id)
            if row is None or row.status is not OpportunityStatus.PUBLISHED:
                await interaction.followup.send(
                    "That posting is no longer published.", ephemeral=True
                )
                self.stop()
                return
            closed = await repository.mark_closed([self.opportunity_id])
        if not closed:
            await interaction.followup.send(
                "Could not remove that posting (already closed or rejected).",
                ephemeral=True,
            )
            self.stop()
            return

        edited = await bot.publisher.mark_published_messages_closed([self.opportunity_id])
        logger.info(
            "report.removed_for_everyone",
            opportunity_id=str(self.opportunity_id),
            reporter_id=interaction.user.id,
            messages_edited=edited,
        )
        await interaction.followup.send(
            "Removed for everyone. Published messages are marked ``[CLOSED]``.",
            ephemeral=True,
        )
        if interaction.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await interaction.message.edit(view=None)
        self.stop()


class ReportLinkButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]],
    template=re.compile(rf"^{re.escape(REPORT_PREFIX)}(?P<oid>{_UUID_PATTERN})$"),
):
    """Opens hide-for-me / remove-for-everyone choices."""

    def __init__(self, opportunity_id: UUID) -> None:
        super().__init__(
            discord.ui.Button(
                label="Report link",
                style=discord.ButtonStyle.danger,
                custom_id=f"{REPORT_PREFIX}{opportunity_id}",
            )
        )
        self.opportunity_id = opportunity_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[discord.ui.View],
        match: re.Match[str],
        /,
    ) -> ReportLinkButton:
        return cls(UUID(match.group("oid")))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = _bot(interaction)
        async with bot.database.session() as session:
            row = await OpportunityRepository(session).get(self.opportunity_id)
        if row is None or row.status is not OpportunityStatus.PUBLISHED:
            await interaction.response.send_message(
                "That posting is no longer published.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="Report invalid link",
            colour=ACM_GOLD,
            description=(
                f"**{truncate(row.title, 120)}** at **{truncate(row.organization, 80)}**\n"
                f"Application URL: {row.application_url}\n\n"
                "**Hide for me** — drops it from your browse/digest only.\n"
                "**Remove for everyone** — closes it for the whole chapter."
            ),
        )
        await interaction.response.send_message(
            embed=embed,
            view=ReportResolutionView(self.opportunity_id),
            ephemeral=True,
        )
