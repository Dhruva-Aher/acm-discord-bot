"""Officer review-queue filtering, bulk approval and reconsideration.

These are the workflows an officer actually uses when the queue is no longer
small enough to click through one card at a time: narrowing it, approving a
batch after confirming, and undoing a mis-click without opening a SQL prompt.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from discord import app_commands

from acm_hub.bot.commands.admin import AdminCommands
from acm_hub.bot.views import ConfirmationView
from acm_hub.db import models
from acm_hub.db.repositories.opportunities import OpportunityRepository
from acm_hub.db.session import Database
from acm_hub.domain.enums import OpportunityStatus, OpportunityType, ReviewDecision
from acm_hub.pipeline.classify import classify_opportunity
from acm_hub.pipeline.normalize import normalize_opportunity
from acm_hub.pipeline.process import OpportunityPipeline
from acm_hub.publishing.discord import BulkApprovalResult
from tests.conftest import FROZEN_NOW, make_raw
from tests.integration.test_approval_buttons import OFFICER_ROLE_ID, fake_interaction
from tests.integration.test_bot_commands import invoke
from tests.integration.test_opportunity_search import add
from tests.integration.test_pipeline import StubCollector
from tests.integration.test_review_workflow import OFFICER_ONE, seed


def bot_for(database: Database) -> MagicMock:
    from acm_hub.bot.client import AcmHubBot

    fake = MagicMock(spec=AcmHubBot)
    fake.database = database
    fake.settings = MagicMock(officer_role_id=OFFICER_ROLE_ID, review_queue_batch_limit=10)
    fake.publisher = AsyncMock()
    fake.scheduler = AsyncMock()
    fake.latency = 0.042
    return fake


def choice(value: str) -> app_commands.Choice[str]:
    return app_commands.Choice(name=value, value=value)


async def queue(database: Database, **kwargs: object) -> models.Opportunity:
    """Store an opportunity already sitting in the officer queue."""
    row = await seed(database, **kwargs)
    async with database.session() as session:
        await OpportunityRepository(session).mark_queued_for_review(
            row.id, channel_id=1, message_id=2
        )
    return row


class TestQueueFiltering:
    async def test_filtering_by_type(self, database: Database) -> None:
        intern = await queue(database, title="Intern role")
        research_raw = make_raw(
            title="Lab assistant",
            application_url="https://example.com/research",
            opportunity_type=OpportunityType.RESEARCH,
        )
        classified = classify_opportunity(
            normalize_opportunity(research_raw, discovered_at=FROZEN_NOW)
        )
        async with database.session() as session:
            research, _ = await OpportunityRepository(session).upsert(classified)
            await OpportunityRepository(session).mark_queued_for_review(
                research.id, channel_id=1, message_id=3
            )

        bot = bot_for(database)
        interaction = fake_interaction(bot)
        await invoke(AdminCommands(bot), AdminCommands.queue, interaction, type=choice("research"))

        description = interaction.followup.send.await_args.kwargs["embed"].description
        assert str(research.id) in description
        assert str(intern.id) not in description

    async def test_filtering_by_sig(self, database: Database) -> None:
        ai = await add(
            database,
            title="Machine Learning Intern",
            sigs=("SIGAI",),
            status=OpportunityStatus.PENDING_REVIEW,
            key="url:ai",
        )
        await add(
            database,
            title="Security Intern",
            sigs=("SIGSAC",),
            status=OpportunityStatus.PENDING_REVIEW,
            key="url:sec",
        )

        bot = bot_for(database)
        interaction = fake_interaction(bot)
        await invoke(AdminCommands(bot), AdminCommands.queue, interaction, sig=choice("SIGAI"))

        description = interaction.followup.send.await_args.kwargs["embed"].description
        assert str(ai.id) in description
        assert "Security Intern" not in description

    async def test_filtering_by_organization(self, database: Database) -> None:
        cloudflare = await queue(database, organization="Cloudflare")
        await queue(database, organization="Stripe", application_url="https://example.com/stripe")

        bot = bot_for(database)
        interaction = fake_interaction(bot)
        await invoke(AdminCommands(bot), AdminCommands.queue, interaction, organization="cloud")

        description = interaction.followup.send.await_args.kwargs["embed"].description
        assert str(cloudflare.id) in description
        assert "Stripe" not in description

    async def test_newest_first_reverses_the_default_order(self, database: Database) -> None:
        older = await add(
            database,
            title="Older",
            status=OpportunityStatus.PENDING_REVIEW,
            discovered_at=FROZEN_NOW - timedelta(days=2),
            key="url:old",
        )
        newer = await add(
            database,
            title="Newer",
            status=OpportunityStatus.PENDING_REVIEW,
            discovered_at=FROZEN_NOW,
            key="url:new",
        )

        bot = bot_for(database)
        oldest_first = fake_interaction(bot)
        await invoke(AdminCommands(bot), AdminCommands.queue, oldest_first)
        oldest_desc = oldest_first.followup.send.await_args.kwargs["embed"].description or ""
        assert oldest_desc.index(str(older.id)) < oldest_desc.index(str(newer.id))

        newest_first = fake_interaction(bot)
        await invoke(AdminCommands(bot), AdminCommands.queue, newest_first, newest_first=True)
        newest_desc = newest_first.followup.send.await_args.kwargs["embed"].description or ""
        assert newest_desc.index(str(newer.id)) < newest_desc.index(str(older.id))


class TestBulkApprove:
    async def test_the_command_asks_for_confirmation_and_does_not_publish_yet(
        self, database: Database
    ) -> None:
        await queue(database)
        bot = bot_for(database)
        interaction = fake_interaction(bot)
        await invoke(AdminCommands(bot), AdminCommands.bulk_approve, interaction)

        view = interaction.followup.send.await_args.kwargs["view"]
        assert isinstance(view, ConfirmationView)
        bot.publisher.approve_and_publish_many.assert_not_awaited()
        assert "publishes" in interaction.followup.send.await_args.args[0].lower()

    async def test_confirming_runs_the_batch_and_reports_the_outcome(
        self, database: Database
    ) -> None:
        row = await queue(database)
        bot = bot_for(database)
        bot.publisher.approve_and_publish_many = AsyncMock(
            return_value=BulkApprovalResult(published=[row.id])
        )
        interaction = fake_interaction(bot)
        await invoke(AdminCommands(bot), AdminCommands.bulk_approve, interaction)
        view = interaction.followup.send.await_args.kwargs["view"]

        await view.confirm.callback(interaction)

        bot.publisher.approve_and_publish_many.assert_awaited_once()
        report = interaction.edit_original_response.await_args.kwargs["content"]
        assert "Published 1 of 1" in report

    async def test_cancelling_changes_nothing(self, database: Database) -> None:
        await queue(database)
        bot = bot_for(database)
        interaction = fake_interaction(bot)
        await invoke(AdminCommands(bot), AdminCommands.bulk_approve, interaction)
        view = interaction.followup.send.await_args.kwargs["view"]

        await view.cancel.callback(interaction)

        bot.publisher.approve_and_publish_many.assert_not_awaited()
        assert "Cancelled" in interaction.response.edit_message.await_args.kwargs["content"]

    async def test_partial_failures_are_itemised_not_hidden(self) -> None:
        published_id = uuid4()
        failed_id = uuid4()
        result = BulkApprovalResult(published=[published_id], failed=[(failed_id, "HTTPException")])
        assert "Published 1 of 2" in result.summary()
        assert str(failed_id) in result.summary()
        assert "HTTPException" in result.summary()

    async def test_an_empty_filter_match_does_not_ask_for_confirmation(
        self, database: Database
    ) -> None:
        bot = bot_for(database)
        interaction = fake_interaction(bot)
        await invoke(
            AdminCommands(bot), AdminCommands.bulk_approve, interaction, sig=choice("SIGACCESS")
        )
        assert "Nothing in the review queue" in interaction.followup.send.await_args.args[0]
        assert interaction.followup.send.await_args.kwargs.get("view") is None


class TestReconsider:
    async def test_a_rejected_opportunity_returns_to_the_queue(self, database: Database) -> None:
        row = await seed(database)
        async with database.session() as session:
            await OpportunityRepository(session).record_decision(
                row.id, decision=ReviewDecision.REJECTED, reviewer_discord_id=OFFICER_ONE
            )

        bot = bot_for(database)
        interaction = fake_interaction(bot)
        await invoke(AdminCommands(bot), AdminCommands.reconsider, interaction, str(row.id))

        assert "Back in the queue" in interaction.followup.send.await_args.args[0]
        async with database.session() as session:
            refreshed = await OpportunityRepository(session).get(row.id)
            awaiting = await OpportunityRepository(session).list_awaiting_queue(10)
        assert refreshed is not None
        assert refreshed.status is OpportunityStatus.DISCOVERED
        assert refreshed.review_message_id is None
        assert [item.id for item in awaiting] == [row.id]

    async def test_the_original_rejection_stays_in_the_audit_trail(
        self, database: Database
    ) -> None:
        row = await seed(database)
        async with database.session() as session:
            await OpportunityRepository(session).record_decision(
                row.id,
                decision=ReviewDecision.REJECTED,
                reviewer_discord_id=OFFICER_ONE,
                reason="too junior",
            )

        bot = bot_for(database)
        await invoke(
            AdminCommands(bot), AdminCommands.reconsider, fake_interaction(bot), str(row.id)
        )

        async with database.session() as session:
            refreshed = await OpportunityRepository(session).get(row.id)
        assert refreshed is not None
        assert refreshed.reviews[0].decision is ReviewDecision.REJECTED
        assert refreshed.reviews[0].reason == "too junior"

    async def test_reconsidering_an_unrejected_opportunity_is_refused(
        self, database: Database
    ) -> None:
        row = await seed(database)
        bot = bot_for(database)
        interaction = fake_interaction(bot)
        await invoke(AdminCommands(bot), AdminCommands.reconsider, interaction, str(row.id))
        assert "not currently rejected" in interaction.followup.send.await_args.args[0]

    async def test_rejected_lists_the_id_an_officer_needs(self, database: Database) -> None:
        row = await seed(database)
        async with database.session() as session:
            await OpportunityRepository(session).record_decision(
                row.id, decision=ReviewDecision.REJECTED, reviewer_discord_id=OFFICER_ONE
            )

        bot = bot_for(database)
        interaction = fake_interaction(bot)
        await invoke(AdminCommands(bot), AdminCommands.rejected, interaction)
        assert str(row.id) in interaction.followup.send.await_args.kwargs["embed"].description


class TestHealthDoesNotLeak:
    async def test_health_does_not_print_secrets_urls_or_traces(self, database: Database) -> None:
        bot = bot_for(database)
        interaction = fake_interaction(bot)
        await invoke(AdminCommands(bot), AdminCommands.health, interaction)

        embed = interaction.followup.send.await_args.kwargs["embed"]
        blob = " ".join(
            [
                embed.title or "",
                embed.description or "",
                *(f"{field.name} {field.value}" for field in embed.fields),
                embed.footer.text or "",
            ]
        ).lower()
        assert "postgresql" not in blob
        assert "sqlite" not in blob
        assert "token" not in blob
        assert "traceback" not in blob
        assert "secret" not in blob
        assert "asyncpg" not in blob
        assert embed.footer.text == "Failure detail is in the bot logs, not here."

    async def test_a_failed_run_is_reported_without_its_error_text(
        self, database: Database
    ) -> None:
        await OpportunityPipeline(database).run(StubCollector(error="postgresql://user:pass@db/x"))

        bot = bot_for(database)
        interaction = fake_interaction(bot)
        await invoke(AdminCommands(bot), AdminCommands.health, interaction)

        embed = interaction.followup.send.await_args.kwargs["embed"]
        blob = " ".join(str(field.value) for field in embed.fields)
        assert "postgresql://" not in blob
        assert "user:pass" not in blob
        assert "see the bot logs" in blob.lower()
