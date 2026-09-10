"""Officer-only ``/acm-admin`` commands.

Every command in this group is gated twice: a Discord-side default permission so
the commands are hidden from ordinary members, and a runtime check against
``OFFICER_ROLE_ID``. The Discord-side gate alone is not enough, because server
administrators can re-expose commands to any role.

Everything an officer might otherwise need database access for is available
here: changing a source's trust policy, releasing an initial import into the
review queue, and putting a rejected opportunity back in front of reviewers.
Nothing in this module prints tokens, connection strings or tracebacks - a
failure is reported as something the officer can act on, and the detail goes to
the logs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import discord
from discord import app_commands
from discord.ext import commands

from acm_hub.bot.embeds.opportunity import truncate
from acm_hub.bot.permissions import is_officer
from acm_hub.bot.views import ConfirmationView
from acm_hub.config.sigs import SIG_NAMES
from acm_hub.db import models
from acm_hub.db.repositories.collector_runs import CollectorRunRepository
from acm_hub.db.repositories.opportunities import OpportunityRepository
from acm_hub.db.repositories.sources import SourceRepository
from acm_hub.db.repositories.subscriptions import SubscriptionRepository
from acm_hub.domain.enums import (
    OpportunityStatus,
    OpportunityType,
    PublicationPolicy,
    ReviewDecision,
)
from acm_hub.observability.logging import get_logger
from acm_hub.publishing.discord import ChannelUnavailableError

if TYPE_CHECKING:
    from acm_hub.bot.client import AcmHubBot

logger = get_logger(__name__)

QUEUE_PAGE_SIZE = 10
MAX_BULK_APPROVE = 25

SIG_CHOICES = [app_commands.Choice(name=sig, value=sig) for sig in SIG_NAMES[:25]]
TYPE_CHOICES = [
    app_commands.Choice(name=member.value.replace("_", " ").title(), value=member.value)
    for member in OpportunityType
]
POLICY_CHOICES = [
    app_commands.Choice(name="Manual review (default)", value=PublicationPolicy.MANUAL_REVIEW),
    app_commands.Choice(name="Auto-publish", value=PublicationPolicy.AUTO_PUBLISH),
    app_commands.Choice(name="Disabled", value=PublicationPolicy.DISABLED),
]


def _parse_opportunity_id(value: str) -> UUID | None:
    try:
        return UUID(value.strip())
    except ValueError:
        return None


def _queue_line(row: models.Opportunity) -> str:
    return f"`{row.id}`\n**{truncate(row.title, 80)}** \u2014 {truncate(row.organization, 40)}"


class AdminCommands(commands.Cog):
    """The ``/acm-admin`` command group."""

    def __init__(self, bot: AcmHubBot) -> None:
        self.bot = bot

    admin = app_commands.Group(
        name="acm-admin",
        description="ACM Hub officer controls",
        default_permissions=discord.Permissions(manage_guild=True),
    )
    source = app_commands.Group(
        name="source", description="Inspect and configure collection sources", parent=admin
    )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Block non-officers before any command body runs."""
        if is_officer(interaction.user, self.bot.settings.officer_role_id):
            return True
        await interaction.response.send_message(
            "This command is limited to ACM officers.", ephemeral=True
        )
        return False

    # ---------------------------------------------------------------- review

    @admin.command(name="queue", description="List opportunities awaiting officer review")
    @app_commands.describe(
        type="Only show one kind of opportunity",
        sig="Only show opportunities matched to this SIG",
        organization="Only show opportunities from organizations matching this text",
        newest_first="Show the most recently discovered first instead of the oldest",
        page="Which page of the filtered queue to show",
    )
    @app_commands.choices(sig=SIG_CHOICES, type=TYPE_CHOICES)
    async def queue(
        self,
        interaction: discord.Interaction,
        type: app_commands.Choice[str] | None = None,
        sig: app_commands.Choice[str] | None = None,
        organization: str | None = None,
        newest_first: bool = False,
        page: app_commands.Range[int, 1, 100] = 1,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        opportunity_type = OpportunityType(type.value) if type else None
        offset = (page - 1) * QUEUE_PAGE_SIZE

        async with self.bot.database.session() as session:
            repository = OpportunityRepository(session)
            pending = await repository.list_pending_review(
                QUEUE_PAGE_SIZE,
                offset=offset,
                opportunity_type=opportunity_type,
                sig=sig.value if sig else None,
                organization=organization,
                newest_first=newest_first,
            )
            total = await repository.count_pending_review(
                opportunity_type=opportunity_type,
                sig=sig.value if sig else None,
                organization=organization,
            )

        if not pending:
            await interaction.followup.send(
                "No opportunities awaiting review match that filter."
                if total == 0
                else f"Page {page} is past the end; the queue has {total} matching entries.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"Review queue \u2014 {total} matching",
            description="\n\n".join(_queue_line(row) for row in pending),
            colour=discord.Colour(0x8C1D40),
        )
        pages = max(1, -(-total // QUEUE_PAGE_SIZE))
        embed.set_footer(
            text=(
                f"Page {page} of {pages} \u2022 use the buttons on the review cards, "
                "/acm-admin approve <id>, or /acm-admin bulk-approve"
            )
        )
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

    @admin.command(
        name="bulk-approve",
        description="Approve and publish everything matching a filter, after confirming",
    )
    @app_commands.describe(
        type="Only approve one kind of opportunity",
        sig="Only approve opportunities matched to this SIG",
        organization="Only approve opportunities from organizations matching this text",
        limit="Maximum number to approve in this batch",
    )
    @app_commands.choices(sig=SIG_CHOICES, type=TYPE_CHOICES)
    async def bulk_approve(
        self,
        interaction: discord.Interaction,
        type: app_commands.Choice[str] | None = None,
        sig: app_commands.Choice[str] | None = None,
        organization: str | None = None,
        limit: app_commands.Range[int, 1, MAX_BULK_APPROVE] = 10,
    ) -> None:
        """Preview the batch, then require a second press before publishing.

        The limit is capped rather than unbounded: publishing is a message per
        opportunity in a channel the whole server reads, and no filter mistake
        should be able to post hundreds of them.
        """
        await interaction.response.defer(ephemeral=True, thinking=True)

        opportunity_type = OpportunityType(type.value) if type else None
        async with self.bot.database.session() as session:
            candidates = await OpportunityRepository(session).list_pending_review(
                limit,
                opportunity_type=opportunity_type,
                sig=sig.value if sig else None,
                organization=organization,
            )
            # Detached from the session; only plain values cross into the view.
            preview = [(row.id, row.title, row.organization) for row in candidates]

        if not preview:
            await interaction.followup.send(
                "Nothing in the review queue matches that filter.", ephemeral=True
            )
            return

        identifiers = [identifier for identifier, _, _ in preview]
        reviewer_id = interaction.user.id

        async def run() -> str:
            result = await self.bot.publisher.approve_and_publish_many(
                identifiers, reviewer_discord_id=reviewer_id
            )
            return result.summary()

        listing = "\n".join(
            f"\u2022 **{truncate(title, 70)}** \u2014 {truncate(organization, 35)}"
            for _, title, organization in preview
        )
        view = ConfirmationView(reviewer_id, action=run, confirm_label=f"Publish {len(preview)}")
        message = await interaction.followup.send(
            f"This publishes **{len(preview)}** opportunit"
            f"{'y' if len(preview) == 1 else 'ies'} to the members' channel:\n{listing}",
            view=view,
            ephemeral=True,
            wait=True,
        )
        view.bind(message)

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

    @admin.command(name="rejected", description="List recently rejected opportunities")
    @app_commands.describe(page="Which page of rejected opportunities to show")
    async def rejected(
        self,
        interaction: discord.Interaction,
        page: app_commands.Range[int, 1, 100] = 1,
    ) -> None:
        """Where an officer finds the ID to hand to ``/acm-admin reconsider``."""
        await interaction.response.defer(ephemeral=True, thinking=True)

        async with self.bot.database.session() as session:
            rows = await OpportunityRepository(session).list_rejected(
                limit=QUEUE_PAGE_SIZE, offset=(page - 1) * QUEUE_PAGE_SIZE
            )

        if not rows:
            await interaction.followup.send(
                "No rejected opportunities on that page.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="Rejected opportunities",
            description="\n\n".join(_queue_line(row) for row in rows),
            colour=discord.Colour(0x6E6E6E),
        )
        embed.set_footer(text="Use /acm-admin reconsider <id> to put one back in the queue.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @admin.command(
        name="reconsider", description="Put a rejected opportunity back into the review queue"
    )
    @app_commands.describe(opportunity_id="The opportunity UUID shown in /acm-admin rejected")
    async def reconsider(self, interaction: discord.Interaction, opportunity_id: str) -> None:
        """Undo a rejection without anyone touching the database.

        The original rejection stays in the audit trail; this only makes the
        opportunity eligible for a fresh review card.
        """
        parsed = _parse_opportunity_id(opportunity_id)
        if parsed is None:
            await interaction.response.send_message(
                "That is not a valid opportunity ID.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.bot.database.session() as session:
            reopened = await OpportunityRepository(session).reopen_for_review(parsed)

        if not reopened:
            await interaction.followup.send(
                "That opportunity does not exist or is not currently rejected.", ephemeral=True
            )
            return

        logger.info(
            "review.reconsidered", opportunity_id=str(parsed), officer_id=interaction.user.id
        )
        await interaction.followup.send(
            "Back in the queue. A new review card is posted on the next cycle, "
            "or immediately with `/acm-admin requeue`.",
            ephemeral=True,
        )

    # --------------------------------------------------------------- sources

    @source.command(name="list", description="Show every source and its policy")
    async def source_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        async with self.bot.database.session() as session:
            sources = await SourceRepository(session).list_all()
            baselines = {
                row.name: await OpportunityRepository(session).count_baseline(row.name)
                for row in sources
            }

        if not sources:
            await interaction.followup.send(
                "No sources are registered yet. They register on their first run.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(title="Collection sources", colour=discord.Colour(0xFFC627))
        for row in sources:
            initialized = (
                f"initialized <t:{int(row.initialized_at.timestamp())}:R>"
                if row.initialized_at
                else "**not yet initialized** \u2014 next run imports as baseline"
            )
            held = baselines.get(row.name, 0)
            embed.add_field(
                name=row.display_name,
                value=(
                    f"`{row.name}`\n"
                    f"Policy: **{row.publication_policy.value.replace('_', ' ')}**\n"
                    f"{initialized}\n"
                    f"Baseline held back: {held}"
                ),
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @source.command(name="policy", description="Set what happens to a source's findings")
    @app_commands.describe(
        name="The source identifier from /acm-admin source list",
        policy="Manual review, auto-publish, or disabled",
    )
    @app_commands.choices(policy=POLICY_CHOICES)
    async def source_policy(
        self,
        interaction: discord.Interaction,
        name: str,
        policy: app_commands.Choice[str],
    ) -> None:
        """Change a source's trust level.

        Auto-publish is reachable only through this command, deliberately: it
        is never inferred, never defaulted to and never set by a deploy.
        """
        await interaction.response.defer(ephemeral=True, thinking=True)

        chosen = PublicationPolicy(policy.value)
        async with self.bot.database.session() as session:
            updated = await SourceRepository(session).set_policy(name.strip(), chosen)

        if not updated:
            await interaction.followup.send(
                f"No source called `{truncate(name, 64)}`. Check `/acm-admin source list`.",
                ephemeral=True,
            )
            return

        logger.info(
            "source.policy_changed",
            source=name.strip(),
            policy=chosen.value,
            officer_id=interaction.user.id,
        )
        note = {
            PublicationPolicy.AUTO_PUBLISH: (
                " New findings from this source now go straight to the members' channel "
                "with no officer review."
            ),
            PublicationPolicy.DISABLED: " This source will no longer be collected.",
            PublicationPolicy.MANUAL_REVIEW: " New findings go to the review queue.",
        }[chosen]
        await interaction.followup.send(
            f"`{truncate(name.strip(), 64)}` is now **{chosen.value.replace('_', ' ')}**.{note}",
            ephemeral=True,
        )

    @source.command(
        name="release-baseline",
        description="Move part of a source's initial import into the review queue",
    )
    @app_commands.describe(
        name="The source identifier from /acm-admin source list",
        count="How many of the newest baseline opportunities to release",
    )
    async def source_release_baseline(
        self,
        interaction: discord.Interaction,
        name: str,
        count: app_commands.Range[int, 1, 100] = 25,
    ) -> None:
        """Release backlog deliberately, in bounded batches.

        Newest first, because the oldest entries in a source's back catalogue
        are the least likely to still be open.
        """
        await interaction.response.defer(ephemeral=True, thinking=True)
        source_name = name.strip()

        async with self.bot.database.session() as session:
            held = await OpportunityRepository(session).count_baseline(source_name)

        if not held:
            await interaction.followup.send(
                f"`{truncate(source_name, 64)}` has no baseline opportunities held back.",
                ephemeral=True,
            )
            return

        async def run() -> str:
            async with self.bot.database.session() as session:
                released = await OpportunityRepository(session).release_baseline(
                    source_name, limit=count
                )
            logger.info(
                "source.baseline_released",
                source=source_name,
                released=released,
                officer_id=interaction.user.id,
            )
            return (
                f"Released {released} opportunit{'y' if released == 1 else 'ies'} into the "
                "review queue. Review cards are posted in batches on the next cycle."
            )

        view = ConfirmationView(
            interaction.user.id, action=run, confirm_label=f"Release {min(count, held)}"
        )
        message = await interaction.followup.send(
            f"`{truncate(source_name, 64)}` is holding **{held}** baseline opportunities. "
            f"Releasing up to **{count}** queues them for officer review.",
            view=view,
            ephemeral=True,
            wait=True,
        )
        view.bind(message)

    @source.command(
        name="reset-initialization",
        description="Treat a source as never initialized, so its next run re-imports as baseline",
    )
    @app_commands.describe(name="The source identifier from /acm-admin source list")
    async def source_reset_initialization(
        self, interaction: discord.Interaction, name: str
    ) -> None:
        """Recover from an initial import that ran against a broken feed."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        source_name = name.strip()

        async def run() -> str:
            async with self.bot.database.session() as session:
                cleared = await SourceRepository(session).clear_initialization(source_name)
            if not cleared:
                return f"No source called `{truncate(source_name, 64)}`."
            logger.warning(
                "source.initialization_cleared",
                source=source_name,
                officer_id=interaction.user.id,
            )
            return (
                f"`{truncate(source_name, 64)}` is marked uninitialized. Its next successful "
                "run imports everything new as baseline instead of queueing it for review."
            )

        view = ConfirmationView(
            interaction.user.id, action=run, confirm_label="Reset initialization"
        )
        message = await interaction.followup.send(
            f"This makes the next run of `{truncate(source_name, 64)}` an initial import: "
            "anything it discovers is stored as baseline rather than queued for review. "
            "Already-known opportunities are unaffected.",
            view=view,
            ephemeral=True,
            wait=True,
        )
        view.bind(message)

    # ------------------------------------------------------------ operations

    @admin.command(name="sync", description="Run every collector now and refresh the review queue")
    async def sync(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        results = await self.bot.scheduler.run_all_collectors()

        embed = discord.Embed(title="Collection run", colour=discord.Colour(0xFFC627))
        for stats in results:
            if stats.skipped:
                value = "skipped \u2014 the source's policy is `disabled`"
            elif stats.error:
                value = f"error: {truncate(stats.error, 200)}"
            else:
                value = (
                    f"fetched {stats.fetched} \u00b7 new {stats.new} \u00b7 "
                    f"updated {stats.updated} \u00b7 duplicates {stats.duplicates} \u00b7 "
                    f"rejected {stats.rejected} \u00b7 closed {stats.closed} \u00b7 "
                    f"{stats.duration_seconds:.1f}s"
                )
                if stats.baseline_import:
                    value += "\n\u26a0\ufe0f initial import \u2014 held back as baseline"
                if stats.closure_skipped_reason:
                    value += (
                        f"\nClosure sweep skipped: {truncate(stats.closure_skipped_reason, 150)}"
                    )
            embed.add_field(name=stats.collector, value=value, inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @admin.command(name="health", description="Check database, scheduler and collector health")
    async def health(self, interaction: discord.Interaction) -> None:
        """Operational status.

        Reports only facts an officer can act on. Connection strings, tokens
        and exception tracebacks stay out of Discord and go to the logs; a
        failure is surfaced as its type and a pointer to the logs.
        """
        await interaction.response.defer(ephemeral=True, thinking=True)

        database_ok = await self.bot.database.check_connection()

        # Querying a database we already know is unreachable would raise and
        # turn the health command into another outage, so the counts are simply
        # omitted when the connection check fails.
        runs: list[models.CollectorRun] = []
        counts: dict[OpportunityStatus, int] = {}
        sources: list[models.Source] = []
        subscribers = 0
        if database_ok:
            async with self.bot.database.session() as session:
                runs = await CollectorRunRepository(session).latest_per_collector()
                counts = await OpportunityRepository(session).count_by_status()
                sources = await SourceRepository(session).list_all()
                subscribers = await SubscriptionRepository(session).count()

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
            name="Sources",
            value="\n".join(
                f"**{row.name}**: {row.publication_policy.value.replace('_', ' ')}"
                + ("" if row.initialized_at else " \u2014 awaiting initial import")
                for row in sources
            )
            or "none registered",
            inline=False,
        )
        embed.add_field(
            name="Last collector runs",
            value="\n".join(self._run_line(run) for run in sorted(runs, key=lambda r: r.collector))
            or "no runs recorded",
            inline=False,
        )
        embed.add_field(name="Members with saved interests", value=str(subscribers), inline=True)
        embed.set_footer(text="Failure detail is in the bot logs, not here.")

        await interaction.followup.send(embed=embed, ephemeral=True)

    @staticmethod
    def _run_line(run: models.CollectorRun) -> str:
        """One collector's last outcome, with no exception text.

        ``run.error`` can contain an upstream URL or a driver message. Only the
        status and counts are shown; the full error is already logged.
        """
        line = f"**{run.collector}**: {run.status.value}"
        if run.started_at:
            line += f" <t:{int(run.started_at.timestamp())}:R>"
        if run.status.value == "success":
            line += (
                f"\nfetched {run.records_fetched} \u00b7 new {run.records_new} \u00b7 "
                f"closed {run.records_closed}"
            )
        else:
            line += "\nsee the bot logs for the failure detail"
        return line

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
