"""Officer review and publication.

The properties under test are the ones that would embarrass ACM in a live
server: an opportunity must never be published twice, a decision must never be
applied twice, and a rejected opportunity must never come back next cycle.

Discord itself is replaced with mocks specced against the real classes, so
``isinstance`` checks in the publisher behave as they do in production while no
network call is made.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import discord
import pytest

from acm_hub.db import models
from acm_hub.db.repositories.opportunities import OpportunityRepository
from acm_hub.db.session import Database
from acm_hub.domain.enums import OpportunityStatus, OpportunityType, ReviewDecision
from acm_hub.pipeline.classify import classify_opportunity
from acm_hub.pipeline.normalize import normalize_opportunity
from acm_hub.pipeline.process import OpportunityPipeline
from acm_hub.publishing.discord import ChannelUnavailableError, OpportunityPublisher
from tests.conftest import FROZEN_NOW, initialize_source, make_raw
from tests.integration.test_pipeline import StubCollector

OPPORTUNITIES_CHANNEL_ID = 111111111111111111
REVIEW_CHANNEL_ID = 222222222222222222
RESEARCH_CHANNEL_ID = 333333333333333333
OFFICER_ONE = 555555555555555555
OFFICER_TWO = 666666666666666666


def fake_channel(channel_id: int, *, message_id_start: int = 900000000000000000) -> MagicMock:
    """A Discord text channel stand-in that hands out increasing message IDs."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    counter = iter(range(message_id_start, message_id_start + 1000))
    posted: dict[int, MagicMock] = {}

    async def send(*_args: object, **_kwargs: object) -> MagicMock:
        message = MagicMock(spec=discord.Message)
        message.id = next(counter)
        message.edit = AsyncMock()
        posted[message.id] = message
        return message

    async def fetch_message(message_id: int) -> MagicMock:
        if message_id in posted:
            return posted[message_id]
        raise discord.NotFound(MagicMock(), "unknown message")

    channel.send = AsyncMock(side_effect=send)
    channel.fetch_message = AsyncMock(side_effect=fetch_message)
    channel._posted = posted  # noqa: SLF001 - test helper, not production access
    return channel


def fake_client(channels: dict[int, MagicMock]) -> MagicMock:
    client = MagicMock(spec=discord.Client)
    client.get_channel = MagicMock(side_effect=lambda channel_id: channels.get(channel_id))
    client.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(), "not found"))
    return client


@pytest.fixture
def channels() -> dict[int, MagicMock]:
    return {
        OPPORTUNITIES_CHANNEL_ID: fake_channel(OPPORTUNITIES_CHANNEL_ID),
        REVIEW_CHANNEL_ID: fake_channel(REVIEW_CHANNEL_ID, message_id_start=800000000000000000),
    }


@pytest.fixture
def publisher(database: Database, channels: dict[int, MagicMock]) -> OpportunityPublisher:
    return OpportunityPublisher(
        fake_client(channels),
        database,
        opportunities_channel_id=OPPORTUNITIES_CHANNEL_ID,
        officer_review_channel_id=REVIEW_CHANNEL_ID,
        source_display_names={"stub": "Stub Source"},
    )


async def seed(database: Database, **kwargs: object) -> models.Opportunity:
    """Store one classified opportunity and return the stored row."""
    opportunity = classify_opportunity(
        normalize_opportunity(make_raw(**kwargs), discovered_at=FROZEN_NOW)  # type: ignore[arg-type]
    )
    async with database.session() as session:
        row, _ = await OpportunityRepository(session).upsert(opportunity)
        return row


