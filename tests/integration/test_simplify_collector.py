"""Simplify collector parsing and failure behaviour.

Every HTTP call is mocked. These tests must keep passing when GitHub is down or
when the upstream repository changes shape, which is exactly the scenario the
collector's error handling exists for.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from acm_hub.collectors.base import CollectorError, safe_collect
from acm_hub.collectors.github.feeds_config import GitHubListingsFeed
from acm_hub.collectors.github.listings_fetch import (
    GITHUB_RAW_ACCEPT,
    feed_api_url,
    feed_raw_url,
)
from acm_hub.collectors.github.simplify import (
    MalformedListingError,
    SimplifyCollector,
    listing_is_open,
    parse_listing,
)
from acm_hub.collectors.http import fetch_with_retry
from acm_hub.domain.enums import OpportunityType, SponsorshipStatus, VerificationStatus

SUMMER2027_FEED = GitHubListingsFeed(owner="SimplifyJobs", repo="Summer2027-Internships")
SUMMER2027_API = feed_api_url(SUMMER2027_FEED)
SUMMER2027_RAW = feed_raw_url(SUMMER2027_FEED)


@pytest.fixture(autouse=True)
def _mock_http() -> Iterator[None]:
    """Intercept every HTTP call for the whole module.

    Not ``@respx.mock`` on the test classes: used as a class decorator respx
    returns a *function*, so pytest stops collecting the class entirely and the
    tests inside it silently never run.
    """
    with respx.mock:
        yield


async def _noop_sleep(_seconds: float) -> None:
    """Stand-in for asyncio.sleep so retry tests do not wait in real time."""


def _collector(**kwargs: Any) -> SimplifyCollector:
    kwargs.setdefault("feeds", [SUMMER2027_FEED])
    kwargs.setdefault("discover_orgs", ())
    return SimplifyCollector(max_retries=0, **kwargs)


class TestListingIsOpen:
    def test_requires_active_true(self, simplify_listings: list[dict[str, Any]]) -> None:
        open_record = simplify_listings[0]
        closed_record = next(r for r in simplify_listings if r.get("active") is False)
        assert listing_is_open(open_record) is True
        assert listing_is_open(closed_record) is False

    def test_missing_active_is_treated_as_closed(self) -> None:
        assert listing_is_open({"active": None, "is_visible": True}) is False
        assert listing_is_open({"is_visible": True}) is False


class TestParseListing:
    def test_parses_a_real_upstream_record(self, simplify_listings: list[dict[str, Any]]) -> None:
        record = simplify_listings[0]
        raw = parse_listing(record)
        assert raw.title == record["title"]
        assert raw.organization == record["company_name"]
        assert raw.application_url == record["url"]
        assert raw.source_external_id == record["id"]
        assert raw.opportunity_type is OpportunityType.INTERNSHIP

    def test_every_fixture_record_parses(self, simplify_listings: list[dict[str, Any]]) -> None:
        assert len(simplify_listings) == len([parse_listing(r) for r in simplify_listings])

    def test_no_description_is_invented(self, simplify_listings: list[dict[str, Any]]) -> None:
        """The upstream feed has no prose, so neither may we."""
        assert all(parse_listing(record).description is None for record in simplify_listings)

    def test_aggregator_claims_are_labelled_as_such(
        self, simplify_listings: list[dict[str, Any]]
    ) -> None:
        raw = parse_listing(simplify_listings[0])
        assert raw.verification_status is VerificationStatus.SOURCE_CLAIMED

    @pytest.mark.parametrize(
        ("upstream_value", "expected"),
        [
            ("Offers Sponsorship", SponsorshipStatus.OFFERS_SPONSORSHIP),
            ("Does Not Offer Sponsorship", SponsorshipStatus.NO_SPONSORSHIP),
            ("U.S. Citizenship is Required", SponsorshipStatus.CITIZENSHIP_REQUIRED),
            ("Other", SponsorshipStatus.UNKNOWN),
            ("something new upstream added", SponsorshipStatus.UNKNOWN),
            (None, SponsorshipStatus.UNKNOWN),
        ],
    )
    def test_sponsorship_mapping(
        self,
        simplify_listings: list[dict[str, Any]],
        upstream_value: str | None,
        expected: SponsorshipStatus,
    ) -> None:
        """``Other`` is upstream's "unstated", so it must not read as eligible."""
        record = {**simplify_listings[0], "sponsorship": upstream_value}
        assert parse_listing(record).sponsorship is expected

    def test_citizenship_restriction_is_recorded_when_stated(
        self, simplify_listings: list[dict[str, Any]]
    ) -> None:
        record = {**simplify_listings[0], "sponsorship": "U.S. Citizenship is Required"}
        assert parse_listing(record).citizenship_requirement is not None

    def test_no_citizenship_claim_when_unstated(
        self, simplify_listings: list[dict[str, Any]]
    ) -> None:
        record = {**simplify_listings[0], "sponsorship": "Other"}
        assert parse_listing(record).citizenship_requirement is None

    def test_unmapped_category_is_still_passed_to_the_classifier(
        self, simplify_listings: list[dict[str, Any]]
    ) -> None:
        record = {**simplify_listings[0], "category": "Quantum Computing"}
        assert "quantum computing" in parse_listing(record).tags

    def test_timestamps_are_converted_to_utc(self, simplify_listings: list[dict[str, Any]]) -> None:
        raw = parse_listing(simplify_listings[0])
        assert raw.posted_at is not None
        assert raw.posted_at.tzinfo is not None

    @pytest.mark.parametrize("bad_timestamp", [0, -1, "yesterday", None, True])
    def test_unusable_timestamps_become_none_rather_than_a_wrong_date(
        self, simplify_listings: list[dict[str, Any]], bad_timestamp: Any
    ) -> None:
        record = {**simplify_listings[0], "date_posted": bad_timestamp}
        assert parse_listing(record).posted_at is None

    @pytest.mark.parametrize("field", ["title", "company_name", "url"])
    def test_missing_required_field_is_malformed(
        self, simplify_listings: list[dict[str, Any]], field: str
    ) -> None:
        record = {**simplify_listings[0]}
        del record[field]
        with pytest.raises(MalformedListingError):
            parse_listing(record)

    @pytest.mark.parametrize("value", ["", "   ", None, 42, [], {}])
    def test_unusable_required_field_is_malformed(
        self, simplify_listings: list[dict[str, Any]], value: Any
    ) -> None:
        record = {**simplify_listings[0], "url": value}
        with pytest.raises(MalformedListingError):
            parse_listing(record)

    @pytest.mark.parametrize("record", ["a string", 42, None, ["a", "list"]])
    def test_non_object_record_is_malformed(self, record: Any) -> None:
        with pytest.raises(MalformedListingError):
            parse_listing(record)

    def test_locations_of_the_wrong_type_are_malformed(
        self, simplify_listings: list[dict[str, Any]]
    ) -> None:
        record = {**simplify_listings[0], "locations": "San Jose, CA"}
        with pytest.raises(MalformedListingError):
            parse_listing(record)

    def test_non_string_entries_inside_lists_are_dropped(
        self, simplify_listings: list[dict[str, Any]]
    ) -> None:
        record = {**simplify_listings[0], "locations": ["Austin, TX", None, 5, "NYC"]}
        assert parse_listing(record).locations == ("Austin, TX", "NYC")


