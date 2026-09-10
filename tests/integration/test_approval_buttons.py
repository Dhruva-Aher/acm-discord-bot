"""The officer approval buttons themselves.

The repository-level idempotency guarantees are covered in
``test_review_workflow``. What is exercised here is the button handler on top of
them: authorization, the double-press path, and the fact that an interaction is
always answered even when publishing fails.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import discord
import pytest

from acm_hub.db.repositories.opportunities import OpportunityRepository
from acm_hub.db.session import Database
from acm_hub.domain.enums import OpportunityStatus, ReviewDecision
from acm_hub.publishing.approval import (
    APPROVE_PREFIX,
    REJECT_PREFIX,
    ApproveButton,
    RejectButton,
    RejectionReasonModal,
    _apply_decision,
    build_review_view,
)
from acm_hub.publishing.discord import ChannelUnavailableError
from tests.integration.test_review_workflow import (
    OFFICER_ONE,
    OFFICER_TWO,
    OPPORTUNITIES_CHANNEL_ID,
    seed,
)

OFFICER_ROLE_ID = 444444444444444444


def fake_interaction(
    bot: MagicMock, *, user_id: int = OFFICER_ONE, is_officer: bool = True
) -> MagicMock:
    """An interaction whose responses are recorded rather than sent."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = bot

    user = MagicMock(spec=discord.Member)
    user.id = user_id
    user.mention = f"<@{user_id}>"
    user.roles = [MagicMock(id=OFFICER_ROLE_ID)] if is_officer else []
    user.guild_permissions = discord.Permissions(manage_guild=False)
    interaction.user = user

    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    message = MagicMock(spec=discord.Message)
    message.id = 987654321
    message.embeds = [discord.Embed(title="Pending")]
    message.edit = AsyncMock()
    interaction.message = message

    return interaction


@pytest.fixture
def bot(database: Database) -> MagicMock:
    """A stand-in for AcmHubBot exposing only what the handlers touch."""
    from acm_hub.bot.client import AcmHubBot

    fake = MagicMock(spec=AcmHubBot)
    fake.database = database
    fake.settings = MagicMock(officer_role_id=OFFICER_ROLE_ID)
    fake.publisher = AsyncMock()
    return fake


class TestAuthorization:
    async def test_a_non_officer_press_is_refused(self, database: Database, bot: MagicMock) -> None:
        row = await seed(database)
        interaction = fake_interaction(bot, is_officer=False)

        await ApproveButton(row.id).callback(interaction)

        interaction.response.send_message.assert_awaited_once()
        assert "officers" in interaction.response.send_message.await_args.args[0]
        bot.publisher.publish_approved.assert_not_awaited()

    async def test_a_non_officer_press_does_not_change_the_status(
        self, database: Database, bot: MagicMock
    ) -> None:
        row = await seed(database)
        await ApproveButton(row.id).callback(fake_interaction(bot, is_officer=False))

        async with database.session() as session:
            refreshed = await OpportunityRepository(session).get(row.id)
        assert refreshed is not None
        assert refreshed.status is OpportunityStatus.DISCOVERED

    async def test_a_non_officer_cannot_reject_either(
        self, database: Database, bot: MagicMock
    ) -> None:
        row = await seed(database)
        interaction = fake_interaction(bot, is_officer=False)
        await RejectButton(row.id).callback(interaction)
        interaction.response.send_modal.assert_not_awaited()


class TestApprovePress:
    async def test_approving_publishes_and_confirms(
        self, database: Database, bot: MagicMock
    ) -> None:
        row = await seed(database)
        interaction = fake_interaction(bot)

        await ApproveButton(row.id).callback(interaction)

        bot.publisher.publish_approved.assert_awaited_once_with(row.id)
        assert "Approved and published" in interaction.followup.send.await_args.args[0]

    async def test_the_decision_is_recorded_against_the_pressing_officer(
        self, database: Database, bot: MagicMock
    ) -> None:
        row = await seed(database)
        await ApproveButton(row.id).callback(fake_interaction(bot, user_id=OFFICER_TWO))

        async with database.session() as session:
            refreshed = await OpportunityRepository(session).get(row.id)
        assert refreshed is not None
        assert refreshed.reviews[0].reviewer_discord_id == OFFICER_TWO
        assert refreshed.reviews[0].decision is ReviewDecision.APPROVED

    async def test_the_card_is_stamped_and_its_buttons_removed(
        self, database: Database, bot: MagicMock
    ) -> None:
        row = await seed(database)
        interaction = fake_interaction(bot)

        await ApproveButton(row.id).callback(interaction)

        interaction.message.edit.assert_awaited_once()
        assert interaction.message.edit.await_args.kwargs["view"] is None

    async def test_a_second_officer_pressing_approve_publishes_nothing(
        self, database: Database, bot: MagicMock
    ) -> None:
        """The button-level view of the double-approval race."""
        row = await seed(database)
        await ApproveButton(row.id).callback(fake_interaction(bot, user_id=OFFICER_ONE))
        bot.publisher.publish_approved.reset_mock()

        second = fake_interaction(bot, user_id=OFFICER_TWO)
        await ApproveButton(row.id).callback(second)

        bot.publisher.publish_approved.assert_not_awaited()
        assert "already reviewed" in second.followup.send.await_args.args[0]

    async def test_a_publish_failure_is_reported_and_stays_approved(
        self, database: Database, bot: MagicMock
    ) -> None:
        row = await seed(database)
        bot.publisher.publish_approved.side_effect = ChannelUnavailableError("channel missing")
        interaction = fake_interaction(bot)

        await ApproveButton(row.id).callback(interaction)

        assert "publishing" in interaction.followup.send.await_args.args[0]
        async with database.session() as session:
            refreshed = await OpportunityRepository(session).get(row.id)
        assert refreshed is not None
        assert refreshed.status is OpportunityStatus.APPROVED

    async def test_a_cosmetic_edit_failure_does_not_undo_the_approval(
        self, database: Database, bot: MagicMock
    ) -> None:
        row = await seed(database)
        interaction = fake_interaction(bot)
        interaction.message.edit = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=403), "forbidden")
        )

        await ApproveButton(row.id).callback(interaction)

        async with database.session() as session:
            refreshed = await OpportunityRepository(session).get(row.id)
        assert refreshed is not None
        assert refreshed.status is OpportunityStatus.APPROVED


