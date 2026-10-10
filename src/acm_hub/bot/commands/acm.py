"""Member-facing ``/acm`` commands.

Commands: ``status``, ``opportunities``, ``research``, ``conferences``,
``digest``, ``resume-check``, ``company-prep`` and ``subscribe``.

Two properties are enforced throughout.

**Everything is ephemeral.** Browsing is a private action. A member paging
through internships should not push the channel's actual content off-screen.

**Filtering and paging happen in the database.** The command asks for one page
and a count; it never loads the catalogue to slice it in Python.
"""

from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING
from uuid import UUID

import discord
from discord import app_commands
from discord.ext import commands

from acm_hub.bot.embeds.opportunity import ACM_GOLD, format_locations, truncate
from acm_hub.bot.member_actions import build_company_prep_view, company_prep_embed
from acm_hub.bot.subscriptions import SubscriptionView, describe_interests
from acm_hub.bot.views import Page, PaginatedView
from acm_hub.config.sigs import SIG_NAMES
from acm_hub.db import models
from acm_hub.db.repositories.collector_runs import CollectorRunRepository
from acm_hub.db.repositories.hides import MemberHideRepository
from acm_hub.db.repositories.opportunities import OpportunityFilters, OpportunityRepository
from acm_hub.db.repositories.sources import SourceRepository
from acm_hub.db.repositories.subscriptions import SubscriptionRepository
from acm_hub.digest.html import DEFAULT_DIGEST_LIMIT, build_opportunities_html
from acm_hub.domain.enums import OpportunityStatus, OpportunityType
from acm_hub.observability.logging import get_logger
from acm_hub.prep.company import build_company_prep
from acm_hub.prep.resume import analyse_resume_against_listing, extract_pdf_text

if TYPE_CHECKING:
    from acm_hub.bot.client import AcmHubBot

logger = get_logger(__name__)

RESULTS_PER_PAGE = 5

SIG_CHOICES = [app_commands.Choice(name=sig, value=sig) for sig in SIG_NAMES[:25]]
TYPE_CHOICES = [
    app_commands.Choice(name=member.value.replace("_", " ").title(), value=member.value)
    for member in OpportunityType
]