class TestCollect:
    async def test_returns_only_open_listings(
        self, simplify_listings: list[dict[str, Any]]
    ) -> None:
        respx.get(SUMMER2027_API).mock(
            return_value=httpx.Response(200, json=simplify_listings)
        )
        collector = _collector()
        try:
            records = await collector.collect()
        finally:
            await collector.aclose()

        expected_open = sum(1 for record in simplify_listings if record["active"])
        assert len(records) == expected_open
        assert expected_open < len(simplify_listings), "fixture should contain a closed listing"

    async def test_include_inactive_returns_everything(
        self, simplify_listings: list[dict[str, Any]]
    ) -> None:
        respx.get(SUMMER2027_API).mock(
            return_value=httpx.Response(200, json=simplify_listings)
        )
        collector = _collector(include_inactive=True)
        try:
            records = await collector.collect()
        finally:
            await collector.aclose()
        assert len(records) == len(simplify_listings)

    async def test_a_malformed_record_is_skipped_not_fatal(
        self, simplify_listings: list[dict[str, Any]]
    ) -> None:
        """One bad row upstream must not cost us the whole run."""
        payload = [{"nonsense": True}, *simplify_listings]
        respx.get(SUMMER2027_API).mock(return_value=httpx.Response(200, json=payload))
        collector = _collector()
        try:
            records = await collector.collect()
        finally:
            await collector.aclose()
        assert records

    async def test_a_wholly_changed_format_fails_loudly(self) -> None:
        """If nothing parses, report a failure rather than "0 opportunities"."""
        respx.get(SUMMER2027_API).mock(
            return_value=httpx.Response(200, json=[{"unexpected": 1}, {"unexpected": 2}])
        )
        collector = _collector()
        try:
            with pytest.raises(CollectorError, match="format has probably changed"):
                await collector.collect()
        finally:
            await collector.aclose()

    async def test_non_array_payload_fails(self) -> None:
        respx.get(SUMMER2027_API).mock(
            return_value=httpx.Response(200, json={"listings": []})
        )
        collector = _collector()
        try:
            with pytest.raises(CollectorError, match="expected a JSON array"):
                await collector.collect()
        finally:
            await collector.aclose()

    async def test_invalid_json_fails(self) -> None:
        respx.get(SUMMER2027_API).mock(
            return_value=httpx.Response(200, text="<!DOCTYPE html><html>404</html>")
        )
        collector = _collector()
        try:
            with pytest.raises(CollectorError, match="not valid JSON"):
                await collector.collect()
        finally:
            await collector.aclose()

    async def test_empty_feed_is_not_an_error(self) -> None:
        respx.get(SUMMER2027_API).mock(return_value=httpx.Response(200, json=[]))
        collector = _collector()
        try:
            assert await collector.collect() == []
        finally:
            await collector.aclose()

    @pytest.mark.parametrize("status_code", [404, 500, 503])
    async def test_http_errors_become_collector_errors(self, status_code: int) -> None:
        respx.get(SUMMER2027_API).mock(return_value=httpx.Response(status_code))
        respx.get(SUMMER2027_RAW).mock(return_value=httpx.Response(status_code))
        collector = _collector()
        try:
            with pytest.raises(CollectorError, match=str(status_code)):
                await collector.collect()
        finally:
            await collector.aclose()

    async def test_api_403_falls_back_to_raw_host(
        self, simplify_listings: list[dict[str, Any]]
    ) -> None:
        respx.get(SUMMER2027_API).mock(return_value=httpx.Response(403))
        respx.get(SUMMER2027_RAW).mock(
            return_value=httpx.Response(200, json=simplify_listings)
        )
        collector = _collector()
        try:
            records = await collector.collect()
        finally:
            await collector.aclose()
        assert records

    async def test_network_failure_becomes_a_collector_error(self) -> None:
        respx.get(SUMMER2027_API).mock(
            side_effect=httpx.ConnectError("no route to host")
        )
        respx.get(SUMMER2027_RAW).mock(
            side_effect=httpx.ConnectError("no route to host")
        )
        collector = _collector()
        try:
            with pytest.raises(CollectorError, match="ConnectError"):
                await collector.collect()
        finally:
            await collector.aclose()

    async def test_github_token_is_sent_as_a_bearer_header(
        self, simplify_listings: list[dict[str, Any]]
    ) -> None:
        route = respx.get(SUMMER2027_API).mock(
            return_value=httpx.Response(200, json=simplify_listings)
        )
        collector = _collector(github_token="ghp_example")  # noqa: S106 - fake value
        try:
            await collector.collect()
        finally:
            await collector.aclose()
        assert route.calls.last.request.headers["Authorization"] == "Bearer ghp_example"
        assert route.calls.last.request.headers["Accept"] == GITHUB_RAW_ACCEPT

    async def test_partial_feed_failure_disables_complete_snapshot(
        self, simplify_listings: list[dict[str, Any]]
    ) -> None:
        """One failed feed must not look like the other feed's jobs closed."""
        second_feed = GitHubListingsFeed(owner="SimplifyJobs", repo="New-Grad-Positions")
        respx.get(SUMMER2027_API).mock(return_value=httpx.Response(200, json=simplify_listings))
        respx.get(feed_api_url(second_feed)).mock(return_value=httpx.Response(503))
        respx.get(feed_raw_url(second_feed)).mock(return_value=httpx.Response(503))
        collector = _collector(feeds=[SUMMER2027_FEED, second_feed])
        try:
            records = await collector.collect()
        finally:
            await collector.aclose()
        assert records
        assert collector.produces_complete_snapshot is False

    async def test_duplicate_application_urls_across_feeds_are_merged(
        self, simplify_listings: list[dict[str, Any]]
    ) -> None:
        second_feed = GitHubListingsFeed(owner="SimplifyJobs", repo="New-Grad-Positions")
        respx.get(SUMMER2027_API).mock(return_value=httpx.Response(200, json=simplify_listings[:1]))
        respx.get(feed_api_url(second_feed)).mock(
            return_value=httpx.Response(200, json=[simplify_listings[0]])
        )
        collector = _collector(
            feeds=[SUMMER2027_FEED, second_feed],
        )
        try:
            records = await collector.collect()
        finally:
            await collector.aclose()
        assert len(records) == 1

    async def test_a_custom_url_is_used(self, simplify_listings: list[dict[str, Any]]) -> None:
        url = "https://example.test/listings.json"
        respx.get(url).mock(return_value=httpx.Response(200, json=simplify_listings))
        collector = _collector(listings_url=url, feeds=[SUMMER2027_FEED])
        try:
            assert await collector.collect()
        finally:
            await collector.aclose()


