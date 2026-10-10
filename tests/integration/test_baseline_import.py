"""Initial-import behaviour.

The scenario this protects against is concrete: an officer enables a source
that already lists thousands of internships, and ACM Hub posts a review card
for every one of them. Everything here is about that first run being treated as
backlog, and about the treatment being decided by persisted state rather than
by a date in the code.
"""

from __future__ import annotations

import pytest

from acm_hub.db.repositories.opportunities import OpportunityRepository
from acm_hub.db.repositories.sources import SourceRepository
from acm_hub.db.session import Database
from acm_hub.domain.enums import OpportunityStatus
from acm_hub.domain.source import RawOpportunity
from acm_hub.pipeline.process import OpportunityPipeline
from tests.conftest import make_raw
from tests.integration.test_pipeline import StubCollector


@pytest.fixture
def records() -> list[RawOpportunity]:
    return [make_raw(application_url=f"https://example.com/{index}") for index in range(3)]


class TestFirstRunIsBaseline:
    async def test_the_first_run_stores_everything_without_queueing_it(
        self, database: Database, records: list[RawOpportunity]
    ) -> None:
        """The headline guarantee: enabling a source floods nobody."""
        stats = await OpportunityPipeline(database).run(StubCollector(records))

        assert stats.baseline_import is True
        assert stats.new == 3

        async with database.session() as session:
            repository = OpportunityRepository(session)
            assert await repository.list_awaiting_queue(100) == []
            assert await repository.count_baseline("stub") == 3
            # Stored and searchable, not discarded.
            counts = await repository.count_by_status()
        assert counts[OpportunityStatus.DISCOVERED] == 3

    async def test_the_second_run_queues_new_findings_normally(
        self, database: Database, records: list[RawOpportunity]
    ) -> None:
        pipeline = OpportunityPipeline(database)
        await pipeline.run(StubCollector(records))

        stats = await pipeline.run(
            StubCollector([*records, make_raw(application_url="https://example.com/fresh")])
        )

        assert stats.baseline_import is False
        assert stats.new == 1

        async with database.session() as session:
            # Default auto-publish: news is approved for the publish pass, not
            # left in the officer review queue.
            queued = await OpportunityRepository(session).list_awaiting_publication(100)
        assert [row.application_url for row in queued] == ["https://example.com/fresh"]
        assert queued[0].status is OpportunityStatus.APPROVED

    async def test_initialization_state_is_persisted_not_inferred_from_a_date(
        self, database: Database, records: list[RawOpportunity]
    ) -> None:
        """Nothing about this depends on the wall clock or a release date."""
        async with database.session() as session:
            assert await SourceRepository(session).is_initialized("stub") is False

        await OpportunityPipeline(database).run(StubCollector(records))

        async with database.session() as session:
            source = await SourceRepository(session).get("stub")
        assert source is not None
        assert source.initialized_at is not None
        assert source.baseline_run_id is not None

    async def test_a_failed_first_run_does_not_consume_the_initial_import(
        self, database: Database, records: list[RawOpportunity]
    ) -> None:
        """Otherwise a transient outage would push the real catalogue at officers."""
        pipeline = OpportunityPipeline(database)
        failed = await pipeline.run(StubCollector(error="upstream is down"))
        assert not failed.succeeded

        async with database.session() as session:
            assert await SourceRepository(session).is_initialized("stub") is False

        stats = await pipeline.run(StubCollector(records))
        assert stats.baseline_import is True

        async with database.session() as session:
            assert await OpportunityRepository(session).list_awaiting_queue(100) == []


class TestOfficerOverride:
    async def test_releasing_baseline_moves_rows_into_the_queue(
        self, database: Database, records: list[RawOpportunity]
    ) -> None:
        await OpportunityPipeline(database).run(StubCollector(records))

        async with database.session() as session:
            released = await OpportunityRepository(session).release_baseline("stub", limit=2)
        assert released == 2

        async with database.session() as session:
            repository = OpportunityRepository(session)
            assert len(await repository.list_awaiting_queue(100)) == 2
            assert await repository.count_baseline("stub") == 1

    async def test_releasing_is_bounded_so_it_cannot_flood_discord(
        self, database: Database
    ) -> None:
        many = [make_raw(application_url=f"https://example.com/{index}") for index in range(20)]
        await OpportunityPipeline(database).run(StubCollector(many))

        async with database.session() as session:
            released = await OpportunityRepository(session).release_baseline("stub", limit=5)
        assert released == 5

    async def test_releasing_a_source_with_no_baseline_is_a_no_op(self, database: Database) -> None:
        async with database.session() as session:
            assert await OpportunityRepository(session).release_baseline("stub", limit=10) == 0

    async def test_resetting_initialization_makes_the_next_run_a_baseline_import_again(
        self, database: Database, records: list[RawOpportunity]
    ) -> None:
        """The recovery path for an initial import that ran against a bad feed."""
        pipeline = OpportunityPipeline(database)
        await pipeline.run(StubCollector(records))

        async with database.session() as session:
            assert await SourceRepository(session).clear_initialization("stub") is True

        stats = await pipeline.run(
            StubCollector([make_raw(application_url="https://example.com/after-reset")])
        )
        assert stats.baseline_import is True

        async with database.session() as session:
            assert await OpportunityRepository(session).list_awaiting_queue(100) == []

    async def test_initialization_is_stamped_once_and_never_rewritten(
        self, database: Database, records: list[RawOpportunity]
    ) -> None:
        pipeline = OpportunityPipeline(database)
        await pipeline.run(StubCollector(records))

        async with database.session() as session:
            first = await SourceRepository(session).get("stub")
            assert first is not None
            stamped_at = first.initialized_at

        await pipeline.run(StubCollector(records))

        async with database.session() as session:
            second = await SourceRepository(session).get("stub")
        assert second is not None
        assert second.initialized_at == stamped_at