class TestReviewQueue:
    async def test_discovered_opportunities_are_posted_for_review(
        self, database: Database, publisher: OpportunityPublisher, channels: dict[int, MagicMock]
    ) -> None:
        await seed(database)
        posted = await publisher.queue_pending_reviews(10)

        assert posted == 1
        assert channels[REVIEW_CHANNEL_ID].send.await_count == 1

    async def test_queued_opportunities_move_to_pending_review(
        self, database: Database, publisher: OpportunityPublisher
    ) -> None:
        row = await seed(database)
        await publisher.queue_pending_reviews(10)

        async with database.session() as session:
            refreshed = await OpportunityRepository(session).get(row.id)
        assert refreshed is not None
        assert refreshed.status is OpportunityStatus.PENDING_REVIEW
        assert refreshed.review_channel_id == REVIEW_CHANNEL_ID
        assert refreshed.review_message_id is not None

    async def test_an_opportunity_is_not_queued_twice(
        self, database: Database, publisher: OpportunityPublisher, channels: dict[int, MagicMock]
    ) -> None:
        await seed(database)
        await publisher.queue_pending_reviews(10)
        await publisher.queue_pending_reviews(10)

        assert channels[REVIEW_CHANNEL_ID].send.await_count == 1

    async def test_the_batch_limit_caps_a_first_run_flood(
        self, database: Database, publisher: OpportunityPublisher, channels: dict[int, MagicMock]
    ) -> None:
        for index in range(12):
            await seed(database, application_url=f"https://example.com/{index}")

        assert await publisher.queue_pending_reviews(5) == 5
        assert channels[REVIEW_CHANNEL_ID].send.await_count == 5

    async def test_a_misconfigured_review_channel_raises_a_clear_error(
        self, database: Database
    ) -> None:
        await seed(database)
        publisher = OpportunityPublisher(
            fake_client({}),
            database,
            opportunities_channel_id=OPPORTUNITIES_CHANNEL_ID,
            officer_review_channel_id=999,
        )
        with pytest.raises(ChannelUnavailableError):
            await publisher.queue_pending_reviews(10)

    async def test_a_discord_failure_leaves_the_row_requeueable(
        self, database: Database, publisher: OpportunityPublisher, channels: dict[int, MagicMock]
    ) -> None:
        """A rate limit must not silently lose the opportunity."""
        row = await seed(database)
        channels[REVIEW_CHANNEL_ID].send = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=429), "rate limited")
        )

        assert await publisher.queue_pending_reviews(10) == 0

        async with database.session() as session:
            refreshed = await OpportunityRepository(session).get(row.id)
        assert refreshed is not None
        assert refreshed.status is OpportunityStatus.DISCOVERED
        assert refreshed.review_message_id is None


class TestDecisionIdempotency:
    async def test_the_first_approval_wins(self, database: Database) -> None:
        row = await seed(database)
        async with database.session() as session:
            assert await OpportunityRepository(session).record_decision(
                row.id, decision=ReviewDecision.APPROVED, reviewer_discord_id=OFFICER_ONE
            )

    async def test_a_second_approval_is_refused(self, database: Database) -> None:
        """Two officers pressing Approve must produce one decision."""
        row = await seed(database)
        async with database.session() as session:
            repository = OpportunityRepository(session)
            first = await repository.record_decision(
                row.id, decision=ReviewDecision.APPROVED, reviewer_discord_id=OFFICER_ONE
            )
            second = await repository.record_decision(
                row.id, decision=ReviewDecision.APPROVED, reviewer_discord_id=OFFICER_TWO
            )
        assert first is True
        assert second is False

    async def test_only_one_audit_record_is_written(self, database: Database) -> None:
        row = await seed(database)
        async with database.session() as session:
            repository = OpportunityRepository(session)
            await repository.record_decision(
                row.id, decision=ReviewDecision.APPROVED, reviewer_discord_id=OFFICER_ONE
            )
            await repository.record_decision(
                row.id, decision=ReviewDecision.REJECTED, reviewer_discord_id=OFFICER_TWO
            )

        async with database.session() as session:
            refreshed = await OpportunityRepository(session).get(row.id)
        assert refreshed is not None
        assert len(refreshed.reviews) == 1
        assert refreshed.status is OpportunityStatus.APPROVED

    async def test_rejection_is_recorded_with_reviewer_and_reason(self, database: Database) -> None:
        row = await seed(database)
        async with database.session() as session:
            await OpportunityRepository(session).record_decision(
                row.id,
                decision=ReviewDecision.REJECTED,
                reviewer_discord_id=OFFICER_ONE,
                reason="not relevant to ACM members",
            )

        async with database.session() as session:
            refreshed = await OpportunityRepository(session).get(row.id)
        assert refreshed is not None
        review = refreshed.reviews[0]
        assert review.reviewer_discord_id == OFFICER_ONE
        assert review.reason == "not relevant to ACM members"
        assert review.created_at is not None

    async def test_a_decision_on_a_missing_opportunity_is_refused(self, database: Database) -> None:
        async with database.session() as session:
            accepted = await OpportunityRepository(session).record_decision(
                uuid4(), decision=ReviewDecision.APPROVED, reviewer_discord_id=OFFICER_ONE
            )
        assert accepted is False


