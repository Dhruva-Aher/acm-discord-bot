"""End-to-end pipeline behaviour against a real (SQLite) database."""

from __future__ import annotations

from typing import Any

import httpx
import respx

from acm_hub.collectors.base import Collector, CollectorError
from acm_hub.collectors.github.simplify import DEFAULT_LISTINGS_URL, SimplifyCollector
from acm_hub.db.repositories.collector_runs import CollectorRunRepository
from acm_hub.db.repositories.opportunities import OpportunityRepository
from acm_hub.db.session import Database
from acm_hub.domain.enums import CollectorRunStatus, OpportunityStatus
from acm_hub.domain.source import RawOpportunity
from acm_hub.pipeline.process import OpportunityPipeline
from tests.conftest import make_raw


class StubCollector(Collector):
    """A collector with no network, so pipeline tests control their input exactly."""

    name = "stub"
    display_name = "Stub Source"
    source_url = "https://example.test/source"

    def __init__(self, records: list[RawOpportunity] | None = None, error: str | None = None):
        self._records = records or []
        self._error = error
        self.call_count = 0

    async def collect(self) -> list[RawOpportunity]:
        self.call_count += 1
        if self._error:
            raise CollectorError(self.name, self._error)
        return self._records


class TestPipelinePersistence:
    async def test_stores_new_opportunities(self, database: Database) -> None:
        collector = StubCollector(
            [
                make_raw(application_url="https://example.com/a"),
                make_raw(application_url="https://example.com/b"),
            ]
        )
        stats = await OpportunityPipeline(database).run(collector)

        assert stats.succeeded
        assert stats.fetched == 2
        assert stats.new == 2

        async with database.session() as session:
            counts = await OpportunityRepository(session).count_by_status()
        assert counts[OpportunityStatus.DISCOVERED] == 2

    async def test_a_second_identical_run_stores_nothing_new(self, database: Database) -> None:
        """The core anti-duplicate guarantee, exercised across two full runs."""
        records = [make_raw(application_url="https://example.com/a")]
        pipeline = OpportunityPipeline(database)

        first = await pipeline.run(StubCollector(records))
        second = await pipeline.run(StubCollector(records))

        assert first.new == 1
        assert second.new == 0
        assert second.unchanged == 1

        async with database.session() as session:
            counts = await OpportunityRepository(session).count_by_status()
        assert sum(counts.values()) == 1

    async def test_a_changed_posting_is_detected_and_updated(self, database: Database) -> None:
        pipeline = OpportunityPipeline(database)
        await pipeline.run(StubCollector([make_raw(application_url="https://example.com/a")]))

        stats = await pipeline.run(
            StubCollector(
                [
                    make_raw(
                        application_url="https://example.com/a",
                        title="Senior Software Engineering Intern",
                    )
                ]
            )
        )

        assert stats.updated == 1
        assert stats.new == 0

        async with database.session() as session:
            row = await OpportunityRepository(session).get_by_canonical_key(
                (await OpportunityRepository(session).list_awaiting_queue(1))[0].canonical_key
            )
        assert row is not None
        assert row.title == "Senior Software Engineering Intern"

    async def test_duplicates_within_one_batch_are_collapsed(self, database: Database) -> None:
        collector = StubCollector(
            [
                make_raw(application_url="https://example.com/a", locations=("Austin, TX",)),
                make_raw(application_url="https://example.com/a?utm_source=x", locations=("NYC",)),
            ]
        )
        stats = await OpportunityPipeline(database).run(collector)

        assert stats.duplicates == 1
        assert stats.new == 1

        async with database.session() as session:
            rows = await OpportunityRepository(session).list_awaiting_queue(10)
        assert sorted(rows[0].locations) == ["Austin, TX", "NYC"]

    async def test_invalid_records_are_rejected_and_counted(self, database: Database) -> None:
        collector = StubCollector(
            [
                make_raw(application_url="https://example.com/good"),
                make_raw(application_url="ftp://example.com/bad"),
            ]
        )
        stats = await OpportunityPipeline(database).run(collector)

        assert stats.rejected == 1
        assert stats.new == 1

    async def test_sig_classification_is_persisted(self, database: Database) -> None:
        collector = StubCollector([make_raw(title="Machine Learning Engineer Intern")])
        await OpportunityPipeline(database).run(collector)

        async with database.session() as session:
            rows = await OpportunityRepository(session).list_awaiting_queue(10)
        assert [entry["sig"] for entry in rows[0].sig_matches] == ["SIGAI"]

    async def test_eligibility_stays_unknown_when_the_source_is_silent(
        self, database: Database
    ) -> None:
        await OpportunityPipeline(database).run(StubCollector([make_raw()]))
        async with database.session() as session:
            rows = await OpportunityRepository(session).list_awaiting_queue(10)
        assert rows[0].sponsorship.value == "unknown"
        assert rows[0].citizenship_requirement is None


