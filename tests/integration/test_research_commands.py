"""``/acm research`` and ``/acm conferences`` browse behaviour.

These commands are type-locked. Saved opportunity types must not hide research
or conferences, and internships must never appear in either list.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from acm_hub.bot.commands.acm import AcmCommands
from acm_hub.db.repositories.subscriptions import SubscriptionRepository
from acm_hub.db.session import Database
from acm_hub.domain.enums import OpportunityStatus, OpportunityType
from tests.integration.test_approval_buttons import OFFICER_ROLE_ID
from tests.integration.test_bot_commands import invoke
from tests.integration.test_member_commands import (
    MEMBER_ID,
    choice,
    member_interaction,
    sent_embed,
    sent_text,
)
from tests.integration.test_opportunity_search import add


@pytest.fixture
def bot(database: Database) -> MagicMock:
    from acm_hub.bot.client import AcmHubBot

    fake = MagicMock(spec=AcmHubBot)
    fake.database = database
    fake.settings = MagicMock(officer_role_id=OFFICER_ROLE_ID)
    return fake


class TestResearchBrowse:
    async def test_empty_hub_says_so(self, bot: MagicMock, database: Database) -> None:
        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.research, interaction, all=True)
        assert "No research mentorships have been published yet." in sent_text(interaction)

    async def test_only_published_research_is_listed(
        self, bot: MagicMock, database: Database
    ) -> None:
        await add(
            database,
            title="Ready to mentor: Aman Arora",
            organization="Aman Arora",
            opportunity_type=OpportunityType.RESEARCH,
        )
        await add(
            database,
            title="Hidden mentor",
            organization="Hidden",
            opportunity_type=OpportunityType.RESEARCH,
            status=OpportunityStatus.PENDING_REVIEW,
        )
        await add(database, title="Compiler Intern", organization="Acme")

        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.research, interaction, all=True)

        description = sent_embed(interaction).description or ""
        assert "Aman Arora" in description
        assert "Hidden mentor" not in description
        assert "Compiler Intern" not in description
        title = sent_embed(interaction).title
        assert title is not None
        assert title.startswith("Research")

    async def test_saved_types_do_not_hide_research(
        self, bot: MagicMock, database: Database
    ) -> None:
        await add(
            database,
            title="Ready to mentor: Ruben Acuna",
            organization="Ruben Acuna",
            opportunity_type=OpportunityType.RESEARCH,
            sigs=("SIGCSE",),
        )
        async with database.session() as session:
            await SubscriptionRepository(session).upsert(
                MEMBER_ID, opportunity_types=["internship"], sigs=["SIGCSE"]
            )

        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.research, interaction)

        embed = sent_embed(interaction)
        assert "Ruben Acuna" in (embed.description or "")
        author = embed.author.name or ""
        assert author.startswith("Using your ACM interests:")
        assert "SIGCSE" in author
        assert "Internships" not in author

    async def test_saved_sigs_filter_research(self, bot: MagicMock, database: Database) -> None:
        await add(
            database,
            title="Ready to mentor: AI Faculty",
            organization="AI Faculty",
            opportunity_type=OpportunityType.RESEARCH,
            sigs=("SIGAI",),
        )
        await add(
            database,
            title="Ready to mentor: Graphics Faculty",
            organization="Graphics Faculty",
            opportunity_type=OpportunityType.RESEARCH,
            sigs=("SIGGRAPH",),
        )
        async with database.session() as session:
            await SubscriptionRepository(session).upsert(MEMBER_ID, sigs=["SIGAI"])

        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.research, interaction)

        description = sent_embed(interaction).description or ""
        assert "AI Faculty" in description
        assert "Graphics Faculty" not in description

    async def test_all_true_ignores_saved_sigs(self, bot: MagicMock, database: Database) -> None:
        await add(
            database,
            title="Ready to mentor: AI Faculty",
            organization="AI Faculty",
            opportunity_type=OpportunityType.RESEARCH,
            sigs=("SIGAI",),
        )
        await add(
            database,
            title="Ready to mentor: Graphics Faculty",
            organization="Graphics Faculty",
            opportunity_type=OpportunityType.RESEARCH,
            sigs=("SIGGRAPH",),
        )
        async with database.session() as session:
            await SubscriptionRepository(session).upsert(MEMBER_ID, sigs=["SIGAI"])

        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.research, interaction, all=True)

        description = sent_embed(interaction).description or ""
        assert "AI Faculty" in description
        assert "Graphics Faculty" in description
        assert sent_embed(interaction).author.name is None

    async def test_faculty_filter(self, bot: MagicMock, database: Database) -> None:
        await add(
            database,
            title="Ready to mentor: Aman Arora",
            organization="Aman Arora",
            opportunity_type=OpportunityType.RESEARCH,
        )
        await add(
            database,
            title="Ready to mentor: Ruben Acuna",
            organization="Ruben Acuna",
            opportunity_type=OpportunityType.RESEARCH,
        )

        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.research, interaction, None, "Arora")

        description = sent_embed(interaction).description or ""
        assert "Aman Arora" in description
        assert "Ruben Acuna" not in description


class TestConferenceBrowse:
    async def test_empty_hub_says_so(self, bot: MagicMock, database: Database) -> None:
        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.conferences, interaction, all=True)
        assert "No conferences have been published yet." in sent_text(interaction)

    async def test_only_published_conferences_are_listed(
        self, bot: MagicMock, database: Database
    ) -> None:
        await add(
            database,
            title="ACM Internet Measurement Conference",
            organization="ACM SIGCOMM",
            opportunity_type=OpportunityType.CONFERENCE,
        )
        await add(database, title="Compiler Intern", organization="Acme")
        await add(
            database,
            title="Ready to mentor: Aman Arora",
            organization="Aman Arora",
            opportunity_type=OpportunityType.RESEARCH,
        )

        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.conferences, interaction, all=True)

        description = sent_embed(interaction).description or ""
        assert "Internet Measurement" in description
        assert "Compiler Intern" not in description
        assert "Aman Arora" not in description

    async def test_explicit_sig_filter(self, bot: MagicMock, database: Database) -> None:
        await add(
            database,
            title="CHI Conference",
            organization="ACM",
            opportunity_type=OpportunityType.CONFERENCE,
            sigs=("SIGCHI",),
        )
        await add(
            database,
            title="IMC Conference",
            organization="ACM",
            opportunity_type=OpportunityType.CONFERENCE,
            sigs=("SIGCOMM",),
        )

        cog = AcmCommands(bot)
        interaction = member_interaction(bot)
        await invoke(cog, cog.conferences, interaction, choice("SIGCHI"))

        description = sent_embed(interaction).description or ""
        assert "CHI Conference" in description
        assert "IMC Conference" not in description