class TestRejectedOpportunitiesStayRejected:
    async def test_rediscovery_does_not_requeue_a_rejected_opportunity(
        self, database: Database, publisher: OpportunityPublisher, channels: dict[int, MagicMock]
    ) -> None:
        """The behaviour officers actually notice: rejects do not come back."""
        await initialize_source(database, "stub")
        records = [make_raw(application_url="https://example.com/a")]
        pipeline = OpportunityPipeline(database)
        await pipeline.run(StubCollector(records))
        await publisher.queue_pending_reviews(10)

        async with database.session() as session:
            row = (await OpportunityRepository(session).list_pending_review())[0]
            await OpportunityRepository(session).record_decision(
                row.id, decision=ReviewDecision.REJECTED, reviewer_discord_id=OFFICER_ONE
            )

        sends_before = channels[REVIEW_CHANNEL_ID].send.await_count
        await pipeline.run(StubCollector(records))
        await publisher.queue_pending_reviews(10)

        assert channels[REVIEW_CHANNEL_ID].send.await_count == sends_before

    async def test_a_rejected_opportunity_that_changes_upstream_stays_rejected(
        self, database: Database
    ) -> None:
        await initialize_source(database, "stub")
        pipeline = OpportunityPipeline(database)
        await pipeline.run(StubCollector([make_raw(application_url="https://example.com/a")]))

        async with database.session() as session:
            row = (await OpportunityRepository(session).list_awaiting_queue(1))[0]
            await OpportunityRepository(session).record_decision(
                row.id, decision=ReviewDecision.REJECTED, reviewer_discord_id=OFFICER_ONE
            )

        await pipeline.run(
            StubCollector(
                [make_raw(application_url="https://example.com/a", title="Updated Title")]
            )
        )

        async with database.session() as session:
            refreshed = await OpportunityRepository(session).get(row.id)
        assert refreshed is not None
        assert refreshed.status is OpportunityStatus.REJECTED
        assert refreshed.title == "Updated Title"


