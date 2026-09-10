"""Scheduler behaviour: isolation between collectors and overlap protection."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock

from acm_hub.collectors.base import CollectorError
from acm_hub.db.repositories.opportunities import OpportunityRepository
from acm_hub.db.session import Database
from acm_hub.domain.source import RawOpportunity
from acm_hub.pipeline.process import OpportunityPipeline
from acm_hub.publishing.discord import ChannelUnavailableError, OpportunityPublisher
from acm_hub.scheduler.jobs import CollectionScheduler
from tests.conftest import make_raw
from tests.integration.test_pipeline import StubCollector


class SlowCollector(StubCollector):
    """Records overlapping executions so the lock can be observed."""

    name = "slow"
    display_name = "Slow Source"

    def __init__(self) -> None:
        super().__init__([make_raw()])
        self.concurrent_peak = 0
        self._active = 0

    async def collect(self) -> list[RawOpportunity]:
        self._active += 1
        self.concurrent_peak = max(self.concurrent_peak, self._active)
        try:
            await asyncio.sleep(0.05)
            return await super().collect()
        finally:
            self._active -= 1


def build_scheduler(
    database: Database,
    collectors: list[StubCollector],
    publisher: OpportunityPublisher | AsyncMock | None = None,
) -> CollectionScheduler:
    return CollectionScheduler(
        pipeline=OpportunityPipeline(database),
        publisher=cast(OpportunityPublisher, publisher or AsyncMock()),
        collectors=list(collectors),
    )


class TestCollectorIsolation:
    async def test_one_failing_collector_does_not_stop_the_others(self, database: Database) -> None:
        failing = StubCollector(error="upstream is down")
        failing.name = "failing"
        failing.display_name = "Failing Source"
        working = StubCollector([make_raw(application_url="https://example.com/ok")])

        results = await build_scheduler(database, [failing, working]).run_all_collectors()

        assert len(results) == 2
        assert not results[0].succeeded
        assert results[1].succeeded

        async with database.session() as session:
            rows = await OpportunityRepository(session).list_awaiting_queue(10)
        assert len(rows) == 1

    async def test_an_unexpected_crash_is_contained(self, database: Database) -> None:
        """Even a bug inside a collector must not take the scheduler down."""

        class ExplodingCollector(StubCollector):
            name = "exploding"
            display_name = "Exploding Source"

            async def collect(self) -> list[RawOpportunity]:
                raise ZeroDivisionError("a genuine bug, not a source failure")

        results = await build_scheduler(
            database, [ExplodingCollector(), StubCollector([make_raw()])]
        ).run_all_collectors()

        assert not results[0].succeeded
        assert results[1].succeeded

    async def test_every_collector_is_attempted(self, database: Database) -> None:
        first = StubCollector(error="down")
        first.name = "first"
        second = StubCollector([make_raw()])
        second.name = "second"

        await build_scheduler(database, [first, second]).run_all_collectors()

        assert first.call_count == 1
        assert second.call_count == 1


class TestOverlapProtection:
    async def test_concurrent_cycles_do_not_overlap(self, database: Database) -> None:
        """A manual sync during a scheduled run must queue, not race."""
        collector = SlowCollector()
        scheduler = build_scheduler(database, [collector])

        await asyncio.gather(scheduler.run_all_collectors(), scheduler.run_all_collectors())

        assert collector.concurrent_peak == 1
        assert collector.call_count == 2

    async def test_overlapping_cycles_still_store_exactly_one_record(
        self, database: Database
    ) -> None:
        scheduler = build_scheduler(database, [SlowCollector()])
        await asyncio.gather(scheduler.run_all_collectors(), scheduler.run_all_collectors())

        async with database.session() as session:
            counts = await OpportunityRepository(session).count_by_status()
        assert sum(counts.values()) == 1


class TestReviewQueueing:
    async def test_reviews_are_queued_after_collection(self, database: Database) -> None:
        publisher = AsyncMock()
        scheduler = build_scheduler(database, [StubCollector([make_raw()])], publisher)
        await scheduler.run_all_collectors()
        publisher.queue_pending_reviews.assert_awaited_once()

    async def test_an_unavailable_review_channel_does_not_fail_the_cycle(
        self, database: Database
    ) -> None:
        publisher = AsyncMock()
        publisher.queue_pending_reviews.side_effect = ChannelUnavailableError("channel 1 missing")

        results = await build_scheduler(
            database, [StubCollector([make_raw()])], publisher
        ).run_all_collectors()

        assert results[0].succeeded

        async with database.session() as session:
            rows = await OpportunityRepository(session).list_awaiting_queue(10)
        assert len(rows) == 1, "the opportunity is stored and can be queued later"


class TestLifecycle:
    async def test_the_scheduler_starts_and_stops(self, database: Database) -> None:
        scheduler = build_scheduler(database, [StubCollector([])])
        assert not scheduler.running

        scheduler.start(internship_interval_minutes=180)
        assert scheduler.running

        await scheduler.shutdown()
        assert not scheduler.running

    async def test_shutdown_is_safe_when_never_started(self, database: Database) -> None:
        await build_scheduler(database, []).shutdown()

    async def test_a_collector_error_is_surfaced_in_the_stats(self, database: Database) -> None:
        collector = StubCollector(error="boom")
        results = await build_scheduler(database, [collector]).run_all_collectors()
        assert isinstance(CollectorError(collector.name, "boom"), Exception)
        assert results[0].error is not None