def _result_line(row: models.Opportunity) -> str:
    """One search hit: title as a link, plus the facts the source actually gave."""
    from acm_hub.bot.insights import build_insights

    location = format_locations(list(row.locations), row.remote_status)
    sigs = " \u00b7 ".join(str(entry.get("sig", "")) for entry in row.sig_matches)
    freshness = build_insights(row).freshness
    prefix = "**Opened today** · " if freshness else ""
    line = (
        f"{prefix}**[{truncate(row.title, 90)}]({row.application_url})**\n"
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

        await self._send_browse(
            interaction,
            filters=filters,
            personalized=personalized,
            title_noun="Opportunities",
            empty=self._no_results_message(personalized=personalized, all_requested=all),
        )

    @acm.command(
        name="research", description="Browse ASU faculty currently labelled ready to mentor"
    )
    @app_commands.describe(
        sig="Only show mentors matched to this SIG",
        faculty="Only show faculty whose name matches this text",
        search="Match this text against the title or faculty name",
        all="Ignore your saved SIG interests",
    )
    @app_commands.choices(sig=SIG_CHOICES)
    async def research(
        self,
        interaction: discord.Interaction,
        sig: app_commands.Choice[str] | None = None,
        faculty: str | None = None,
        search: str | None = None,
        all: bool = False,
    ) -> None:
        """Search published research mentorships only.

        Saved opportunity *types* are ignored: this command already means
        research. Saved SIGs still apply when the member passed no other
        filter, and that is disclosed the same way as ``/acm opportunities``.
        """
        await interaction.response.defer(ephemeral=True, thinking=True)
        filters, personalized = await self._scoped_filters(
            interaction.user.id,
            locked_type=OpportunityType.RESEARCH,
            sig=sig.value if sig else None,
            organization=faculty,
            query=search,
            ignore_saved=all,
        )
        await self._send_browse(
            interaction,
            filters=filters,
            personalized=personalized,
            title_noun="Research",
            empty=self._scoped_empty_message(
                kind="research mentorships",
                command="research",
                personalized=personalized,
                all_requested=all,
            ),
        )

    @acm.command(
        name="conferences", description="Browse official ACM conferences ACM Hub has published"
    )
    @app_commands.describe(
        sig="Only show conferences matched to this SIG",
        search="Match this text against the title or organizer",
        all="Ignore your saved SIG interests",
    )
    @app_commands.choices(sig=SIG_CHOICES)
    async def conferences(
        self,
        interaction: discord.Interaction,
        sig: app_commands.Choice[str] | None = None,
        search: str | None = None,
        all: bool = False,
    ) -> None:
        """Search published official ACM conferences only."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        filters, personalized = await self._scoped_filters(
            interaction.user.id,
            locked_type=OpportunityType.CONFERENCE,
            sig=sig.value if sig else None,
            query=search,
            ignore_saved=all,
        )
        await self._send_browse(
            interaction,
            filters=filters,
            personalized=personalized,
            title_noun="Conferences",
            empty=self._scoped_empty_message(
                kind="conferences",
                command="conferences",
                personalized=personalized,
                all_requested=all,
            ),
        )

    async def _send_browse(
        self,
        interaction: discord.Interaction,
        *,
        filters: OpportunityFilters,
        personalized: str | None,
        title_noun: str,
        empty: str,
    ) -> None:
        """Fetch one page and reply with results or the empty-state copy."""

        async with self.bot.database.session() as session:
            hidden = await MemberHideRepository(session).hidden_ids(interaction.user.id)

        async def fetch(offset: int) -> Page[models.Opportunity]:
            async with self.bot.database.session() as session:
                repository = OpportunityRepository(session)
                items = await repository.search_published(
                    filters,
                    limit=RESULTS_PER_PAGE,
                    offset=offset,
                    exclude_ids=hidden,
                )
                total = await repository.count_published(filters, exclude_ids=hidden)
            return Page(items=items, total=total, offset=offset, page_size=RESULTS_PER_PAGE)

        page = await fetch(0)

        if not page.items:
            await interaction.followup.send(empty, ephemeral=True)
            return

        def render(current: Page[models.Opportunity]) -> discord.Embed:
            return self._results_embed(current, personalized=personalized, title_noun=title_noun)

        view = PaginatedView(interaction.user.id, fetch_page=fetch, render=render, page=page)
        message = await interaction.followup.send(
            embed=view.embed(), view=view, ephemeral=True, wait=True
        )
        view.bind(message)

    async def _scoped_filters(
        self,
        user_id: int,
        *,
        locked_type: OpportunityType,
        sig: str | None,
        organization: str | None = None,
        query: str | None = None,
        ignore_saved: bool,
    ) -> tuple[OpportunityFilters, str | None]:
        """Build filters for a command that is already locked to one type.

        Saved types are never applied: the command itself is the type filter.
        Saved SIGs apply only when the member did not pass another narrowing
        option and did not ask for ``all``.
        """
        explicit = OpportunityFilters(
            opportunity_types=(locked_type,),
            sigs=(sig,) if sig else (),
            organization=organization,
            query=query,
        )
        if sig or organization or query or ignore_saved:
            return explicit, None

        saved, _personalized = await self._personalized_filters(user_id)
        if not saved.sigs:
            return OpportunityFilters(opportunity_types=(locked_type,)), None
        return (
            OpportunityFilters(opportunity_types=(locked_type,), sigs=saved.sigs),
            describe_interests([], list(saved.sigs)),
        )

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
    def _scoped_empty_message(
        *,
        kind: str,
        command: str,
        personalized: str | None,
        all_requested: bool,
    ) -> str:
        if personalized:
            return (
                f"Nothing published matches your ACM interests: {personalized}.\n"
                f"Try `/acm {command} all:true` to see every published {kind}, or "
                "`/acm subscribe` to adjust them."
            )
        if all_requested:
            return f"No {kind} have been published yet."
        return (
            f"Nothing published matches that search. Try fewer filters, or "
            f"`/acm {command}` with none."
        )

    @staticmethod
    def _results_embed(
        page: Page[models.Opportunity],
        *,
        personalized: str | None,
        title_noun: str = "Opportunities",
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"{title_noun} \u2014 {page.total} match" + ("" if page.total == 1 else "es"),
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

    # --------------------------------------------------------------- digest

    @acm.command(
        name="digest",
        description="Download an HTML digest of published opportunities",
    )
    @app_commands.describe(
        type="Only include one kind of opportunity",
        sig="Only include opportunities matched to this SIG",
        organization="Only include organizations matching this text",
        remote="Only include postings the source describes as remote or hybrid",
        search="Match this text against the title or organization",
        all="Ignore your saved interests and include everything published",
    )
    @app_commands.choices(sig=SIG_CHOICES, type=TYPE_CHOICES)
    async def digest(
        self,
        interaction: discord.Interaction,
        type: app_commands.Choice[str] | None = None,
        sig: app_commands.Choice[str] | None = None,
        organization: str | None = None,
        remote: bool = False,
        search: str | None = None,
        all: bool = False,
    ) -> None:
        """Build a self-contained HTML file from published opportunities.

        Same facts as the Discord embeds, as a downloadable page members can
        open offline or share. Does not replace officer approval: only
        published rows are included.
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

        async with self.bot.database.session() as session:
            hidden = await MemberHideRepository(session).hidden_ids(interaction.user.id)
            repository = OpportunityRepository(session)
            total = await repository.count_published(filters, exclude_ids=hidden)
            rows = await repository.search_published(
                filters, limit=DEFAULT_DIGEST_LIMIT, offset=0, exclude_ids=hidden
            )
            sources = await SourceRepository(session).list_all()

        if total == 0:
            await interaction.followup.send(
                self._no_results_message(personalized=personalized, all_requested=all),
                ephemeral=True,
            )
            return

        display_names = {source.name: source.display_name for source in sources}
        display_names.update(self.bot.publisher.source_display_names)
        digest = build_opportunities_html(
            rows,
            total_matching=total,
            source_display_names=display_names,
            filter_summary=personalized,
        )
        filename = (
            f"acm-hub-digest-{digest.generated_at.astimezone().strftime('%Y%m%d-%H%M')}.html"
        )
        file = discord.File(BytesIO(digest.html.encode("utf-8")), filename=filename)

        summary = (
            f"HTML digest with **{digest.included}** published opportunit"
            f"{'y' if digest.included == 1 else 'ies'}"
        )
        if digest.truncated:
            summary += f" (of {digest.total_matching} matching)"
        if personalized:
            summary += f"\nUsing your ACM interests: {truncate(personalized, 200)}"
        summary += (
            "\nOpen the file in a browser. Details come from sources; "
            "ACM Hub does not invent eligibility."
        )

        await interaction.followup.send(summary, file=file, ephemeral=True)

    # ----------------------------------------------------------- resume / prep

    @acm.command(
        name="resume-check",
        description="Upload a PDF resume and compare it to a published listing's themes",
    )
    @app_commands.describe(
        resume="Your resume as a PDF (text PDF, not a scanned image)",
        opportunity_id="UUID from the Resume check button or opportunity card",
    )
    async def resume_check(
        self,
        interaction: discord.Interaction,
        resume: discord.Attachment,
        opportunity_id: str,
    ) -> None:
        """Keyword-scan a PDF against listing themes — not an AI interview prediction."""
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            oid = UUID(opportunity_id.strip())
        except ValueError:
            await interaction.followup.send(
                "That opportunity id is not a valid UUID. Use the id from the "
                "**Resume check** button on a published posting.",
                ephemeral=True,
            )
            return

        if not (resume.filename or "").lower().endswith(".pdf"):
            await interaction.followup.send("Please attach a `.pdf` file.", ephemeral=True)
            return
        if resume.size is not None and resume.size > 5 * 1024 * 1024:
            await interaction.followup.send("PDF must be under 5 MB.", ephemeral=True)
            return

        async with self.bot.database.session() as session:
            row = await OpportunityRepository(session).get(oid)
        if row is None or row.status is not OpportunityStatus.PUBLISHED:
            await interaction.followup.send(
                "No published opportunity matches that id.", ephemeral=True
            )
            return

        try:
            data = await resume.read()
            text, pages = extract_pdf_text(data)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        report = analyse_resume_against_listing(text, row, page_count=pages)
        embed = discord.Embed(
            title="Resume check result",
            colour=ACM_GOLD,
            description=(
                f"**{truncate(row.title, 120)}** at **{truncate(row.organization, 80)}**\n"
                f"Pages read: {report.page_count} · Characters: {report.resume_chars}"
            ),
        )
        if report.themes_found:
            embed.add_field(
                name="Themes found in your resume",
                value=" · ".join(f"`{t}`" for t in report.themes_found) or "—",
                inline=False,
            )
        if report.themes_missing:
            embed.add_field(
                name="Themes not found in resume text",
                value=" · ".join(f"`{t}`" for t in report.themes_missing) or "—",
                inline=False,
            )
        elif report.themes_checked:
            embed.add_field(
                name="Themes not found in resume text",
                value="None of the checked themes were missing.",
                inline=False,
            )
        embed.add_field(
            name="Notes",
            value="\n".join(report.notes),
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

        channel_id = self.bot.settings.resume_review_channel_id
        if channel_id is None:
            return
        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            if isinstance(channel, discord.abc.Messageable):
                public = discord.Embed(
                    title="Resume check shared",
                    colour=ACM_GOLD,
                    description=(
                        f"{interaction.user.mention} checked a resume against "
                        f"**{truncate(row.organization, 60)}** / {truncate(row.title, 80)}"
                    ),
                )
                if report.themes_missing:
                    public.add_field(
                        name="Themes not found",
                        value=" · ".join(f"`{t}`" for t in report.themes_missing[:8]),
                        inline=False,
                    )
                await channel.send(embed=public)
        except discord.HTTPException as exc:
            logger.warning(
                "resume_check.share_failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    @acm.command(
        name="company-prep",
        description="LeetCode / OA search links and listing practice themes for a company",
    )
    @app_commands.describe(
        opportunity_id="UUID of a published opportunity (preferred)",
        organization="Company name if you do not have an opportunity id",
    )
    async def company_prep(
        self,
        interaction: discord.Interaction,
        opportunity_id: str | None = None,
        organization: str | None = None,
    ) -> None:
        """Company prep links — community sources only, no invented question lists."""
        await interaction.response.defer(ephemeral=True, thinking=True)

        row: models.Opportunity | None = None
        if opportunity_id:
            try:
                oid = UUID(opportunity_id.strip())
            except ValueError:
                await interaction.followup.send(
                    "That opportunity id is not a valid UUID.", ephemeral=True
                )
                return
            async with self.bot.database.session() as session:
                row = await OpportunityRepository(session).get(oid)
            if row is None or row.status is not OpportunityStatus.PUBLISHED:
                await interaction.followup.send(
                    "No published opportunity matches that id.", ephemeral=True
                )
                return
        elif organization and organization.strip():
            filters = OpportunityFilters(organization=organization.strip())
            async with self.bot.database.session() as session:
                matches = await OpportunityRepository(session).search_published(
                    filters, limit=1, offset=0
                )
            if not matches:
                await interaction.followup.send(
                    "No published opportunity matched that organization. "
                    "Publish one first, or pass an opportunity id from a posting.",
                    ephemeral=True,
                )
                return
            row = matches[0]
        else:
            await interaction.followup.send(
                "Pass `opportunity_id` from a posting, or an `organization` name.",
                ephemeral=True,
            )
            return

        prep = build_company_prep(row)
        embed = company_prep_embed(row)
        view = build_company_prep_view(prep)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        channel_id = self.bot.settings.company_prep_channel_id
        if channel_id is None:
            return
        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            if isinstance(channel, discord.abc.Messageable):
                await channel.send(
                    content=(
                        f"{interaction.user.mention} · company prep for **{prep.company}**"
                    ),
                    embed=embed,
                    view=view,
                )
        except discord.HTTPException as exc:
            logger.warning(
                "company_prep.share_failed",
                error=f"{type(exc).__name__}: {exc}",
            )

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

        source_links = {source.name: source.source_url for source in sources}
        if runs:
            lines: list[str] = []
            for run in sorted(runs, key=lambda item: item.collector):
                label = display_names.get(run.collector, run.collector)
                link = source_links.get(run.collector)
                when = (
                    f", last run <t:{int(run.started_at.timestamp())}:R>"
                    if run.started_at
                    else ""
                )
                if link:
                    lines.append(
                        f"**{label}** \u2014 {run.status.value}{when}\n"
                        f"[Open source]({link})"
                    )
                else:
                    lines.append(f"**{label}** \u2014 {run.status.value}{when}")
            embed.add_field(name="Sources", value="\n".join(lines), inline=False)
        elif sources:
            embed.add_field(
                name="Sources",
                value="\n".join(
                    f"**{source.display_name}**\n[Open source]({source.source_url})"
                    for source in sorted(sources, key=lambda item: item.name)
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