class TestSafeCollect:
    async def test_a_failing_collector_is_reported_not_raised(self) -> None:
        """The isolation boundary: one dead source must not stop the others."""
        respx.get(SUMMER2027_API).mock(return_value=httpx.Response(500))
        collector = _collector()
        try:
            outcome = await safe_collect(collector)
        finally:
            await collector.aclose()

        assert not outcome.succeeded
        assert outcome.error is not None
        assert outcome.records == []
        assert outcome.collector == "simplify_internships"

    async def test_a_successful_collector_reports_its_records(
        self, simplify_listings: list[dict[str, Any]]
    ) -> None:
        respx.get(SUMMER2027_API).mock(
            return_value=httpx.Response(200, json=simplify_listings)
        )
        collector = _collector()
        try:
            outcome = await safe_collect(collector)
        finally:
            await collector.aclose()
        assert outcome.succeeded
        assert outcome.records


class TestFetchWithRetry:
    async def test_retries_a_transient_failure_then_succeeds(self) -> None:
        route = respx.get("https://example.test/data").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(429),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        async with httpx.AsyncClient() as client:
            response = await fetch_with_retry(
                client, "https://example.test/data", max_retries=3, sleep=_noop_sleep
            )
        assert response.json() == {"ok": True}
        assert route.call_count == 3

    async def test_does_not_retry_a_permanent_failure(self) -> None:
        """A 404 means the upstream path moved; retrying only wastes requests."""
        route = respx.get("https://example.test/gone").mock(return_value=httpx.Response(404))
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_with_retry(
                    client, "https://example.test/gone", max_retries=3, sleep=_noop_sleep
                )
        assert route.call_count == 1

    async def test_gives_up_after_the_retry_budget(self) -> None:
        route = respx.get("https://example.test/flaky").mock(return_value=httpx.Response(503))
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_with_retry(
                    client, "https://example.test/flaky", max_retries=2, sleep=_noop_sleep
                )
        assert route.call_count == 3

    async def test_retries_connection_errors(self) -> None:
        route = respx.get("https://example.test/down").mock(
            side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json=[])]
        )
        async with httpx.AsyncClient() as client:
            response = await fetch_with_retry(
                client, "https://example.test/down", max_retries=2, sleep=_noop_sleep
            )
        assert response.status_code == 200
        assert route.call_count == 2


def test_fixture_matches_the_documented_upstream_schema(
    simplify_listings: list[dict[str, Any]],
) -> None:
    """Fails loudly if someone edits the fixture away from the real feed shape."""
    required = {
        "id",
        "company_name",
        "title",
        "url",
        "locations",
        "terms",
        "degrees",
        "category",
        "sponsorship",
        "active",
        "is_visible",
        "date_posted",
        "date_updated",
    }
    for record in simplify_listings:
        assert required <= set(record), f"fixture record missing {required - set(record)}"
    assert json.dumps(simplify_listings)  # fixture must stay JSON-serializable