class TestRejectPress:
    async def test_pressing_reject_asks_for_a_reason(
        self, database: Database, bot: MagicMock
    ) -> None:
        row = await seed(database)
        interaction = fake_interaction(bot)

        await RejectButton(row.id).callback(interaction)

        interaction.response.send_modal.assert_awaited_once()
        assert isinstance(interaction.response.send_modal.await_args.args[0], RejectionReasonModal)

    async def test_submitting_the_modal_records_the_rejection(
        self, database: Database, bot: MagicMock
    ) -> None:
        row = await seed(database)
        modal = RejectionReasonModal(row.id)
        modal.reason._value = "duplicate of an existing post"  # noqa: SLF001 - simulating input
        interaction = fake_interaction(bot)

        await modal.on_submit(interaction)

        async with database.session() as session:
            refreshed = await OpportunityRepository(session).get(row.id)
        assert refreshed is not None
        assert refreshed.status is OpportunityStatus.REJECTED
        assert refreshed.reviews[0].reason == "duplicate of an existing post"

    async def test_an_empty_reason_is_stored_as_none_not_empty_text(
        self, database: Database, bot: MagicMock
    ) -> None:
        row = await seed(database)
        modal = RejectionReasonModal(row.id)
        modal.reason._value = "   "  # noqa: SLF001 - simulating input
        await modal.on_submit(fake_interaction(bot))

        async with database.session() as session:
            refreshed = await OpportunityRepository(session).get(row.id)
        assert refreshed is not None
        assert refreshed.reviews[0].reason is None

    async def test_rejecting_never_publishes(self, database: Database, bot: MagicMock) -> None:
        row = await seed(database)
        await _apply_decision(fake_interaction(bot), row.id, ReviewDecision.REJECTED)
        bot.publisher.publish_approved.assert_not_awaited()


class TestPersistentCustomIds:
    async def test_the_view_carries_approve_reject_and_a_source_link(self) -> None:
        opportunity_id = uuid4()
        view = build_review_view(opportunity_id, "https://example.test/source")

        custom_ids = [getattr(item, "custom_id", None) for item in view.children]
        assert f"{APPROVE_PREFIX}{opportunity_id}" in custom_ids
        assert f"{REJECT_PREFIX}{opportunity_id}" in custom_ids
        assert any(
            getattr(item, "url", None) == "https://example.test/source" for item in view.children
        )

    async def test_the_view_never_expires(self) -> None:
        assert build_review_view(uuid4(), "https://example.test").timeout is None

    @pytest.mark.parametrize("button", [ApproveButton, RejectButton])
    async def test_a_button_is_rebuilt_from_its_custom_id_after_a_restart(
        self, button: type[ApproveButton] | type[RejectButton]
    ) -> None:
        """Persistence: a card posted by a previous process must still work."""
        opportunity_id = uuid4()
        instance = button(opportunity_id)
        match = button.__discord_ui_compiled_template__.fullmatch(instance.custom_id)

        assert match is not None
        rebuilt = await button.from_custom_id(MagicMock(), MagicMock(), match)
        assert rebuilt.opportunity_id == opportunity_id

    async def test_an_unrelated_custom_id_is_not_claimed(self) -> None:
        assert (
            ApproveButton.__discord_ui_compiled_template__.fullmatch("acmhub:review:approve:nope")
            is None
        )


class TestApplyDecisionOnMissingRecords:
    async def test_a_decision_on_an_unknown_id_is_reported_not_crashed(
        self, bot: MagicMock
    ) -> None:
        interaction = fake_interaction(bot)
        await _apply_decision(interaction, uuid4(), ReviewDecision.APPROVED)
        assert "already reviewed" in interaction.followup.send.await_args.args[0]

    async def test_the_opportunity_id_type_is_preserved(self, database: Database) -> None:
        row = await seed(database)
        assert isinstance(row.id, UUID)
        assert isinstance(ApproveButton(row.id).opportunity_id, UUID)


async def test_publishing_channel_id_is_never_hardcoded_in_the_handlers() -> None:
    """Channel routing must come from configuration, never from the button code."""
    import inspect

    from acm_hub.publishing import approval

    source = inspect.getsource(approval)
    assert str(OPPORTUNITIES_CHANNEL_ID) not in source
    assert "channel_id=" not in source