class TestPublicationIdempotency:
    async def test_approval_publishes_once(
        self, database: Database, publisher: OpportunityPublisher, channels: dict[int, MagicMock]
    ) -> None:
        row = await seed(database)
        async with database.session() as session:
            await OpportunityRepository(session).record_decision(
                row.id, decision=ReviewDecision.APPROVED, reviewer_discord_id=OFFICER_ONE
            )

        published = await publisher.publish_approved(row.id)

        assert published is not None
        assert channels[OPPORTUNITIES_CHANNEL_ID].send.await_count == 1

    async def test_publishing_twice_sends_one_message(
        self, database: Database, publisher: OpportunityPublisher, channels: dict[int, MagicMock]
    ) -> None:
        row = await seed(database)
        assert await publisher.publish_approved(row.id) is not None
        assert await publisher.publish_approved(row.id) is None
        assert channels[OPPORTUNITIES_CHANNEL_ID].send.await_count == 1

    async def test_concurrent_publishes_send_one_message(
        self, database: Database, publisher: OpportunityPublisher, channels: dict[int, MagicMock]
    ) -> None:
        """Two officers approving simultaneously must not double-post."""
        row = await seed(database)

        results = await asyncio.gather(
            publisher.publish_approved(row.id),
            publisher.publish_approved(row.id),
            return_exceptions=True,
        )

        successes = [item for item in results if isinstance(item, models.Opportunity)]
        assert len(successes) == 1
        assert channels[OPPORTUNITIES_CHANNEL_ID].send.await_count == 1

    async def test_the_publication_record_stores_the_message_id(
        self, database: Database, publisher: OpportunityPublisher
    ) -> None:
        row = await seed(database)
        await publisher.publish_approved(row.id)

        async with database.session() as session:
            refreshed = await OpportunityRepository(session).get(row.id)
        assert refreshed is not None
        assert refreshed.status is OpportunityStatus.PUBLISHED
        assert len(refreshed.publications) == 1
        assert refreshed.publications[0].message_id is not None
        assert refreshed.publications[0].channel_id == OPPORTUNITIES_CHANNEL_ID

    async def test_a_failed_send_releases_the_claim_for_a_retry(
        self, database: Database, publisher: OpportunityPublisher, channels: dict[int, MagicMock]
    ) -> None:
        """A transient Discord error must not permanently mark it published."""
        row = await seed(database)
        async with database.session() as session:
            await OpportunityRepository(session).record_decision(
                row.id, decision=ReviewDecision.APPROVED, reviewer_discord_id=OFFICER_ONE
            )
        channels[OPPORTUNITIES_CHANNEL_ID].send = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=500), "server error")
        )

        with pytest.raises(discord.HTTPException):
            await publisher.publish_approved(row.id)

        async with database.session() as session:
            refreshed = await OpportunityRepository(session).get(row.id)
        assert refreshed is not None
        assert refreshed.status is OpportunityStatus.APPROVED
        assert refreshed.publications == []

        # And the retry, once Discord recovers, succeeds.
        channels[OPPORTUNITIES_CHANNEL_ID] = fake_channel(OPPORTUNITIES_CHANNEL_ID)
        assert await publisher.publish_approved(row.id) is not None

    async def test_publishing_a_missing_opportunity_is_a_no_op(
        self, publisher: OpportunityPublisher, channels: dict[int, MagicMock]
    ) -> None:
        assert await publisher.publish_approved(uuid4()) is None
        assert channels[OPPORTUNITIES_CHANNEL_ID].send.await_count == 0

    async def test_the_unique_constraint_is_enforced_by_the_database(
        self, database: Database
    ) -> None:
        """Proves the guarantee is not just application logic."""
        from sqlalchemy.exc import IntegrityError

        row = await seed(database)
        async with database.session() as session:
            session.add(
                models.PublicationRecord(opportunity_id=row.id, channel_id=OPPORTUNITIES_CHANNEL_ID)
            )

        with pytest.raises(IntegrityError):
            async with database.session() as session:
                session.add(
                    models.PublicationRecord(
                        opportunity_id=row.id, channel_id=OPPORTUNITIES_CHANNEL_ID
                    )
                )

    async def test_the_same_opportunity_may_be_published_to_a_different_channel(
        self, database: Database
    ) -> None:
        """The constraint is per channel, leaving room for future SIG channels."""
        row = await seed(database)
        async with database.session() as session:
            repository = OpportunityRepository(session)
            assert await repository.record_publication(
                row.id, channel_id=OPPORTUNITIES_CHANNEL_ID, message_id=1
            )
            assert await repository.record_publication(row.id, channel_id=333, message_id=2)


class TestClosedMessageEditing:
    async def test_a_published_message_is_edited_in_place_and_nothing_new_is_posted(
        self, database: Database, publisher: OpportunityPublisher, channels: dict[int, MagicMock]
    ) -> None:
        row = await seed(database)
        async with database.session() as session:
            await OpportunityRepository(session).record_decision(
                row.id, decision=ReviewDecision.APPROVED, reviewer_discord_id=OFFICER_ONE
            )
        await publisher.publish_approved(row.id)
        sends_before = channels[OPPORTUNITIES_CHANNEL_ID].send.await_count

        async with database.session() as session:
            closed = await OpportunityRepository(session).mark_closed([row.id])
        assert closed

        edited = await publisher.mark_published_messages_closed([row.id])

        assert edited == 1
        assert channels[OPPORTUNITIES_CHANNEL_ID].send.await_count == sends_before
        message = next(iter(channels[OPPORTUNITIES_CHANNEL_ID]._posted.values()))  # noqa: SLF001
        message.edit.assert_awaited_once()
        embed = message.edit.await_args.kwargs["embed"]
        assert embed.title is not None and embed.title.startswith("[CLOSED]")

    async def test_a_deleted_message_is_treated_as_done_not_as_a_reason_to_repost(
        self, database: Database, publisher: OpportunityPublisher, channels: dict[int, MagicMock]
    ) -> None:
        row = await seed(database)
        async with database.session() as session:
            await OpportunityRepository(session).record_decision(
                row.id, decision=ReviewDecision.APPROVED, reviewer_discord_id=OFFICER_ONE
            )
        await publisher.publish_approved(row.id)
        channels[OPPORTUNITIES_CHANNEL_ID]._posted.clear()  # noqa: SLF001
        sends_before = channels[OPPORTUNITIES_CHANNEL_ID].send.await_count

        edited = await publisher.mark_published_messages_closed([row.id])

        assert edited == 0
        assert channels[OPPORTUNITIES_CHANNEL_ID].send.await_count == sends_before

    async def test_an_unsent_claim_is_skipped_rather_than_guessed_at(
        self, database: Database, publisher: OpportunityPublisher
    ) -> None:
        row = await seed(database)
        async with database.session() as session:
            await OpportunityRepository(session).record_publication(
                row.id, channel_id=OPPORTUNITIES_CHANNEL_ID, message_id=None
            )

        assert await publisher.mark_published_messages_closed([row.id]) == 0


