"""ASU Ready-to-mentor collector.

HTTP is mocked. The fixture is a trimmed capture of the official Fulton Forge
mentors page: three Ready-to-mentor cards and two expo-only mentors. Expo
mentors must never become research opportunities.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from acm_hub.collectors.asu.ready_to_mentor import (
    DEFAULT_MENTORS_URL,
    AsuReadyToMentorCollector,
    MalformedMentorError,
    parse_mentor_card,
    parse_ready_mentor_page,
)
from acm_hub.collectors.base import CollectorError
from acm_hub.db.repositories.opportunities import OpportunityRepository
from acm_hub.db.session import Database
from acm_hub.domain.enums import OpportunityStatus, OpportunityType, VerificationStatus
from acm_hub.pipeline.process import OpportunityPipeline
from tests.conftest import initialize_source


@pytest.fixture(autouse=True)
def _mock_http() -> Iterator[None]:
    with respx.mock:
        yield


def _collector(**kwargs: Any) -> AsuReadyToMentorCollector:
    return AsuReadyToMentorCollector(max_retries=0, **kwargs)


class TestParseReadyMentorPage:
    def test_reads_only_ready_to_mentor_cards(self, asu_mentors_html: str) -> None:
        cards = parse_ready_mentor_page(asu_mentors_html)
        names = {card["name"] for card in cards}
        assert names == {"Ruben Acuna", "Aman Arora", "Maria Elena Chavez Echeagaray"}
        assert "Robert Atkinson" not in names
        assert "Christopher Bailey" not in names

    def test_expo_section_is_not_an_empty_ready_section(self, asu_mentors_html: str) -> None:
        """A page that still has expo mentors must not look like nobody is recruiting."""
        assert "Mentors from the current expo" in asu_mentors_html
        assert parse_ready_mentor_page(asu_mentors_html)


class TestParseMentorCard:
    def test_faculty_name_is_the_organization(self, asu_mentors_html: str) -> None:
        card = parse_ready_mentor_page(asu_mentors_html)[0]
        raw = parse_mentor_card(card)
        assert raw.organization == "Ruben Acuna"
        assert raw.title == "Ready to mentor: Ruben Acuna"
        assert raw.opportunity_type is OpportunityType.RESEARCH
        assert (
            raw.application_url == "https://forge.engineering.asu.edu/faculty_mentor/ruben-acuna/"
        )
        assert raw.source_external_id == "racuna1"

    def test_stated_interests_are_kept_and_not_embellished(self, asu_mentors_html: str) -> None:
        cards = {card["name"]: card for card in parse_ready_mentor_page(asu_mentors_html)}
        raw = parse_mentor_card(cards["Aman Arora"])
        assert raw.description is not None
        assert raw.description.startswith("Using machine learning for chip design")
        assert "Listed for: FURI, MORE" in raw.description
        assert "looking for students" not in raw.description.lower()

    def test_department_is_the_location_when_the_page_states_it(
        self, asu_mentors_html: str
    ) -> None:
        cards = {card["name"]: card for card in parse_ready_mentor_page(asu_mentors_html)}
        raw = parse_mentor_card(cards["Aman Arora"])
        assert raw.locations == ("School of Computing and Augmented Intelligence",)

    def test_missing_department_falls_back_to_the_stated_school(
        self, asu_mentors_html: str
    ) -> None:
        cards = {card["name"]: card for card in parse_ready_mentor_page(asu_mentors_html)}
        raw = parse_mentor_card(cards["Ruben Acuna"])
        assert raw.locations == ("Ira A. Fulton Schools of Engineering",)

    def test_html_entities_are_decoded(self, asu_mentors_html: str) -> None:
        cards = {card["name"]: card for card in parse_ready_mentor_page(asu_mentors_html)}
        raw = parse_mentor_card(cards["Maria Elena Chavez Echeagaray"])
        assert raw.locations == ("ENGR Academic & Student Affrs",)
        assert raw.description is not None
        assert "…" in raw.description or "..." in raw.description

    def test_programs_become_tags_not_degree_requirements(self, asu_mentors_html: str) -> None:
        cards = {card["name"]: card for card in parse_ready_mentor_page(asu_mentors_html)}
        raw = parse_mentor_card(cards["Maria Elena Chavez Echeagaray"])
        assert "furi" in raw.tags
        assert "more" not in raw.tags
        assert raw.degree_requirements == ()

    def test_claim_is_labelled_source_claimed(self, asu_mentors_html: str) -> None:
        raw = parse_mentor_card(parse_ready_mentor_page(asu_mentors_html)[0])
        assert raw.verification_status is VerificationStatus.SOURCE_CLAIMED

    def test_missing_name_is_malformed(self) -> None:
        with pytest.raises(MalformedMentorError):
            parse_mentor_card(
                {"name": "", "href": "https://forge.engineering.asu.edu/faculty_mentor/x/"}
            )


class TestCollect:
    async def test_returns_ready_mentors_and_skips_expo(self, asu_mentors_html: str) -> None:
        respx.get(DEFAULT_MENTORS_URL).mock(return_value=httpx.Response(200, text=asu_mentors_html))
        collector = _collector()
        try:
            records = await collector.collect()
        finally:
            await collector.aclose()

        names = {record.organization for record in records}
        assert names == {"Ruben Acuna", "Aman Arora", "Maria Elena Chavez Echeagaray"}
        assert all(record.source_name == collector.name for record in records)

    async def test_empty_ready_section_is_a_valid_snapshot(self) -> None:
        html = """
        <html><body>
        <h2>Ready to mentor</h2>
        <div class="mentor-ready-block"></div>
        <h2>Mentors from the current expo</h2>
        <div class="uds-person-profile"><h3 class="person-name">
        <a href="https://forge.engineering.asu.edu/faculty_mentor/expo-only/">Expo Only</a>
        </h3></div>
        </body></html>
        """
        respx.get(DEFAULT_MENTORS_URL).mock(return_value=httpx.Response(200, text=html))
        collector = _collector()
        try:
            assert await collector.collect() == []
        finally:
            await collector.aclose()

    async def test_missing_ready_section_fails_loudly(self) -> None:
        respx.get(DEFAULT_MENTORS_URL).mock(
            return_value=httpx.Response(200, text="<html><body>Unrelated page</body></html>")
        )
        collector = _collector()
        try:
            with pytest.raises(CollectorError, match="Ready to mentor"):
                await collector.collect()
        finally:
            await collector.aclose()

    async def test_bot_protection_is_not_parsed_as_an_empty_list(self) -> None:
        respx.get(DEFAULT_MENTORS_URL).mock(
            return_value=httpx.Response(
                200, text="<html><h1>Sorry, you have been blocked</h1></html>"
            )
        )
        collector = _collector()
        try:
            with pytest.raises(CollectorError, match="bot-protection"):
                await collector.collect()
        finally:
            await collector.aclose()

    async def test_wholly_unreadable_cards_fail_loudly(self) -> None:
        html = """
        <html><body>
        <h2>Ready to mentor</h2>
        <div class="mentor-card ready-mentor" data-asurite="x"></div>
        </body></html>
        """
        respx.get(DEFAULT_MENTORS_URL).mock(return_value=httpx.Response(200, text=html))
        collector = _collector()
        try:
            with pytest.raises(CollectorError, match="failed to parse"):
                await collector.collect()
        finally:
            await collector.aclose()

    @pytest.mark.parametrize("status_code", [404, 500, 503])
    async def test_http_errors_become_collector_errors(self, status_code: int) -> None:
        respx.get(DEFAULT_MENTORS_URL).mock(return_value=httpx.Response(status_code))
        collector = _collector()
        try:
            with pytest.raises(CollectorError, match=f"HTTP {status_code}"):
                await collector.collect()
        finally:
            await collector.aclose()

    async def test_network_failure_becomes_a_collector_error(self) -> None:
        respx.get(DEFAULT_MENTORS_URL).mock(side_effect=httpx.ConnectError("no route"))
        collector = _collector()
        try:
            with pytest.raises(CollectorError, match="could not reach"):
                await collector.collect()
        finally:
            await collector.aclose()

    async def test_a_custom_url_is_used(self, asu_mentors_html: str) -> None:
        url = "https://example.test/mentors/"
        respx.get(url).mock(return_value=httpx.Response(200, text=asu_mentors_html))
        collector = _collector(mentors_url=url)
        try:
            assert await collector.collect()
        finally:
            await collector.aclose()

    def test_complete_snapshot_is_on(self) -> None:
        assert AsuReadyToMentorCollector.produces_complete_snapshot is True


class TestPipeline:
    async def test_ready_mentors_flow_through_as_research(
        self, database: Database, asu_mentors_html: str
    ) -> None:
        await initialize_source(database, "asu_ready_to_mentor")
        respx.get(DEFAULT_MENTORS_URL).mock(return_value=httpx.Response(200, text=asu_mentors_html))
        collector = _collector()
        try:
            stats = await OpportunityPipeline(database).run(collector)
        finally:
            await collector.aclose()

        assert stats.succeeded
        assert stats.fetched == 3
        assert stats.new == 3

        async with database.session() as session:
            rows = await OpportunityRepository(session).list_awaiting_queue(10)

        assert {row.organization for row in rows} == {
            "Ruben Acuna",
            "Aman Arora",
            "Maria Elena Chavez Echeagaray",
        }
        assert all(row.opportunity_type is OpportunityType.RESEARCH for row in rows)
        assert all(row.status is OpportunityStatus.DISCOVERED for row in rows)
        assert all(
            row.application_url.startswith("https://forge.engineering.asu.edu/") for row in rows
        )
