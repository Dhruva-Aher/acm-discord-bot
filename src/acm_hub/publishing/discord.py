"""Sending opportunities to Discord.

This is the only module that both reads the database and writes to Discord, and
the ordering inside it is deliberate.

When publishing, the publication record is inserted **before** the message is
sent. The insert is what claims the right to publish - it either succeeds (this
caller owns the publish) or hits the unique constraint (someone else already
owns it). Sending first and recording afterwards would leave a window where two
approvals both send a message and only then discover the conflict, which is
exactly the double-post this design prevents. If the send then fails, the claim
is released so a later retry can try again.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

import discord

from acm_hub.bot.embeds.opportunity import (
    build_opportunity_embed,
    build_review_embed,
    mark_embed_closed,
)
from acm_hub.db import models
from acm_hub.db.repositories.opportunities import OpportunityRepository
from acm_hub.db.session import Database
from acm_hub.domain.enums import OpportunityStatus, ReviewDecision
from acm_hub.observability.logging import get_logger

logger = get_logger(__name__)


class ChannelUnavailableError(RuntimeError):
    """A configured channel ID does not resolve to a channel we can post in."""


@dataclass(slots=True)
class BulkApprovalResult:
    """What a bulk approval actually managed to do.

    Reported in full rather than as a single success/failure, because the
    realistic outcome of approving twenty postings at once is that most work
    and one hits a rate limit. An officer needs to know which.
    """

    published: list[UUID] = field(default_factory=list)
    #: Approved by someone else, or already published, between the officer
    #: opening the confirmation and confirming it.
    already_decided: list[UUID] = field(default_factory=list)
    #: Approved in the database but not posted; the publish pass will retry.
    failed: list[tuple[UUID, str]] = field(default_factory=list)

    @property
    def attempted(self) -> int:
        return len(self.published) + len(self.already_decided) + len(self.failed)

    def summary(self) -> str:
        """A short report an officer can act on."""
        if not self.attempted:
            return "Nothing matched, so nothing was approved."

        lines = [f"Published {len(self.published)} of {self.attempted}."]
        if self.already_decided:
            lines.append(
                f"{len(self.already_decided)} had already been decided by another officer."
            )
        if self.failed:
            lines.append(
                f"{len(self.failed)} could not be posted and stay approved for the next "
                "publish pass:"
            )
            lines.extend(
                f"\u2022 `{identifier}` \u2014 {reason}" for identifier, reason in self.failed[:5]
            )
            if len(self.failed) > 5:
                lines.append(f"\u2022 \u2026and {len(self.failed) - 5} more; see the logs.")
        return "\n".join(lines)


class OpportunityPublisher:
    """Posts review cards to the officer channel and approved posts to members."""

    def __init__(
        self,
        client: discord.Client,
        database: Database,
        *,
        opportunities_channel_id: int,
        officer_review_channel_id: int,
        source_display_names: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self._database = database
        self._opportunities_channel_id = opportunities_channel_id
        self._officer_review_channel_id = officer_review_channel_id
        self._source_display_names = source_display_names or {}

    async def _resolve_channel(self, channel_id: int) -> discord.abc.Messageable:
        """Look up a channel, falling back to an API fetch when uncached.

        Raises:
            ChannelUnavailableError: when the ID is wrong, the bot cannot see
                the channel, or the channel cannot receive messages. Raised
                rather than swallowed so a misconfiguration is visible instead
                of silently dropping every post.
        """
        channel = self._client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                raise ChannelUnavailableError(
                    f"channel {channel_id} could not be fetched: {type(exc).__name__}: {exc}"
                ) from exc
        if not isinstance(channel, discord.abc.Messageable):
            raise ChannelUnavailableError(
                f"channel {channel_id} is a {type(channel).__name__} and cannot receive messages"
            )
        return channel

    def _display_name(self, row: models.Opportunity) -> str:
        return self._source_display_names.get(row.source_name, row.source_name)

    async def queue_pending_reviews(self, limit: int) -> int:
        """Post review cards for newly discovered opportunities.

        Import is local to avoid a circular import: the approval view needs the
        publisher in order to publish on approve.

        Returns:
            How many review cards were posted.
        """
        from acm_hub.publishing.approval import build_review_view

        async with self._database.session() as session:
            repository = OpportunityRepository(session)
            pending = await repository.list_awaiting_queue(limit)

        if not pending:
            return 0

        channel = await self._resolve_channel(self._officer_review_channel_id)
        posted = 0

        for row in pending:
            try:
                message = await channel.send(
                    embed=build_review_embed(row, source_display_name=self._display_name(row)),
                    view=build_review_view(row.id, row.source_url),
                )
            except discord.HTTPException as exc:
                # Rate limits and transient 5xx are retried on the next cycle:
                # the row keeps status DISCOVERED with no review_message_id.
                logger.error(
                    "review.post_failed",
                    opportunity_id=str(row.id),
                    channel_id=self._officer_review_channel_id,
                    error=f"{type(exc).__name__}: {exc}",
                    exc_info=True,
                )
                break

            async with self._database.session() as session:
                transitioned = await OpportunityRepository(session).mark_queued_for_review(
                    row.id,
                    channel_id=self._officer_review_channel_id,
                    message_id=message.id,
                )
            if transitioned:
                posted += 1
            else:
                logger.warning(
                    "review.transition_lost_race",
                    opportunity_id=str(row.id),
                    message_id=message.id,
                )

        logger.info("review.queued", count=posted, requested=len(pending))
        return posted

    async def publish_approved(self, opportunity_id: UUID) -> models.Opportunity | None:
        """Publish an approved opportunity to the members' channel.

        Returns:
            The published row, or ``None`` when another approval already
            published it (in which case nothing is sent).

        Raises:
            ChannelUnavailableError: the opportunities channel is misconfigured.
            discord.HTTPException: the send failed; the publication claim has
                been released so a later retry can publish.
        """
        async with self._database.session() as session:
            repository = OpportunityRepository(session)
            row = await repository.get(opportunity_id)
            if row is None:
                logger.warning("publish.missing_opportunity", opportunity_id=str(opportunity_id))
                return None
            claimed = await repository.record_publication(
                row.id, channel_id=self._opportunities_channel_id, message_id=None
            )
            embed = build_opportunity_embed(row, source_display_name=self._display_name(row))

        if not claimed:
            logger.info("publish.already_published", opportunity_id=str(opportunity_id))
            return None

        try:
            channel = await self._resolve_channel(self._opportunities_channel_id)
            message = await channel.send(embed=embed)
        except (discord.HTTPException, ChannelUnavailableError):
            await self._release_claim(row.id)
            logger.error(
                "publish.send_failed",
                opportunity_id=str(row.id),
                channel_id=self._opportunities_channel_id,
                exc_info=True,
            )
            raise

        async with self._database.session() as session:
            await OpportunityRepository(session).set_publication_message(
                row.id, channel_id=self._opportunities_channel_id, message_id=message.id
            )

        logger.info(
            "publish.sent",
            opportunity_id=str(row.id),
            channel_id=self._opportunities_channel_id,
            message_id=message.id,
        )
        return row

    async def publish_pending_approved(self, limit: int) -> int:
        """Publish opportunities already approved but not yet posted.

        Serves auto-published sources and retries publishes that previously
        failed. A send failure stops the pass rather than hammering a channel
        that is rate-limited or gone; the rows stay ``APPROVED`` and the next
        cycle picks them up.

        Returns:
            How many opportunities were posted.
        """
        async with self._database.session() as session:
            pending = await OpportunityRepository(session).list_awaiting_publication(limit)

        published = 0
        for row in pending:
            try:
                if await self.publish_approved(row.id) is not None:
                    published += 1
            except (discord.HTTPException, ChannelUnavailableError) as exc:
                logger.error(
                    "publish.batch_stopped",
                    opportunity_id=str(row.id),
                    error=f"{type(exc).__name__}: {exc}",
                    exc_info=True,
                )
                break

        if published:
            logger.info("publish.batch_finished", published=published, candidates=len(pending))
        return published

    async def approve_and_publish_many(
        self, opportunity_ids: Sequence[UUID], *, reviewer_discord_id: int
    ) -> BulkApprovalResult:
        """Approve and publish several opportunities, reporting each outcome.

        One posting failing does not abandon the rest, and every approval is
        still recorded individually in ``review_actions`` with the officer who
        made it, so a bulk action is as auditable as twenty button presses.
        """
        result = BulkApprovalResult()

        for opportunity_id in opportunity_ids:
            async with self._database.session() as session:
                accepted = await OpportunityRepository(session).record_decision(
                    opportunity_id,
                    decision=ReviewDecision.APPROVED,
                    reviewer_discord_id=reviewer_discord_id,
                    reason="bulk approval",
                )
            if not accepted:
                result.already_decided.append(opportunity_id)
                continue

            try:
                published = await self.publish_approved(opportunity_id)
            except (discord.HTTPException, ChannelUnavailableError) as exc:
                logger.error(
                    "publish.bulk_item_failed",
                    opportunity_id=str(opportunity_id),
                    error=f"{type(exc).__name__}: {exc}",
                    exc_info=True,
                )
                result.failed.append((opportunity_id, type(exc).__name__))
                continue

            if published is None:
                result.already_decided.append(opportunity_id)
            else:
                result.published.append(opportunity_id)

        logger.info(
            "publish.bulk_finished",
            reviewer_id=reviewer_discord_id,
            published=len(result.published),
            already_decided=len(result.already_decided),
            failed=len(result.failed),
        )
        return result

    async def mark_published_messages_closed(self, opportunity_ids: Sequence[UUID]) -> int:
        """Edit the members' posts for opportunities that have closed.

        Editing in place is the least noisy option available and needs nothing
        fragile: the message ID was persisted when we published, so this is a
        lookup rather than a channel scan. Nothing new is posted, so a sweep
        that closes fifty postings produces zero new Discord messages.

        A message an officer already deleted is treated as done rather than as
        an error - the point was that members stop seeing an open posting.

        Returns:
            How many messages were successfully edited.
        """
        edited = 0
        for opportunity_id in opportunity_ids:
            async with self._database.session() as session:
                repository = OpportunityRepository(session)
                row = await repository.get(opportunity_id)
                targets = await repository.publication_messages(opportunity_id)
                embed = (
                    mark_embed_closed(
                        build_opportunity_embed(row, source_display_name=self._display_name(row))
                    )
                    if row is not None
                    else None
                )

            if embed is None:
                continue

            for channel_id, message_id in targets:
                if await self._edit_message(channel_id, message_id, embed):
                    edited += 1

        if edited:
            logger.info("closure.messages_edited", edited=edited)
        return edited

    async def _edit_message(self, channel_id: int, message_id: int, embed: discord.Embed) -> bool:
        """Replace one published message's embed, tolerating its absence."""
        try:
            channel = await self._resolve_channel(channel_id)
        except ChannelUnavailableError as exc:
            logger.warning("closure.channel_unavailable", channel_id=channel_id, error=str(exc))
            return False

        try:
            message = await channel.fetch_message(message_id)
            await message.edit(embed=embed)
        except discord.NotFound:
            logger.info("closure.message_gone", channel_id=channel_id, message_id=message_id)
            return False
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning(
                "closure.edit_failed",
                channel_id=channel_id,
                message_id=message_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return False
        return True

    async def _release_claim(self, opportunity_id: UUID) -> None:
        """Roll the opportunity back to ``approved`` so a retry can publish it."""
        async with self._database.session() as session:
            await OpportunityRepository(session).release_publication(
                opportunity_id,
                channel_id=self._opportunities_channel_id,
                revert_status=OpportunityStatus.APPROVED,
            )
