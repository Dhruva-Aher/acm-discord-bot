"""``/acm opportunities`` and ``/acm subscribe`` behaviour.

These drive the command callbacks directly with a fake interaction, so they
cover the parts a live-server smoke test cannot check quickly: what a member
sees when nothing matches, that saved interests are applied *and disclosed*,
that the escape hatch works, and that one member cannot drive another member's
pagination.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from acm_hub.bot.commands.acm import AcmCommands
from acm_hub.bot.subscriptions import SubscriptionView, describe_interests
from acm_hub.bot.views import PaginatedView
from acm_hub.db.repositories.subscriptions import SubscriptionRepository
from acm_hub.db.session import Database
from acm_hub.domain.enums import OpportunityStatus, OpportunityType
from tests.integration.test_approval_buttons import OFFICER_ROLE_ID, fake_interaction
from tests.integration.test_bot_commands import invoke
from tests.integration.test_opportunity_search import add

MEMBER_ID = 555_000_111
OTHER_MEMBER_ID = 555_000_222


@pytest.fixture
def bot(database: Database) -> MagicMock:
    from acm_hub.bot.client import AcmHubBot

    fake = MagicMock(spec=AcmHubBot)
    fake.database = database
    fake.settings = MagicMock(officer_role_id=OFFICER_ROLE_ID)
    fake.publisher = AsyncMock()
    fake.scheduler = AsyncMock()
    fake.latency = 0.042
    return fake


def member_interaction(bot: MagicMock, user_id: int = MEMBER_ID) -> MagicMock:
    interaction = fake_interaction(bot)
    interaction.user.id = user_id
    return interaction


def choice(value: str) -> app_commands.Choice[str]:
    return app_commands.Choice(name=value, value=value)


def sent_embed(interaction: MagicMock) -> discord.Embed:
    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert isinstance(embed, discord.Embed)
    return embed


def sent_text(interaction: MagicMock) -> str:
    call = interaction.followup.send.await_args
    if call.args:
        assert isinstance(call.args[0], str)
        return call.args[0]
    content = call.kwargs.get("content", "")
    assert isinstance(content, str)
    return content


class TestBrowsing:
    async def test_an_empty_hub_says_so_without_erroring(
        self, bot: MagicMock, database: Database
    ) -> None:
        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.opportunities, interaction, all=True)

        assert "Nothing has been published yet." in sent_text(interaction)

    async def test_published_opportunities_are_listed(
        self, bot: MagicMock, database: Database
    ) -> None:
        await add(database, title="Compiler Intern", organization="Acme")

        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.opportunities, interaction)

        embed = sent_embed(interaction)
        assert "Compiler Intern" in (embed.description or "")
        assert interaction.followup.send.await_args.kwargs["ephemeral"] is True

    async def test_unpublished_opportunities_are_never_shown(
        self, bot: MagicMock, database: Database
    ) -> None:
        await add(database, title="Awaiting review", status=OpportunityStatus.PENDING_REVIEW)

        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.opportunities, interaction, all=True)

        assert "Nothing has been published yet." in sent_text(interaction)

    async def test_an_explicit_filter_narrows_the_results(
        self, bot: MagicMock, database: Database
    ) -> None:
        await add(database, title="AI role", sigs=("SIGAI",))
        await add(database, title="Security role", sigs=("SIGSAC",))

        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.opportunities, interaction, None, choice("SIGAI"))

        description = sent_embed(interaction).description or ""
        assert "AI role" in description
        assert "Security role" not in description

    async def test_a_filter_matching_nothing_explains_itself(
        self, bot: MagicMock, database: Database
    ) -> None:
        await add(database, title="AI role", sigs=("SIGAI",))

        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.opportunities, interaction, None, choice("SIGACCESS"))

        assert "Try fewer filters" in sent_text(interaction)


class TestPersonalization:
    async def test_saved_interests_are_applied_when_no_filters_are_given(
        self, bot: MagicMock, database: Database
    ) -> None:
        await add(database, title="AI role", sigs=("SIGAI",))
        await add(database, title="Graphics role", sigs=("SIGGRAPH",))
        async with database.session() as session:
            await SubscriptionRepository(session).upsert(MEMBER_ID, sigs=["SIGAI"])

        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.opportunities, interaction)

        embed = sent_embed(interaction)
        assert "AI role" in (embed.description or "")
        assert "Graphics role" not in (embed.description or "")

    async def test_the_member_is_told_their_interests_are_being_applied(
        self, bot: MagicMock, database: Database
    ) -> None:
        """Otherwise a narrow result set reads as 'ACM Hub found nothing'."""
        await add(database, title="AI role", sigs=("SIGAI",))
        async with database.session() as session:
            await SubscriptionRepository(session).upsert(MEMBER_ID, sigs=["SIGAI"])

        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.opportunities, interaction)

        author = sent_embed(interaction).author.name or ""
        assert author.startswith("Using your ACM interests:")
        assert "SIGAI" in author

    async def test_all_true_ignores_saved_interests(
        self, bot: MagicMock, database: Database
    ) -> None:
        await add(database, title="AI role", sigs=("SIGAI",))
        await add(database, title="Graphics role", sigs=("SIGGRAPH",))
        async with database.session() as session:
            await SubscriptionRepository(session).upsert(MEMBER_ID, sigs=["SIGAI"])

        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.opportunities, interaction, all=True)

        description = sent_embed(interaction).description or ""
        assert "AI role" in description
        assert "Graphics role" in description

    async def test_an_explicit_filter_overrides_saved_interests(
        self, bot: MagicMock, database: Database
    ) -> None:
        await add(database, title="Graphics role", sigs=("SIGGRAPH",))
        async with database.session() as session:
            await SubscriptionRepository(session).upsert(MEMBER_ID, sigs=["SIGAI"])

        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.opportunities, interaction, None, choice("SIGGRAPH"))

        embed = sent_embed(interaction)
        assert "Graphics role" in (embed.description or "")
        assert embed.author.name is None

    async def test_no_results_under_saved_interests_points_at_the_escape_hatch(
        self, bot: MagicMock, database: Database
    ) -> None:
        await add(database, title="Graphics role", sigs=("SIGGRAPH",))
        async with database.session() as session:
            await SubscriptionRepository(session).upsert(MEMBER_ID, sigs=["SIGAI"])

        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.opportunities, interaction)

        message = sent_text(interaction)
        assert "all:true" in message
        assert "/acm subscribe" in message

    async def test_a_member_with_no_saved_interests_sees_everything(
        self, bot: MagicMock, database: Database
    ) -> None:
        await add(database, title="AI role", sigs=("SIGAI",))
        await add(database, title="Graphics role", sigs=("SIGGRAPH",))

        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.opportunities, interaction)

        embed = sent_embed(interaction)
        assert embed.author.name is None
        assert "AI role" in (embed.description or "")
        assert "Graphics role" in (embed.description or "")

    async def test_a_saved_type_that_no_longer_exists_is_skipped_not_fatal(
        self, bot: MagicMock, database: Database
    ) -> None:
        await add(database, title="Intern role", opportunity_type=OpportunityType.INTERNSHIP)
        async with database.session() as session:
            await SubscriptionRepository(session).upsert(
                MEMBER_ID, opportunity_types=["internship", "quantum_teleportation"]
            )

        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.opportunities, interaction)

        assert "Intern role" in (sent_embed(interaction).description or "")


class TestPaginationView:
    async def test_a_second_page_is_offered_when_results_overflow(
        self, bot: MagicMock, database: Database
    ) -> None:
        for index in range(7):
            await add(database, title=f"Role {index}", key=f"url:{index}")

        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.opportunities, interaction, all=True)

        view = interaction.followup.send.await_args.kwargs["view"]
        assert isinstance(view, PaginatedView)
        assert view.page.page_count == 2
        assert view.next_page.disabled is False
        assert view.previous_page.disabled is True

    async def test_paging_forward_re_queries_and_moves_the_window(
        self, bot: MagicMock, database: Database
    ) -> None:
        for index in range(7):
            await add(database, title=f"Role {index}", key=f"url:{index}")

        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.opportunities, interaction, all=True)
        view = interaction.followup.send.await_args.kwargs["view"]

        press = member_interaction(bot)
        await view.next_page.callback(press)

        assert view.page.offset == 5
        assert view.page.page_number == 2
        assert view.next_page.disabled is True
        press.response.edit_message.assert_awaited_once()

    async def test_another_member_cannot_drive_someone_elses_results(
        self, bot: MagicMock, database: Database
    ) -> None:
        for index in range(7):
            await add(database, title=f"Role {index}", key=f"url:{index}")

        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.opportunities, interaction, all=True)
        view = interaction.followup.send.await_args.kwargs["view"]

        intruder = member_interaction(bot, user_id=OTHER_MEMBER_ID)
        assert await view.interaction_check(intruder) is False
        intruder.response.send_message.assert_awaited_once()

    async def test_the_owner_passes_the_check(self, bot: MagicMock, database: Database) -> None:
        await add(database, title="Role")
        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.opportunities, interaction, all=True)
        view = interaction.followup.send.await_args.kwargs["view"]

        assert await view.interaction_check(member_interaction(bot)) is True

    async def test_a_single_page_offers_no_navigation(
        self, bot: MagicMock, database: Database
    ) -> None:
        await add(database, title="Only one")

        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.opportunities, interaction, all=True)
        view = interaction.followup.send.await_args.kwargs["view"]

        assert view.next_page.disabled is True
        assert view.previous_page.disabled is True


class TestSubscribe:
    async def test_a_new_member_starts_with_nothing_saved(
        self, bot: MagicMock, database: Database
    ) -> None:
        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.subscribe, interaction)

        assert "no filters saved" in sent_text(interaction)

    async def test_choosing_sigs_saves_only_the_sig_dimension(
        self, bot: MagicMock, database: Database
    ) -> None:
        """The two menus are submitted separately; neither may wipe the other."""
        async with database.session() as session:
            await SubscriptionRepository(session).upsert(
                MEMBER_ID, opportunity_types=["internship"]
            )

        view = SubscriptionView(
            MEMBER_ID, database=database, opportunity_types=["internship"], sigs=[]
        )
        await view.save(member_interaction(bot), sigs=["SIGAI"])

        async with database.session() as session:
            saved = await SubscriptionRepository(session).get(MEMBER_ID)
        assert saved is not None
        assert saved.opportunity_types == ["internship"]
        assert saved.sigs == ["SIGAI"]

    async def test_deselecting_everything_in_one_menu_clears_only_that_menu(
        self, bot: MagicMock, database: Database
    ) -> None:
        async with database.session() as session:
            await SubscriptionRepository(session).upsert(
                MEMBER_ID, opportunity_types=["internship"], sigs=["SIGAI"]
            )

        view = SubscriptionView(
            MEMBER_ID, database=database, opportunity_types=["internship"], sigs=["SIGAI"]
        )
        await view.save(member_interaction(bot), sigs=[])

        async with database.session() as session:
            saved = await SubscriptionRepository(session).get(MEMBER_ID)
        assert saved is not None
        assert saved.opportunity_types == ["internship"]
        assert saved.sigs == []

    async def test_an_empty_selection_widens_results_rather_than_hiding_everything(
        self, bot: MagicMock, database: Database
    ) -> None:
        await add(database, title="AI role", sigs=("SIGAI",))
        await add(database, title="Graphics role", sigs=("SIGGRAPH",))
        async with database.session() as session:
            await SubscriptionRepository(session).upsert(MEMBER_ID, sigs=[])

        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.opportunities, interaction)

        description = sent_embed(interaction).description or ""
        assert "AI role" in description
        assert "Graphics role" in description

    async def test_clearing_removes_the_saved_row_entirely(
        self, bot: MagicMock, database: Database
    ) -> None:
        async with database.session() as session:
            await SubscriptionRepository(session).upsert(MEMBER_ID, sigs=["SIGAI"])

        view = SubscriptionView(MEMBER_ID, database=database, opportunity_types=[], sigs=["SIGAI"])
        await view.clear.callback(member_interaction(bot))

        async with database.session() as session:
            assert await SubscriptionRepository(session).get(MEMBER_ID) is None

    async def test_the_view_states_that_nothing_is_ever_direct_messaged(
        self, bot: MagicMock, database: Database
    ) -> None:
        view = SubscriptionView(MEMBER_ID, database=database, opportunity_types=[], sigs=[])
        assert "never sends direct messages" in view.summary()

    async def test_another_member_cannot_edit_someone_elses_interests(
        self, bot: MagicMock, database: Database
    ) -> None:
        view = SubscriptionView(MEMBER_ID, database=database, opportunity_types=[], sigs=[])
        assert await view.interaction_check(member_interaction(bot, OTHER_MEMBER_ID)) is False


class TestInterestDescription:
    def test_no_saved_interests_reads_as_unfiltered(self) -> None:
        assert "you see everything" in describe_interests([], [])

    def test_both_dimensions_are_summarised(self) -> None:
        described = describe_interests(["internship"], ["SIGAI"])
        assert "Internships" in described
        assert "SIGAI" in described

    def test_an_unknown_stored_value_is_shown_verbatim(self) -> None:
        assert "mystery" in describe_interests(["mystery"], [])
