"""Source trust policy.

The property being protected is that auto-publishing is only ever the result of
someone deliberately choosing it for a named source. It is never the default,
never inferred from the source's identity, and never a consequence of deploying.
"""

from __future__ import annotations

from acm_hub.db.repositories.opportunities import OpportunityRepository
from acm_hub.db.repositories.sources import SourceRepository
from acm_hub.db.session import Database
from acm_hub.domain.enums import OpportunityStatus, PublicationPolicy
from acm_hub.pipeline.process import OpportunityPipeline
from tests.conftest import initialize_source, make_raw
from tests.integration.test_pipeline import StubCollector


async def counts(database: Database) -> dict[OpportunityStatus, int]:
    async with database.session() as session:
        return await OpportunityRepository(session).count_by_status()


class TestDefaults:
    async def test_a_newly_registered_source_requires_manual_review(
        self, database: Database
    ) -> None:
        await OpportunityPipeline(database).run(StubCollector([make_raw()]))

        async with database.session() as session:
            source = await SourceRepository(session).get("stub")
        assert source is not None
        assert source.publication_policy is PublicationPolicy.MANUAL_REVIEW

    async def test_an_unregistered_source_reads_as_manual_review(self, database: Database) -> None:
        """A missing row must never be a route to publishing without review."""
        async with database.session() as session:
            policy = await SourceRepository(session).policy("never-seen")
        assert policy is PublicationPolicy.MANUAL_REVIEW

    async def test_re_registering_does_not_reset_a_chosen_policy(self, database: Database) -> None:
        """A redeploy must not silently undo an officer's decision."""
        await initialize_source(database, "stub", policy=PublicationPolicy.AUTO_PUBLISH)

        async with database.session() as session:
            await SourceRepository(session).register("stub", "Renamed Source", "https://new.test")
            source = await SourceRepository(session).get("stub")

        assert source is not None
        assert source.display_name == "Renamed Source"
        assert source.publication_policy is PublicationPolicy.AUTO_PUBLISH


class TestManualReview:
    async def test_findings_stay_undecided_and_reach_the_queue(self, database: Database) -> None:
        await initialize_source(database, "stub", policy=PublicationPolicy.MANUAL_REVIEW)
        await OpportunityPipeline(database).run(StubCollector([make_raw()]))

        async with database.session() as session:
            awaiting = await OpportunityRepository(session).list_awaiting_queue(10)
        assert len(awaiting) == 1
        assert awaiting[0].status is OpportunityStatus.DISCOVERED


class TestAutoPublish:
    async def test_new_findings_are_approved_without_a_review_card(
        self, database: Database
    ) -> None:
        await initialize_source(database, "stub", policy=PublicationPolicy.AUTO_PUBLISH)
        await OpportunityPipeline(database).run(StubCollector([make_raw()]))

        async with database.session() as session:
            repository = OpportunityRepository(session)
            assert await repository.list_awaiting_queue(10) == []
            awaiting_publication = await repository.list_awaiting_publication(10)
        assert len(awaiting_publication) == 1
        assert awaiting_publication[0].status is OpportunityStatus.APPROVED

    async def test_no_review_action_is_recorded_for_a_decision_nobody_made(
        self, database: Database
    ) -> None:
        await initialize_source(database, "stub", policy=PublicationPolicy.AUTO_PUBLISH)
        await OpportunityPipeline(database).run(StubCollector([make_raw()]))

        async with database.session() as session:
            row = (await OpportunityRepository(session).list_awaiting_publication(1))[0]
            assert row.reviews == []

    async def test_an_initial_import_is_never_auto_published(self, database: Database) -> None:
        """Trusting a source's new findings is not trusting its back catalogue."""
        async with database.session() as session:
            await SourceRepository(session).register("stub", "Stub Source", "https://s.test")
            await SourceRepository(session).set_policy("stub", PublicationPolicy.AUTO_PUBLISH)

        records = [make_raw(application_url=f"https://example.com/{i}") for i in range(5)]
        stats = await OpportunityPipeline(database).run(StubCollector(records))

        assert stats.baseline_import is True
        async with database.session() as session:
            assert await OpportunityRepository(session).list_awaiting_publication(10) == []
        assert counts_of(await counts(database), OpportunityStatus.APPROVED) == 0

    async def test_an_already_known_opportunity_is_not_re_approved(
        self, database: Database
    ) -> None:
        await initialize_source(database, "stub", policy=PublicationPolicy.MANUAL_REVIEW)
        pipeline = OpportunityPipeline(database)
        await pipeline.run(StubCollector([make_raw()]))

        async with database.session() as session:
            await SourceRepository(session).set_policy("stub", PublicationPolicy.AUTO_PUBLISH)

        await pipeline.run(StubCollector([make_raw()]))

        async with database.session() as session:
            assert await OpportunityRepository(session).list_awaiting_publication(10) == []


class TestDisabled:
    async def test_a_disabled_source_is_not_contacted_at_all(self, database: Database) -> None:
        await initialize_source(database, "stub", policy=PublicationPolicy.DISABLED)
        collector = StubCollector([make_raw()])

        stats = await OpportunityPipeline(database).run(collector)

        assert stats.skipped is True
        assert collector.call_count == 0
        assert await counts(database) == {}

    async def test_a_disabled_source_records_no_run(self, database: Database) -> None:
        """A source that was never contacted has no outcome to report."""
        from acm_hub.db.repositories.collector_runs import CollectorRunRepository

        await initialize_source(database, "stub", policy=PublicationPolicy.DISABLED)
        await OpportunityPipeline(database).run(StubCollector([make_raw()]))

        async with database.session() as session:
            assert await CollectorRunRepository(session).latest("stub") is None

    async def test_re_enabling_resumes_collection(self, database: Database) -> None:
        await initialize_source(database, "stub", policy=PublicationPolicy.DISABLED)
        pipeline = OpportunityPipeline(database)
        await pipeline.run(StubCollector([make_raw()]))

        async with database.session() as session:
            await SourceRepository(session).set_policy("stub", PublicationPolicy.MANUAL_REVIEW)

        stats = await pipeline.run(StubCollector([make_raw()]))
        assert stats.skipped is False
        assert stats.new == 1


class TestPolicyWrites:
    async def test_setting_a_policy_on_a_missing_source_reports_failure(
        self, database: Database
    ) -> None:
        async with database.session() as session:
            updated = await SourceRepository(session).set_policy("nope", PublicationPolicy.DISABLED)
        assert updated is False


def counts_of(mapping: dict[OpportunityStatus, int], status: OpportunityStatus) -> int:
    return mapping.get(status, 0)
