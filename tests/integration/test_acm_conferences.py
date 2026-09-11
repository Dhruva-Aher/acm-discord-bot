"""Official ACM conferences collector.

HTTP is mocked. The happy-path fixture is official-format markup (JSON-LD
Events plus one HTML card) using conference names ACM publishes. The collector
must fail when Cloudflare blocks the official page rather than invent a list.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from acm_hub.collectors.acm.conferences import (
    DEFAULT_CONFERENCES_URL,
    AcmOfficialConferencesCollector,
    parse_official_conferences,
)
from acm_hub.collectors.base import CollectorError
from acm_hub.db.repositories.opportunities import OpportunityRepository
from acm_hub.db.session import Database
from acm_hub.domain.enums import OpportunityType, VerificationStatus
from acm_hub.pipeline.process import OpportunityPipeline
from tests.conftest import initialize_source


@pytest.fixture(autouse=True)
def _mock_http() -> Iterator[None]:
    with respx.mock:
        yield


def _collector(**kwargs: Any) -> AcmOfficialConferencesCollector:
    return AcmOfficialConferencesCollector(max_retries=0, **kwargs)


class TestParseOfficialConferences:
    def test_reads_json_ld_and_html_cards(self, acm_conferences_html: str) -> None:
        records = parse_official_conferences(acm_conferences_html)
        titles = {record.title for record in records}
        assert "ACM Internet Measurement Conference" in titles
        assert "The 2026 ACM/IEEE Joint Conference on Digital Libraries" in titles
        assert "The 28th International ACM SIGACCESS Conference on Computers and Accessibility" in (
            titles
        )

    def test_entries_without_an_official_url_are_skipped(self, acm_conferences_html: str) -> None:
        records = parse_official_conferences(acm_conferences_html)
        assert all(record.application_url.startswith("https://") for record in records)
        assert not any("without an official URL" in record.title for record in records)

    def test_locations_come_from_the_source(self, acm_conferences_html: str) -> None:
        records = {
            record.title: record for record in parse_official_conferences(acm_conferences_html)
        }
        assert records["ACM Internet Measurement Conference"].locations == ("Karlsruhe, Germany",)
        assert records["The 2026 ACM/IEEE Joint Conference on Digital Libraries"].locations == (
            "Frisco, USA",
        )

    def test_no_deadline_is_invented(self, acm_conferences_html: str) -> None:
        for record in parse_official_conferences(acm_conferences_html):
            assert record.expires_at is None
            assert record.description is None

    def test_relative_official_urls_are_resolved(self) -> None:
        html = """
        <html><body>
        <article class="acm-conference">
          <h3><a href="/conference/chi">CHI Conference on Human Factors</a></h3>
          <p class="location">Yokohama, Japan</p>
        </article>
        </body></html>
        """
        records = parse_official_conferences(html)
        assert len(records) == 1
        assert records[0].application_url == "https://www.acm.org/conference/chi"

    def test_records_are_conferences_claimed_by_the_source(self, acm_conferences_html: str) -> None:
        record = parse_official_conferences(acm_conferences_html)[0]
        assert record.opportunity_type is OpportunityType.CONFERENCE
        assert record.verification_status is VerificationStatus.SOURCE_CLAIMED
        assert record.source_name == "acm_official_conferences"


class TestCollect:
    async def test_returns_parseable_official_events(self, acm_conferences_html: str) -> None:
        respx.get(DEFAULT_CONFERENCES_URL).mock(
            return_value=httpx.Response(200, text=acm_conferences_html)
        )
        collector = _collector()
        try:
            records = await collector.collect()
        finally:
            await collector.aclose()
        assert len(records) == 3
        assert all(record.source_name == collector.name for record in records)

    async def test_cloudflare_block_fails_instead_of_inventing_conferences(
        self, acm_cloudflare_html: str
    ) -> None:
        respx.get(DEFAULT_CONFERENCES_URL).mock(
            return_value=httpx.Response(200, text=acm_cloudflare_html)
        )
        collector = _collector()
        try:
            with pytest.raises(CollectorError, match="bot protection"):
                await collector.collect()
        finally:
            await collector.aclose()

    async def test_unrecognised_markup_fails_loudly(self) -> None:
        respx.get(DEFAULT_CONFERENCES_URL).mock(
            return_value=httpx.Response(200, text="<html><body><h1>Conferences</h1></body></html>")
        )
        collector = _collector()
        try:
            with pytest.raises(CollectorError, match="no parseable events"):
                await collector.collect()
        finally:
            await collector.aclose()

    @pytest.mark.parametrize("status_code", [403, 404, 500])
    async def test_http_errors_become_collector_errors(self, status_code: int) -> None:
        respx.get(DEFAULT_CONFERENCES_URL).mock(return_value=httpx.Response(status_code))
        collector = _collector()
        try:
            with pytest.raises(CollectorError, match=f"HTTP {status_code}"):
                await collector.collect()
        finally:
            await collector.aclose()

    async def test_network_failure_becomes_a_collector_error(self) -> None:
        respx.get(DEFAULT_CONFERENCES_URL).mock(side_effect=httpx.ConnectError("blocked"))
        collector = _collector()
        try:
            with pytest.raises(CollectorError, match="could not reach"):
                await collector.collect()
        finally:
            await collector.aclose()

    async def test_just_a_moment_challenge_fails_instead_of_parsing(self) -> None:
        html = (
            "<html><title>Just a moment...</title>"
            "<p>Performance and security by Cloudflare</p></html>"
        )
        respx.get(DEFAULT_CONFERENCES_URL).mock(return_value=httpx.Response(200, text=html))
        collector = _collector()
        try:
            with pytest.raises(CollectorError, match="bot protection"):
                await collector.collect()
        finally:
            await collector.aclose()

    def test_is_not_a_complete_snapshot(self) -> None:
        assert AcmOfficialConferencesCollector.produces_complete_snapshot is False


class TestPipeline:
    async def test_official_conferences_flow_through(
        self, database: Database, acm_conferences_html: str
    ) -> None:
        await initialize_source(database, "acm_official_conferences")
        respx.get(DEFAULT_CONFERENCES_URL).mock(
            return_value=httpx.Response(200, text=acm_conferences_html)
        )
        collector = _collector()
        try:
            stats = await OpportunityPipeline(database).run(collector)
        finally:
            await collector.aclose()

        assert stats.succeeded
        assert stats.fetched == 3

        async with database.session() as session:
            rows = await OpportunityRepository(session).list_awaiting_queue(10)

        assert len(rows) == 3
        assert all(row.opportunity_type is OpportunityType.CONFERENCE for row in rows)
