"""Closure detection and, more importantly, everything that must stop it.

Inferring "this closed" from "the feed did not mention it" is only sound when
the feed promised to be complete and looks complete. The failure mode is
severe and one-directional: a wrongly-inferred mass closure blanks the members'
channel, while a missed closure is one stale post. These tests exist mostly to
prove the sweep declines to run.
"""

from __future__ import annotations

from acm_hub.db.repositories.opportunities import OpportunityRepository
from acm_hub.db.repositories.sources import SourceRepository
from acm_hub.db.session import Database
from acm_hub.domain.enums import OpportunityStatus, ReviewDecision
from acm_hub.domain.source import RawOpportunity
from acm_hub.pipeline.process import ClosureGuard, OpportunityPipeline
from tests.conftest import initialize_source, make_raw
from tests.integration.test_pipeline import StubCollector


def listings(count: int, *, start: int = 0) -> list[RawOpportunity]:
    return [
        make_raw(application_url=f"https://example.com/{index}")
        for index in range(start, start + count)
    ]


async def statuses(database: Database) -> dict[OpportunityStatus, int]:
    async with database.session() as session:
        return await OpportunityRepository(session).count_by_status()


class TestClosuresAreApplied:
    async def test_an_opportunity_absent_from_a_complete_snapshot_is_closed(
        self, database: Database
    ) -> None:
        await initialize_source(database, "stub")
        pipeline = OpportunityPipeline(database)
        await pipeline.run(StubCollector(listings(10), snapshot=True))

        stats = await pipeline.run(StubCollector(listings(9), snapshot=True))

        assert stats.closed == 1
        assert len(stats.closed_ids) == 1
        assert (await statuses(database))[OpportunityStatus.CLOSED] == 1

    async def test_closing_keeps_the_row_and_its_publication_history(
        self, database: Database
    ) -> None:
        """Members must still be able to find what they saw; nothing is deleted."""
        await initialize_source(database, "stub")
        pipeline = OpportunityPipeline(database)
        await pipeline.run(StubCollector(listings(4), snapshot=True))

        async with database.session() as session:
            repository = OpportunityRepository(session)
            row = (await repository.list_awaiting_queue(1))[0]
            await repository.record_decision(
                row.id, decision=ReviewDecision.APPROVED, reviewer_discord_id=1
            )
            await repository.record_publication(row.id, channel_id=99, message_id=1234)

        surviving = [
            record for record in listings(4) if record.application_url != row.application_url
        ]
        await pipeline.run(StubCollector(surviving, snapshot=True))

        async with database.session() as session:
            repository = OpportunityRepository(session)
            refreshed = await repository.get(row.id)
            messages = await repository.publication_messages(row.id)
        assert refreshed is not None
        assert refreshed.status is OpportunityStatus.CLOSED
        assert refreshed.closed_at is not None
        assert messages == [(99, 1234)]

    async def test_a_rejected_opportunity_is_not_closed_by_a_sweep(
        self, database: Database
    ) -> None:
        """An officer's decision outranks upstream churn."""
        await initialize_source(database, "stub")
        pipeline = OpportunityPipeline(database)
        await pipeline.run(StubCollector(listings(4), snapshot=True))

        async with database.session() as session:
            repository = OpportunityRepository(session)
            row = (await repository.list_awaiting_queue(1))[0]
            await repository.record_decision(
                row.id, decision=ReviewDecision.REJECTED, reviewer_discord_id=1
            )

        surviving = [
            record for record in listings(4) if record.application_url != row.application_url
        ]
        await pipeline.run(StubCollector(surviving, snapshot=True))

        async with database.session() as session:
            refreshed = await OpportunityRepository(session).get(row.id)
        assert refreshed is not None
        assert refreshed.status is OpportunityStatus.REJECTED

    async def test_a_reappearing_opportunity_is_reopened(self, database: Database) -> None:
        """Being listed again is direct evidence the closure inference was wrong."""
        await initialize_source(database, "stub")
        pipeline = OpportunityPipeline(database)
        await pipeline.run(StubCollector(listings(10), snapshot=True))
        await pipeline.run(StubCollector(listings(9), snapshot=True))
        assert (await statuses(database))[OpportunityStatus.CLOSED] == 1

        await pipeline.run(StubCollector(listings(10), snapshot=True))

        counts = await statuses(database)
        assert counts.get(OpportunityStatus.CLOSED, 0) == 0
        assert counts[OpportunityStatus.DISCOVERED] == 10