class TestPipelineRunRecords:
    async def test_a_successful_run_is_recorded(self, database: Database) -> None:
        await OpportunityPipeline(database).run(StubCollector([make_raw()]))

        async with database.session() as session:
            run = await CollectorRunRepository(session).latest("stub")
        assert run is not None
        assert run.status is CollectorRunStatus.SUCCESS
        assert run.records_fetched == 1
        assert run.records_new == 1
        assert run.error is None

    async def test_a_failed_collector_is_recorded_without_raising(self, database: Database) -> None:
        stats = await OpportunityPipeline(database).run(
            StubCollector(error="upstream returned HTTP 500")
        )

        assert not stats.succeeded
        assert stats.error is not None

        async with database.session() as session:
            run = await CollectorRunRepository(session).latest("stub")
        assert run is not None
        assert run.status is CollectorRunStatus.FAILED
        assert run.error is not None and "500" in run.error

    async def test_the_source_is_registered_on_first_run(self, database: Database) -> None:
        await OpportunityPipeline(database).run(StubCollector([make_raw()]))

        async with database.session() as session:
            run_repo = CollectorRunRepository(session)
            runs = await run_repo.latest_per_collector()
        assert [run.collector for run in runs] == ["stub"]

    async def test_a_failed_run_does_not_stamp_source_success(self, database: Database) -> None:
        from acm_hub.db import models

        await OpportunityPipeline(database).run(StubCollector(error="boom"))
        async with database.session() as session:
            source = await session.get(models.Source, "stub")
        assert source is not None
        assert source.last_success_at is None


@respx.mock
class TestPipelineWithTheRealCollector:
    async def test_the_upstream_fixture_flows_through_every_stage(
        self, database: Database, simplify_listings: list[dict[str, Any]]
    ) -> None:
        """Real upstream records, mocked transport, full pipeline."""
        respx.get(DEFAULT_LISTINGS_URL).mock(
            return_value=httpx.Response(200, json=simplify_listings)
        )
        collector = SimplifyCollector(max_retries=0)
        try:
            stats = await OpportunityPipeline(database).run(collector)
        finally:
            await collector.aclose()

        assert stats.succeeded
        expected_open = sum(1 for record in simplify_listings if record["active"])
        assert stats.fetched == expected_open
        assert stats.new == stats.valid

        async with database.session() as session:
            rows = await OpportunityRepository(session).list_awaiting_queue(100)

        assert len(rows) == stats.new
        assert all(row.application_url.startswith("http") for row in rows)
        assert all(row.status is OpportunityStatus.DISCOVERED for row in rows)
        # Every listing in the fixture is an AI, software, hardware, product or
        # quant internship, so at least some must classify into a SIG.
        assert any(row.sig_matches for row in rows)

    async def test_github_being_down_records_a_failure_and_stores_nothing(
        self, database: Database
    ) -> None:
        respx.get(DEFAULT_LISTINGS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
        collector = SimplifyCollector(max_retries=0)
        try:
            stats = await OpportunityPipeline(database).run(collector)
        finally:
            await collector.aclose()

        assert not stats.succeeded
        async with database.session() as session:
            assert await OpportunityRepository(session).count_by_status() == {}
