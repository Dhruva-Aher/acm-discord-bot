"""The JSON SIG column and the queryable projection must never disagree.

``opportunities.sig_matches`` is what embeds read. ``opportunity_sigs`` is what
search filters on. If they diverge, a member searching for SIGAI would miss a
posting whose embed still shows SIGAI, or the reverse. They are written in the
same upsert so they can only disagree if that method is broken.
"""

from __future__ import annotations

from acm_hub.db.repositories.opportunities import OpportunityRepository, sig_matches_from_json
from acm_hub.db.session import Database
from acm_hub.pipeline.process import OpportunityPipeline
from tests.conftest import initialize_source, make_raw
from tests.integration.test_pipeline import StubCollector


async def stored_pair(database: Database) -> tuple[list[str], list[str]]:
    async with database.session() as session:
        row = (await OpportunityRepository(session).list_awaiting_queue(1))[0]
        json_sigs = [match.sig for match in sig_matches_from_json(row.sig_matches)]
        table_sigs = [entry.sig for entry in row.sigs]
    return json_sigs, table_sigs


class TestSigProjectionConsistency:
    async def test_a_classified_insert_writes_the_same_sigs_to_both_places(
        self, database: Database
    ) -> None:
        await initialize_source(database, "stub")
        await OpportunityPipeline(database).run(
            StubCollector([make_raw(title="Machine Learning Engineer Intern")])
        )

        json_sigs, table_sigs = await stored_pair(database)
        assert json_sigs == ["SIGAI"]
        assert table_sigs == json_sigs

    async def test_a_reclassification_replaces_both_rather_than_appending(
        self, database: Database
    ) -> None:
        await initialize_source(database, "stub")
        pipeline = OpportunityPipeline(database)
        url = "https://example.com/same"
        await pipeline.run(StubCollector([make_raw(application_url=url, title="Data Analyst")]))
        await pipeline.run(
            StubCollector([make_raw(application_url=url, title="Machine Learning Engineer Intern")])
        )

        json_sigs, table_sigs = await stored_pair(database)
        assert json_sigs == table_sigs
        assert json_sigs == ["SIGAI"]
        assert "SIGKDD" not in json_sigs

    async def test_clearing_every_sig_clears_the_projection_too(self, database: Database) -> None:
        await initialize_source(database, "stub")
        pipeline = OpportunityPipeline(database)
        url = "https://example.com/same"
        await pipeline.run(
            StubCollector([make_raw(application_url=url, title="Machine Learning Engineer Intern")])
        )
        await pipeline.run(
            StubCollector([make_raw(application_url=url, title="Marketing Communications Intern")])
        )

        json_sigs, table_sigs = await stored_pair(database)
        assert json_sigs == []
        assert table_sigs == []