class TestResearchChannelRouting:
    async def test_research_posts_to_the_research_channel_when_configured(
        self, database: Database
    ) -> None:
        channels = {
            OPPORTUNITIES_CHANNEL_ID: fake_channel(OPPORTUNITIES_CHANNEL_ID),
            REVIEW_CHANNEL_ID: fake_channel(REVIEW_CHANNEL_ID),
            RESEARCH_CHANNEL_ID: fake_channel(RESEARCH_CHANNEL_ID),
        }
        publisher = OpportunityPublisher(
            fake_client(channels),
            database,
            opportunities_channel_id=OPPORTUNITIES_CHANNEL_ID,
            officer_review_channel_id=REVIEW_CHANNEL_ID,
            research_channel_id=RESEARCH_CHANNEL_ID,
        )
        row = await seed(
            database,
            title="Ready to mentor: Test Faculty",
            organization="Test Faculty",
            opportunity_type=OpportunityType.RESEARCH,
            application_url="https://forge.engineering.asu.edu/faculty_mentor/test/",
        )

        published = await publisher.publish_approved(row.id)

        assert published is not None
        assert channels[RESEARCH_CHANNEL_ID].send.await_count == 1
        assert channels[OPPORTUNITIES_CHANNEL_ID].send.await_count == 0

    async def test_research_falls_back_to_opportunities_without_a_research_channel(
        self, database: Database, publisher: OpportunityPublisher, channels: dict[int, MagicMock]
    ) -> None:
        row = await seed(
            database,
            title="Ready to mentor: Test Faculty",
            organization="Test Faculty",
            opportunity_type=OpportunityType.RESEARCH,
            application_url="https://forge.engineering.asu.edu/faculty_mentor/test-2/",
        )

        published = await publisher.publish_approved(row.id)

        assert published is not None
        assert channels[OPPORTUNITIES_CHANNEL_ID].send.await_count == 1

    async def test_already_published_research_is_not_reposted_to_a_new_channel(
        self, database: Database
    ) -> None:
        """Adding RESEARCH_CHANNEL_ID later must not create a second member post."""
        opportunities = fake_channel(OPPORTUNITIES_CHANNEL_ID)
        research = fake_channel(RESEARCH_CHANNEL_ID)
        first = OpportunityPublisher(
            fake_client(
                {
                    OPPORTUNITIES_CHANNEL_ID: opportunities,
                    REVIEW_CHANNEL_ID: fake_channel(REVIEW_CHANNEL_ID),
                }
            ),
            database,
            opportunities_channel_id=OPPORTUNITIES_CHANNEL_ID,
            officer_review_channel_id=REVIEW_CHANNEL_ID,
        )
        row = await seed(
            database,
            title="Ready to mentor: Test Faculty",
            organization="Test Faculty",
            opportunity_type=OpportunityType.RESEARCH,
            application_url="https://forge.engineering.asu.edu/faculty_mentor/test-3/",
        )
        assert await first.publish_approved(row.id) is not None

        second = OpportunityPublisher(
            fake_client(
                {
                    OPPORTUNITIES_CHANNEL_ID: opportunities,
                    REVIEW_CHANNEL_ID: fake_channel(REVIEW_CHANNEL_ID),
                    RESEARCH_CHANNEL_ID: research,
                }
            ),
            database,
            opportunities_channel_id=OPPORTUNITIES_CHANNEL_ID,
            officer_review_channel_id=REVIEW_CHANNEL_ID,
            research_channel_id=RESEARCH_CHANNEL_ID,
        )
        assert await second.publish_approved(row.id) is None
        assert research.send.await_count == 0
        assert opportunities.send.await_count == 1
