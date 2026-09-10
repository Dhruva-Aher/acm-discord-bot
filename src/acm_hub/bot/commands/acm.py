"""Member-facing ``/acm`` commands.

Three commands ship here: ``status``, ``opportunities`` and ``subscribe``.
``/acm research`` belongs to a later milestone and is deliberately absent -
registering a command that does nothing useful is worse than not having it.

Two properties are enforced throughout.

**Everything is ephemeral.** Browsing is a private action. A member paging
through internships should not push the channel's actual content off-screen.

**Filtering and paging happen in the database.** The command asks for one page
and a count; it never loads the catalogue to slice it in Python.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from acm_hub.bot.embeds.opportunity import format_locations, truncate
from acm_hub.bot.subscriptions import SubscriptionView, describe_interests
from acm_hub.bot.views import Page, PaginatedView
from acm_hub.config.sigs import SIG_NAMES
from acm_hub.db import models
from acm_hub.db.repositories.collector_runs import CollectorRunRepository
from acm_hub.db.repositories.opportunities import OpportunityFilters, OpportunityRepository
from acm_hub.db.repositories.sources import SourceRepository
from acm_hub.db.repositories.subscriptions import SubscriptionRepository
from acm_hub.domain.enums import OpportunityStatus, OpportunityType
from acm_hub.observability.logging import get_logger

if TYPE_CHECKING:
    from acm_hub.bot.client import AcmHubBot

logger = get_logger(__name__)

RESULTS_PER_PAGE = 5
ACM_GOLD = discord.Colour(0xFFC627)

SIG_CHOICES = [app_commands.Choice(name=sig, value=sig) for sig in SIG_NAMES[:25]]
TYPE_CHOICES = [
    app_commands.Choice(name=member.value.replace("_", " ").title(), value=member.value)
    for member in OpportunityType
]


def _result_line(row: models.Opportunity) -> str:
    """One search hit: title as a link, plus the facts the source actually gave."""
    location = format_locations(list(row.locations), row.remote_status)
    sigs = " \u00b7 ".join(str(entry.get("sig", "")) for entry in row.sig_matches)
    line = (
        f"**[{truncate(row.title, 90)}]({row.application_url})**\n"
        f"{truncate(row.organization, 60)} \u2014 {truncate(location, 60)}"
    )
    if sigs:
        line += f"\n{sigs}"
    return line


class AcmCommands(commands.Cog):
    """The ``/acm`` command group."""

    def __init__(self, bot: AcmHubBot) -> None:
        self.bot = bot

    acm = app_commands.Group(name="acm", description="ACM at ASU opportunity hub")

    # ---------------------------------------------------------------- browse

    @acm.command(name="opportunities", description="Browse opportunities ACM Hub has published")
    @app_commands.describe(
        type="Only show one kind of opportunity",
        sig="Only show opportunities matched to this SIG",
        organization="Only show organizations matching this text",
        remote="Only show postings the source describes as remote or hybrid",
        search="Match this text against the title or organization",
        all="Ignore your saved interests and search everything",
    )
    @app_commands.choices(sig=SIG_CHOICES, type=TYPE_CHOICES)
    async def opportunities(
        self,
        interaction: discord.Interaction,
        type: app_commands.Choice[str] | None = None,
        sig: app_commands.Choice[str] | None = None,
        organization: str | None = None,
        remote: bool = False,
        search: str | None = None,
        all: bool = False,
    ) -> None:
        """Search published opportunities, personalized unless told otherwise.

        With no filters at all, the member's saved interests are applied and
        the fact that they were applied is stated in the reply, so nobody
        concludes that ACM Hub has nothing when it merely has nothing matching
        their saved SIGs. ``all:true`` is the always-available escape.
        """
        await interaction.response.defer(ephemeral=True, thinking=True)

        explicit = OpportunityFilters(
            opportunity_types=(OpportunityType(type.value),) if type else (),
            sigs=(sig.value,) if sig else (),
            organization=organization,
            remote_only=remote,
            query=search,
        )

        filters = explicit
        personalized: str | None = None
        if explicit.is_empty and not all:
            filters, personalized = await self._personalized_filters(interaction.user.id)

        async def fetch(offset: int) -> Page[models.Opportunity]:
            async with self.bot.database.session() as session:
                repository = OpportunityRepository(session)
                items = await repository.search_published(
                    filters, limit=RESULTS_PER_PAGE, offset=offset
                )
                total = await repository.count_published(filters)
            return Page(items=items, total=total, offset=offset, page_size=RESULTS_PER_PAGE)

        page = await fetch(0)

        if not page.items:
            await interaction.followup.send(
                self._no_results_message(personalized=personalized, all_requested=all),
                ephemeral=True,
            )
            return

        def render(current: Page[models.Opportunity]) -> discord.Embed:
            return self._results_embed(current, personalized=personalized)

        view = PaginatedView(interaction.user.id, fetch_page=fetch, render=render, page=page)
        message = await interaction.followup.send(
            embed=view.embed(), view=view, ephemeral=True, wait=True
        )
        view.bind(message)

    async def _personalized_filters(self, user_id: int) -> tuple[OpportunityFilters, str | None]:
        """Turn a member's saved interests into filters, if they saved any."""
        async with self.bot.database.session() as session:
            saved = await SubscriptionRepository(session).get(user_id)

        if saved is None or not (saved.opportunity_types or saved.sigs):
            return OpportunityFilters(), None

        # A stored value that is no longer a known type is skipped rather than
        # raising: an enum rename must degrade someone's saved filter, not
        # break their command.
        known = {member.value for member in OpportunityType}
        types = tuple(OpportunityType(value) for value in saved.opportunity_types if value in known)
        return (
            OpportunityFilters(opportunity_types=types, sigs=tuple(saved.sigs)),
            describe_interests(list(saved.opportunity_types), list(saved.sigs)),
        )

    @staticmethod
    def _no_results_message(*, personalized: str | None, all_requested: bool) -> str:
        if personalized:
            return (
                f"Nothing published matches your ACM interests: {personalized}.\n"
                "Try `/acm opportunities all:true` to see everything, or "
                "`/acm subscribe` to adjust them."
            )
        if all_requested:
            return "Nothing has been published yet."
        return (
            "Nothing published matches that search. Try fewer filters, or "
            "`/acm opportunities` with none."
        )

    @staticmethod
    def _results_embed(
        page: Page[models.Opportunity], *, personalized: str | None
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"Opportunities \u2014 {page.total} match" + ("" if page.total == 1 else "es"),
            colour=ACM_GOLD,
            description="\n\n".join(_result_line(row) for row in page.items),
        )
        if personalized:
            # Stated on every page: a member who filtered nothing still needs
            # to know their saved interests are narrowing the results.
            embed.set_author(name=f"Using your ACM interests: {truncate(personalized, 200)}")
        embed.set_footer(
            text=(
                f"Page {page.page_number} of {page.page_count} \u2022 "
                "details come from the source; ACM Hub does not verify them"
            )
        )
        return embed

    # ------------------------------------------------------------ preferences

    @acm.command(name="subscribe", description="Choose the interests that personalize your results")
    async def subscribe(self, interaction: discord.Interaction) -> None:
        """Edit saved interests.

        Named ``subscribe`` because that is what members look for, but it only
        stores preferences: ACM Hub sends no direct messages, which the view
        says explicitly.
        """
        await interaction.response.defer(ephemeral=True, thinking=True)

        async with self.bot.database.session() as session:
            saved = await SubscriptionRepository(session).get(interaction.user.id)

        view = SubscriptionView(
            interaction.user.id,
            database=self.bot.database,
            opportunity_types=list(saved.opportunity_types) if saved else [],
            sigs=list(saved.sigs) if saved else [],
        )
        message = await interaction.followup.send(
            view.summary(), view=view, ephemeral=True, wait=True
        )
        view.bind(message)

    # ---------------------------------------------------------------- status

    @acm.command(name="status", description="Show what ACM Hub is tracking right now")
    async def status(self, interaction: discord.Interaction) -> None:
        """Report published counts and when each source last ran.

        Ephemeral so checking status never adds noise to a busy channel.
        """
        await interaction.response.defer(ephemeral=True, thinking=True)

        async with self.bot.database.session() as session:
            counts = await OpportunityRepository(session).count_by_status()
            runs = await CollectorRunRepository(session).latest_per_collector()
            sources = await SourceRepository(session).list_all()

        display_names = {source.name: source.display_name for source in sources}

        embed = discord.Embed(
            title="ACM Hub status",
            colour=ACM_GOLD,
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
        embed.add_field(
            name="Closed since posting",
            value=str(counts.get(OpportunityStatus.CLOSED, 0)),
            inline=True,
        )
        embed.add_field(name="Tracked overall", value=str(sum(counts.values())), inline=True)

        if runs:
            embed.add_field(
                name="Sources",
                value="\n".join(
                    f"**{display_names.get(run.collector, run.collector)}** "
                    f"\u2014 {run.status.value}"
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
