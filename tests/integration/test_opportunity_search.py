"""Database-level filtering and pagination for member search.

Two things are being pinned down. First, that each filter narrows on the right
axis and that combining them means AND rather than OR. Second, that paging is
done by the database and is stable - a member pressing Next must not see a row
twice or skip one, which is exactly what happens when rows share a timestamp
and the sort has no tiebreaker.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from acm_hub.db import models
from acm_hub.db.repositories.opportunities import (
    OpportunityFilters,
    OpportunityRepository,
    apply_filters,
)
from acm_hub.db.session import Database
from acm_hub.domain.enums import OpportunityStatus, OpportunityType, RemoteStatus
from tests.conftest import FROZEN_NOW


async def add(
    database: Database,
    *,
    title: str = "Software Engineering Intern",
    organization: str = "Cloudflare",
    opportunity_type: OpportunityType = OpportunityType.INTERNSHIP,
    remote_status: RemoteStatus = RemoteStatus.UNKNOWN,
    sigs: tuple[str, ...] = (),
    status: OpportunityStatus = OpportunityStatus.PUBLISHED,
    discovered_at: datetime | None = None,
    key: str | None = None,
) -> models.Opportunity:
    """Insert one opportunity directly, so a test states only what it cares about."""
    async with database.session() as session:
        row = models.Opportunity(
            canonical_key=key or f"url:{title}-{organization}-{discovered_at}",
            content_hash="hash",
            source_name="stub",
            source_url="https://example.test/source",
            application_url=f"https://example.test/{abs(hash((title, organization)))}",
            title=title,
            organization=organization,
            opportunity_type=opportunity_type,
            locations=["Austin, TX"],
            remote_status=remote_status,
            tags=[],
            sig_matches=[{"sig": sig, "score": 0.9, "matched_terms": []} for sig in sigs],
            degree_requirements=[],
            status=status,
            discovered_at=discovered_at or FROZEN_NOW,
            sigs=[models.OpportunitySig(sig=sig, score=0.9) for sig in sigs],
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return row


async def titles(database: Database, filters: OpportunityFilters) -> list[str]:
    async with database.session() as session:
        rows = await OpportunityRepository(session).search_published(filters, limit=50)
    return [row.title for row in rows]


class TestFilters:
    async def test_only_published_opportunities_are_visible_to_members(
        self, database: Database
    ) -> None:
        await add(database, title="Published", status=OpportunityStatus.PUBLISHED)
        await add(database, title="Awaiting", status=OpportunityStatus.PENDING_REVIEW)
        await add(database, title="Rejected", status=OpportunityStatus.REJECTED)
        await add(database, title="Closed", status=OpportunityStatus.CLOSED)

        assert await titles(database, OpportunityFilters()) == ["Published"]

    async def test_filtering_by_type(self, database: Database) -> None:
        await add(database, title="Intern", opportunity_type=OpportunityType.INTERNSHIP)
        await add(database, title="Research", opportunity_type=OpportunityType.RESEARCH)

        found = await titles(
            database, OpportunityFilters(opportunity_types=(OpportunityType.RESEARCH,))
        )
        assert found == ["Research"]

    async def test_filtering_by_sig_uses_the_projection_table(self, database: Database) -> None:
        await add(database, title="AI role", sigs=("SIGAI",))
        await add(database, title="Security role", sigs=("SIGSAC",))
        await add(database, title="Unclassified role")

        assert await titles(database, OpportunityFilters(sigs=("SIGAI",))) == ["AI role"]

    async def test_a_row_matching_two_requested_sigs_appears_once(self, database: Database) -> None:
        """The subquery form must not multiply rows the way a join would."""
        await add(database, title="Both", sigs=("SIGAI", "SIGKDD"))

        assert await titles(database, OpportunityFilters(sigs=("SIGAI", "SIGKDD"))) == ["Both"]

    async def test_filtering_by_organization_is_a_case_insensitive_substring(
        self, database: Database
    ) -> None:
        await add(database, title="A", organization="Cloudflare")
        await add(database, title="B", organization="Stripe")

        assert await titles(database, OpportunityFilters(organization="cloud")) == ["A"]

    async def test_remote_excludes_postings_that_say_nothing_about_remote_work(
        self, database: Database
    ) -> None:
        """Silence is not evidence of remote. Unknown stays out."""
        await add(database, title="Remote", remote_status=RemoteStatus.REMOTE)
        await add(database, title="Hybrid", remote_status=RemoteStatus.HYBRID)
        await add(database, title="Onsite", remote_status=RemoteStatus.ONSITE)
        await add(database, title="Silent", remote_status=RemoteStatus.UNKNOWN)

        assert sorted(await titles(database, OpportunityFilters(remote_only=True))) == [
            "Hybrid",
            "Remote",
        ]

    async def test_free_text_matches_title_or_organization(self, database: Database) -> None:
        await add(database, title="Compiler Engineer", organization="Acme")
        await add(database, title="Data Analyst", organization="Compiler Labs")
        await add(database, title="Nothing relevant", organization="Zeta")

        assert sorted(await titles(database, OpportunityFilters(query="compiler"))) == [
            "Compiler Engineer",
            "Data Analyst",
        ]

    async def test_wildcards_in_user_input_are_treated_as_literal_text(
        self, database: Database
    ) -> None:
        """Without escaping, searching for `%` would match every opportunity."""
        await add(database, title="Up to 100% remote", organization="Acme")
        await add(database, title="Ordinary role", organization="Zeta")

        assert await titles(database, OpportunityFilters(query="100%")) == ["Up to 100% remote"]
        assert await titles(database, OpportunityFilters(query="_")) == []

    async def test_filters_combine_with_and_not_or(self, database: Database) -> None:
        await add(database, title="AI at Cloudflare", organization="Cloudflare", sigs=("SIGAI",))
        await add(database, title="AI at Stripe", organization="Stripe", sigs=("SIGAI",))
        await add(database, title="Other at Cloudflare", organization="Cloudflare")

        found = await titles(
            database, OpportunityFilters(sigs=("SIGAI",), organization="Cloudflare")
        )
        assert found == ["AI at Cloudflare"]

    async def test_an_empty_filter_set_constrains_nothing(self, database: Database) -> None:
        assert OpportunityFilters().is_empty is True
        assert OpportunityFilters(remote_only=True).is_empty is False

    def test_apply_filters_leaves_a_statement_untouched_when_empty(self) -> None:
        import sqlalchemy as sa

        statement = sa.select(models.Opportunity)
        assert str(apply_filters(statement, OpportunityFilters())) == str(statement)


class TestPagination:
    @pytest.fixture
    async def twelve(self, database: Database) -> Database:
        for index in range(12):
            await add(
                database,
                title=f"Role {index:02d}",
                discovered_at=FROZEN_NOW - timedelta(minutes=index),
                key=f"url:{index}",
            )
        return database

    async def test_pages_are_produced_by_the_database(self, twelve: Database) -> None:
        async with twelve.session() as session:
            repository = OpportunityRepository(session)
            first = await repository.search_published(OpportunityFilters(), limit=5, offset=0)
            second = await repository.search_published(OpportunityFilters(), limit=5, offset=5)
            third = await repository.search_published(OpportunityFilters(), limit=5, offset=10)

        assert [row.title for row in first] == [f"Role {i:02d}" for i in range(5)]
        assert [row.title for row in second] == [f"Role {i:02d}" for i in range(5, 10)]
        assert [row.title for row in third] == ["Role 10", "Role 11"]

    async def test_the_count_is_the_full_match_count_not_the_page_size(
        self, twelve: Database
    ) -> None:
        async with twelve.session() as session:
            total = await OpportunityRepository(session).count_published(OpportunityFilters())
        assert total == 12

    async def test_the_count_respects_the_same_filters_as_the_page(self, twelve: Database) -> None:
        await add(twelve, title="Only AI", sigs=("SIGAI",), key="url:ai")

        filters = OpportunityFilters(sigs=("SIGAI",))
        async with twelve.session() as session:
            repository = OpportunityRepository(session)
            page = await repository.search_published(filters, limit=5)
            total = await repository.count_published(filters)

        assert [row.title for row in page] == ["Only AI"]
        assert total == 1

    async def test_paging_is_stable_when_rows_share_a_timestamp(self, database: Database) -> None:
        """Discovery stamps a whole batch identically; ties need a tiebreaker."""
        same_moment = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
        for index in range(9):
            await add(
                database,
                title=f"Batch {index}",
                discovered_at=same_moment,
                key=f"url:batch-{index}",
            )

        async with database.session() as session:
            repository = OpportunityRepository(session)
            pages = [
                await repository.search_published(OpportunityFilters(), limit=3, offset=offset)
                for offset in (0, 3, 6)
            ]

        seen = [row.id for page in pages for row in page]
        assert len(seen) == 9
        assert len(set(seen)) == 9, "a row appeared on two pages"

    async def test_an_offset_past_the_end_returns_nothing_rather_than_failing(
        self, twelve: Database
    ) -> None:
        async with twelve.session() as session:
            rows = await OpportunityRepository(session).search_published(
                OpportunityFilters(), limit=5, offset=500
            )
        assert rows == []
