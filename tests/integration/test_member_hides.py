"""Personal hides and community removals for dead application links."""

from __future__ import annotations

from acm_hub.db.repositories.hides import MemberHideRepository
from acm_hub.db.repositories.opportunities import OpportunityFilters, OpportunityRepository
from acm_hub.db.session import Database
from acm_hub.domain.enums import OpportunityStatus
from tests.integration.test_opportunity_search import add

MEMBER_ID = 777_001
OTHER_ID = 777_002


class TestMemberHides:
    async def test_hide_excludes_only_that_members_results(self, database: Database) -> None:
        row = await add(database, title="Dead Link Intern", organization="Acme")

        async with database.session() as session:
            await MemberHideRepository(session).hide(
                MEMBER_ID, row.id, reason="reported_invalid_link"
            )
            hidden = await MemberHideRepository(session).hidden_ids(MEMBER_ID)
            repo = OpportunityRepository(session)
            for_member = await repo.search_published(
                OpportunityFilters(), limit=10, exclude_ids=hidden
            )
            for_other = await repo.search_published(OpportunityFilters(), limit=10)

        assert row.id in hidden
        assert all(item.id != row.id for item in for_member)
        assert any(item.id == row.id for item in for_other)

    async def test_mark_closed_removes_from_everyone(self, database: Database) -> None:
        row = await add(database, title="Gone Role", organization="Acme")

        async with database.session() as session:
            closed = await OpportunityRepository(session).mark_closed([row.id])
            assert closed
            assert closed[0].status is OpportunityStatus.CLOSED
            remaining = await OpportunityRepository(session).search_published(
                OpportunityFilters(), limit=10
            )

        assert all(item.id != row.id for item in remaining)