class TestClosuresAreRefused:
    async def test_a_failed_run_closes_nothing(self, database: Database) -> None:
        await initialize_source(database, "stub")
        pipeline = OpportunityPipeline(database)
        await pipeline.run(StubCollector(listings(10), snapshot=True))

        stats = await pipeline.run(StubCollector(error="upstream is down", snapshot=True))

        assert not stats.succeeded
        assert stats.closed == 0
        assert OpportunityStatus.CLOSED not in await statuses(database)

    async def test_an_empty_but_successful_run_closes_nothing(self, database: Database) -> None:
        """A feed that parses to zero records is the classic mass-closure trap."""
        await initialize_source(database, "stub")
        pipeline = OpportunityPipeline(database)
        await pipeline.run(StubCollector(listings(10), snapshot=True))

        stats = await pipeline.run(StubCollector([], snapshot=True))

        assert stats.closed == 0
        assert stats.closure_skipped_reason is not None
        assert OpportunityStatus.CLOSED not in await statuses(database)

    async def test_a_truncated_feed_trips_the_volume_guard(self, database: Database) -> None:
        await initialize_source(database, "stub")
        pipeline = OpportunityPipeline(database)
        await pipeline.run(StubCollector(listings(20), snapshot=True))

        stats = await pipeline.run(StubCollector(listings(4), snapshot=True))

        assert stats.closed == 0
        assert stats.closure_skipped_reason is not None
        assert "below the" in stats.closure_skipped_reason

    async def test_a_plausible_feed_that_would_close_too_much_is_blocked(
        self, database: Database
    ) -> None:
        """Volume looks fine, but the churn does not. Needs a human."""
        await initialize_source(database, "stub")
        pipeline = OpportunityPipeline(
            database, closure_guard=ClosureGuard(min_volume_ratio=0.1, max_close_ratio=0.3)
        )
        await pipeline.run(StubCollector(listings(10), snapshot=True))

        # Same record count, but half of them are different postings.
        stats = await pipeline.run(
            StubCollector([*listings(5), *listings(5, start=100)], snapshot=True)
        )

        assert stats.closed == 0
        assert stats.closure_skipped_reason is not None
        assert "ceiling" in stats.closure_skipped_reason

    async def test_a_collector_that_does_not_promise_a_snapshot_closes_nothing(
        self, database: Database
    ) -> None:
        await initialize_source(database, "stub")
        pipeline = OpportunityPipeline(database)
        await pipeline.run(StubCollector(listings(10), snapshot=False))

        stats = await pipeline.run(StubCollector(listings(1), snapshot=False))

        assert stats.closed == 0
        assert stats.closure_skipped_reason == "collector does not produce a complete snapshot"

    async def test_the_initial_import_closes_nothing(self, database: Database) -> None:
        stats = await OpportunityPipeline(database).run(StubCollector(listings(10), snapshot=True))
        assert stats.closure_skipped_reason == "initial import"

    async def test_closure_detection_can_be_switched_off_entirely(self, database: Database) -> None:
        await initialize_source(database, "stub")
        pipeline = OpportunityPipeline(database, closure_detection_enabled=False)
        await pipeline.run(StubCollector(listings(10), snapshot=True))

        stats = await pipeline.run(StubCollector(listings(9), snapshot=True))

        assert stats.closed == 0
        assert stats.closure_skipped_reason == "closure detection is disabled"

    async def test_a_record_that_stops_parsing_is_not_treated_as_closed(
        self, database: Database
    ) -> None:
        """A validation regression is a bug in us, not a closure upstream."""
        await initialize_source(database, "stub")
        pipeline = OpportunityPipeline(database)
        await pipeline.run(StubCollector(listings(10), snapshot=True))

        # The same posting, still listed under the same URL, but with a title
        # that no longer validates. The collector saw it, so the closure sweep
        # must still count it as present.
        degraded = [
            *listings(9),
            make_raw(application_url="https://example.com/9", title="x" * 400),
        ]
        stats = await pipeline.run(StubCollector(degraded, snapshot=True))

        assert stats.rejected == 1
        assert stats.closed == 0


class TestSnapshotRecording:
    async def test_a_successful_snapshot_run_is_recorded_as_complete(
        self, database: Database
    ) -> None:
        await initialize_source(database, "stub")
        await OpportunityPipeline(database).run(StubCollector(listings(3), snapshot=True))

        async with database.session() as session:
            from acm_hub.db.repositories.collector_runs import CollectorRunRepository

            run = await CollectorRunRepository(session).latest("stub")
        assert run is not None
        assert run.snapshot_complete is True

    async def test_a_failed_run_is_never_recorded_as_a_complete_snapshot(
        self, database: Database
    ) -> None:
        await initialize_source(database, "stub")
        await OpportunityPipeline(database).run(StubCollector(error="boom", snapshot=True))

        async with database.session() as session:
            from acm_hub.db.repositories.collector_runs import CollectorRunRepository

            run = await CollectorRunRepository(session).latest("stub")
        assert run is not None
        assert run.snapshot_complete is False

    async def test_including_inactive_listings_revokes_the_snapshot_promise(self) -> None:
        """The open set is only the open set when closed postings are excluded."""
        from acm_hub.collectors.github.simplify import SimplifyCollector

        assert SimplifyCollector().produces_complete_snapshot is True
        assert SimplifyCollector(include_inactive=True).produces_complete_snapshot is False

    async def test_the_source_row_is_untouched_by_a_closure_sweep(self, database: Database) -> None:
        await initialize_source(database, "stub")
        pipeline = OpportunityPipeline(database)
        await pipeline.run(StubCollector(listings(10), snapshot=True))
        await pipeline.run(StubCollector(listings(9), snapshot=True))

        async with database.session() as session:
            source = await SourceRepository(session).get("stub")
        assert source is not None
        assert source.last_success_at is not None
