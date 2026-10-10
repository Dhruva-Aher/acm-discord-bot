"""Member buttons on published opportunity posts.

Persistent :class:`~discord.ui.DynamicItem` handlers so Apply / Resume check /
Company prep keep working after a restart. Resume PDF intake uses the
``/acm resume-check`` slash command (attachments are not available on buttons
without privileged message-content intents).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from uuid import UUID

import discord

from acm_hub.bot.embeds.opportunity import ACM_GOLD, truncate
from acm_hub.bot.insights import MAX_LINK_LABEL, build_insights
from acm_hub.bot.report import ReportLinkButton
from acm_hub.db import models
from acm_hub.db.repositories.opportunities import OpportunityRepository
from acm_hub.observability.logging import get_logger
from acm_hub.prep.company import build_company_prep

if TYPE_CHECKING:
    from acm_hub.bot.client import AcmHubBot

logger = get_logger(__name__)

RESUME_PREFIX = "acmhub:member:resume:"
PREP_PREFIX = "acmhub:member:prep:"
_UUID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def _bot(interaction: discord.Interaction) -> AcmHubBot:
    from acm_hub.bot.client import AcmHubBot

    client = interaction.client
    assert isinstance(client, AcmHubBot)
    return client


async def _load_opportunity(
    interaction: discord.Interaction, opportunity_id: UUID
) -> models.Opportunity | None:
    bot = _bot(interaction)
    async with bot.database.session() as session:
        return await OpportunityRepository(session).get(opportunity_id)


def company_prep_embed(row: models.Opportunity) -> discord.Embed:
    prep = build_company_prep(row)
    embed = discord.Embed(
        title=truncate(f"Company prep · {prep.company}", 250),
        colour=ACM_GOLD,
        description=truncate(row.title, 200),
    )
    if prep.practice_themes:
        embed.add_field(
            name="Practice themes from this listing",
            value=" · ".join(f"`{tag}`" for tag in prep.practice_themes),
            inline=False,
        )
    embed.add_field(
        name="Links",
        value="\n".join(f"[{link.label}]({link.url})" for link in prep.links),
        inline=False,
    )
    embed.set_footer(text=prep.disclaimer)
    return embed


def resume_prompt_embed(row: models.Opportunity, *, opportunity_id: UUID) -> discord.Embed:
    from acm_hub.bot.insights import focus_tags_for

    themes = focus_tags_for(row)
    embed = discord.Embed(
        title="Resume check",
        colour=ACM_GOLD,
        description=(
            f"Listing: **{truncate(row.title, 120)}** at **{truncate(row.organization, 80)}**\n"
            f"Upload a **PDF** with:\n"
            f"`/acm resume-check opportunity_id:{opportunity_id}`"
        ),
    )
    if themes:
        embed.add_field(
            name="Themes we will scan for",
            value=" · ".join(f"`{tag}`" for tag in themes),
            inline=False,
        )
    embed.set_footer(
        text="Keyword scan only — not an AI review, not a prediction of interview outcomes."
    )
    return embed


class ResumeCheckButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]],
    template=re.compile(rf"^{re.escape(RESUME_PREFIX)}(?P<oid>{_UUID_PATTERN})$"),
):
    def __init__(self, opportunity_id: UUID) -> None:
        super().__init__(
            discord.ui.Button(
                label="Resume check",
                style=discord.ButtonStyle.secondary,
                custom_id=f"{RESUME_PREFIX}{opportunity_id}",
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
    ) -> ResumeCheckButton:
        return cls(UUID(match.group("oid")))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await _load_opportunity(interaction, self.opportunity_id)
        if row is None:
            await interaction.followup.send(
                "That opportunity is no longer available.", ephemeral=True
            )
            return

        embed = resume_prompt_embed(row, opportunity_id=self.opportunity_id)
        await interaction.followup.send(embed=embed, ephemeral=True)

        bot = _bot(interaction)
        channel_id = bot.settings.resume_review_channel_id
        if channel_id is None:
            return
        try:
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                return
            await channel.send(
                content=(
                    f"{interaction.user.mention} opened a resume check for "
                    f"**{truncate(row.organization, 60)}** — "
                    f"`/acm resume-check opportunity_id:{self.opportunity_id}` with a PDF."
                ),
                embed=embed,
            )
        except discord.HTTPException as exc:
            logger.warning(
                "resume_check.channel_post_failed",
                channel_id=channel_id,
                error=f"{type(exc).__name__}: {exc}",
            )


class CompanyPrepButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]],
    template=re.compile(rf"^{re.escape(PREP_PREFIX)}(?P<oid>{_UUID_PATTERN})$"),
):
    def __init__(self, opportunity_id: UUID) -> None:
        super().__init__(
            discord.ui.Button(
                label="Company prep",
                style=discord.ButtonStyle.secondary,
                custom_id=f"{PREP_PREFIX}{opportunity_id}",
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
    ) -> CompanyPrepButton:
        return cls(UUID(match.group("oid")))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await _load_opportunity(interaction, self.opportunity_id)
        if row is None:
            await interaction.followup.send(
                "That opportunity is no longer available.", ephemeral=True
            )
            return

        embed = company_prep_embed(row)
        await interaction.followup.send(embed=embed, ephemeral=True)

        bot = _bot(interaction)
        channel_id = bot.settings.company_prep_channel_id
        if channel_id is None:
            return
        try:
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                return
            await channel.send(
                content=(
                    f"{interaction.user.mention} pulled company prep for "
                    f"**{truncate(row.organization, 80)}**."
                ),
                embed=embed,
            )
        except discord.HTTPException as exc:
            logger.warning(
                "company_prep.channel_post_failed",
                channel_id=channel_id,
                error=f"{type(exc).__name__}: {exc}",
            )


def build_member_action_view(row: models.Opportunity) -> discord.ui.View:
    """Apply, prep tools, report dead links, and one research search link."""
    view: discord.ui.View = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="Apply",
            style=discord.ButtonStyle.link,
            url=row.application_url,
        )
    )
    view.add_item(ResumeCheckButton(row.id))
    view.add_item(CompanyPrepButton(row.id))
    view.add_item(ReportLinkButton(row.id))

    insights = build_insights(row)
    if insights.research_links:
        link = insights.research_links[0]
        label = link.label
        if len(label) > MAX_LINK_LABEL:
            label = label[: MAX_LINK_LABEL - 1] + "…"
        view.add_item(
            discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=link.url)
        )
    return view
