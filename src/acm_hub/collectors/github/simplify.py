"""GitHub CS internship and new-grad collectors (SimplifyJobs-style JSON feeds).

ACM Hub reads machine-readable ``listings.json`` files from one or more public
GitHub repositories, merges them into a single open snapshot, and drops rows
upstream marks closed (``active: false`` or ``is_visible: false``). The same
application URL appearing in multiple feeds is ingested once so closure stays
consistent.
"""

from __future__ import annotations

from typing import Any, Final

import httpx

from acm_hub.collectors.base import Collector, CollectorError
from acm_hub.collectors.github.discovery import discover_org_listing_feeds
from acm_hub.collectors.github.feeds_config import (
    DEFAULT_INTERNSHIP_GITHUB_FEEDS,
    GitHubListingsFeed,
    dedupe_feeds,
    parse_internship_github_feeds,
)
from acm_hub.collectors.github.listings_fetch import fetch_listings_payload
from acm_hub.collectors.http import build_client
from acm_hub.domain.enums import OpportunityType, SponsorshipStatus, VerificationStatus
from acm_hub.domain.source import RawOpportunity
from acm_hub.observability.logging import get_logger
from acm_hub.pipeline.normalize import normalize_url

logger = get_logger(__name__)

DEFAULT_LISTINGS_API_URL: Final = (
    "https://api.github.com/repos/SimplifyJobs/Summer2027-Internships/"
    "contents/.github/scripts/listings.json?ref=dev"
)
DEFAULT_LISTINGS_RAW_URL: Final = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/"
    ".github/scripts/listings.json"
)
DEFAULT_LISTINGS_URL: Final = DEFAULT_LISTINGS_API_URL
LISTINGS_SOURCE_URL: Final = (
    "https://github.com/SimplifyJobs/Summer2027-Internships/blob/dev/"
    ".github/scripts/listings.json"
)
REPOSITORY_URL: Final = "https://github.com/SimplifyJobs/Summer2027-Internships"
GITHUB_RAW_ACCEPT: Final = "application/vnd.github.raw+json"
GITHUB_API_VERSION: Final = "2022-11-28"

COLLECTOR_NAME: Final = "simplify_internships"
DISPLAY_NAME: Final = "GitHub CS job feeds (SimplifyJobs)"

SPONSORSHIP_MAP: Final[dict[str, SponsorshipStatus]] = {
    "offers sponsorship": SponsorshipStatus.OFFERS_SPONSORSHIP,
    "does not offer sponsorship": SponsorshipStatus.NO_SPONSORSHIP,
    "u.s. citizenship is required": SponsorshipStatus.CITIZENSHIP_REQUIRED,
    "us citizenship is required": SponsorshipStatus.CITIZENSHIP_REQUIRED,
    "other": SponsorshipStatus.UNKNOWN,
}

CATEGORY_TAGS: Final[dict[str, tuple[str, ...]]] = {
    "ai/ml/data": ("artificial intelligence", "machine learning", "data science"),
    "data science, ai & machine learning": (
        "data science",
        "machine learning",
        "artificial intelligence",
    ),
    "software": ("software engineering",),
    "software engineering": ("software engineering",),
    "hardware": ("hardware engineering",),
    "hardware engineering": ("hardware engineering",),
    "product": ("product management",),
    "product management": ("product management",),
    "quant": ("quantitative finance", "algorithms"),
    "quantitative finance": ("quantitative finance", "algorithms"),
}


class MalformedListingError(ValueError):
    """One upstream record could not be parsed; the rest of the run continues."""


def _require_str(record: dict[str, Any], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MalformedListingError(f"missing or non-string {key!r}")
    return value.strip()


def _optional_str_list(record: dict[str, Any], key: str) -> tuple[str, ...]:
    value = record.get(key)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise MalformedListingError(f"{key!r} must be a list, got {type(value).__name__}")
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _optional_timestamp(record: dict[str, Any], key: str):
    from datetime import UTC, datetime

    value = record.get(key)
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, int | float):
        return None
    if value <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _category_tags(category: str) -> tuple[str, ...]:
    normalized = category.strip().lower()
    if not normalized:
        return ()
    return CATEGORY_TAGS.get(normalized, (normalized,))


def listing_is_open(record: dict[str, Any]) -> bool:
    """Whether upstream explicitly marks a posting as open and visible."""
    if record.get("is_visible") is False:
        return False
    return record.get("active") is True


def parse_listing(record: Any, *, feed: GitHubListingsFeed | None = None) -> RawOpportunity:
    """Convert one upstream JSON object into a :class:`RawOpportunity`."""
    if not isinstance(record, dict):
        raise MalformedListingError(f"expected an object, got {type(record).__name__}")

    active_feed = feed or GitHubListingsFeed(
        owner="SimplifyJobs", repo="Summer2027-Internships", label="Summer 2027 internships"
    )
    title = _require_str(record, "title")
    company = _require_str(record, "company_name")
    application_url = _require_str(record, "url")

    external_id = record.get("id")
    source_external_id = (
        external_id.strip() if isinstance(external_id, str) and external_id.strip() else None
    )

    category = record.get("category")
    category_text = category if isinstance(category, str) else ""

    sponsorship_raw = record.get("sponsorship")
    sponsorship = SPONSORSHIP_MAP.get(
        sponsorship_raw.strip().lower() if isinstance(sponsorship_raw, str) else "",
        SponsorshipStatus.UNKNOWN,
    )
    citizenship_requirement = (
        "U.S. citizenship required (per source)"
        if sponsorship is SponsorshipStatus.CITIZENSHIP_REQUIRED
        else None
    )

    terms = _optional_str_list(record, "terms")
    active = record.get("active")
    opportunity_type = active_feed.inferred_opportunity_type()
    extra_tags: tuple[str, ...] = ()
    if opportunity_type is OpportunityType.PROGRAM:
        extra_tags = ("new grad",)

    return RawOpportunity(
        source_name=COLLECTOR_NAME,
        source_external_id=source_external_id,
        source_url=active_feed.source_page_url,
        application_url=application_url,
        title=title,
        organization=company,
        description=None,
        opportunity_type=opportunity_type,
        locations=_optional_str_list(record, "locations"),
        tags=tuple(
            dict.fromkeys(
                (
                    *_category_tags(category_text),
                    *(t.lower() for t in terms),
                    *extra_tags,
                    f"github:{active_feed.slug}",
                )
            )
        ),
        sponsorship=sponsorship,
        citizenship_requirement=citizenship_requirement,
        degree_requirements=_optional_str_list(record, "degrees"),
        posted_at=_optional_timestamp(record, "date_posted"),
        is_active=active if isinstance(active, bool) else None,
        verification_status=VerificationStatus.SOURCE_CLAIMED,
        extra={
            "upstream_source": str(record.get("source", "")),
            "github_feed": active_feed.slug,
            "github_feed_label": active_feed.label or active_feed.repo,
        },
    )


class SimplifyCollector(Collector):
    """Reads open CS internships and new-grad roles from GitHub listing feeds."""

    name = COLLECTOR_NAME
    display_name = DISPLAY_NAME
    source_url = LISTINGS_SOURCE_URL
    produces_complete_snapshot = True

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        listings_url: str | None = None,
        feeds: list[GitHubListingsFeed] | None = None,
        discover_orgs: tuple[str, ...] = (),
        github_token: str | None = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        include_inactive: bool = False,
    ) -> None:
        client_headers: dict[str, str] = {}
        if github_token:
            client_headers["Authorization"] = f"Bearer {github_token}"
        self._owns_client = client is None
        self._client = client or build_client(
            timeout_seconds=timeout_seconds, headers=client_headers or None
        )
        self._listings_url = listings_url
        self._configured_feeds = feeds or parse_internship_github_feeds(
            DEFAULT_INTERNSHIP_GITHUB_FEEDS
        )
        self._discover_orgs = discover_orgs
        self._resolved_feeds: list[GitHubListingsFeed] | None = None
        self._max_retries = max_retries
        self._include_inactive = include_inactive
        if include_inactive:
            self.produces_complete_snapshot = False

    async def collect(self) -> list[RawOpportunity]:
        # Reset each run: a prior partial failure must not permanently disable
        # closure detection, and a partial run must not look like a full snapshot.
        self.produces_complete_snapshot = not self._include_inactive

        feeds = await self._feeds_for_run()
        if not feeds and self._listings_url is None:
            raise CollectorError(self.name, "no GitHub internship feeds are configured")

        opportunities: list[RawOpportunity] = []
        seen_application_urls: set[str] = set()
        total_records = 0
        skipped_malformed = 0
        skipped_inactive = 0
        feed_errors: list[str] = []

        for feed in feeds:
            try:
                payload = await fetch_listings_payload(
                    self._client,
                    feed,
                    max_retries=self._max_retries,
                    single_url=self._listings_url,
                )
            except httpx.HTTPError as exc:
                feed_errors.append(f"{feed.slug}: {type(exc).__name__}: {exc}")
                continue
            except (RuntimeError, ValueError) as exc:
                feed_errors.append(f"{feed.slug}: {exc}")
                continue

            if not isinstance(payload, list):
                feed_errors.append(
                    f"{feed.slug}: expected a JSON array, got {type(payload).__name__}"
                )
                continue

            total_records += len(payload)
            for record in payload:
                if not isinstance(record, dict):
                    skipped_malformed += 1
                    continue
                try:
                    raw = parse_listing(record, feed=feed)
                except MalformedListingError as exc:
                    skipped_malformed += 1
                    logger.debug("github_listings.record_skipped", feed=feed.slug, reason=str(exc))
                    continue
                if not self._include_inactive and not listing_is_open(record):
                    skipped_inactive += 1
                    continue

                normalized = normalize_url(raw.application_url) or raw.application_url
                if normalized in seen_application_urls:
                    continue
                seen_application_urls.add(normalized)
                opportunities.append(raw)

        if feed_errors and not opportunities:
            hint = (
                " Set GITHUB_TOKEN in .env (no scopes needed) if GitHub limits "
                "are blocking feeds."
            )
            raise CollectorError(self.name, "; ".join(feed_errors) + hint)

        if feed_errors:
            # One dead feed would look like mass closures on the jobs that feed
            # alone provides. Skip the closure sweep for this run.
            self.produces_complete_snapshot = False
            logger.warning(
                "github_listings.partial_feed_failure",
                feed_errors=feed_errors,
                usable=len(opportunities),
            )

        if total_records and not opportunities and skipped_malformed >= total_records:
            raise CollectorError(
                self.name,
                f"every one of {total_records} upstream records failed to parse; "
                "the feed format has probably changed",
            )

        logger.info(
            "github_listings.parsed",
            collector=self.name,
            feeds=len(feeds),
            total_records=total_records,
            usable=len(opportunities),
            skipped_malformed=skipped_malformed,
            skipped_inactive=skipped_inactive,
            feed_errors=len(feed_errors),
        )
        return opportunities

    async def _feeds_for_run(self) -> list[GitHubListingsFeed]:
        if self._listings_url is not None:
            return self._configured_feeds[:1] or [
                GitHubListingsFeed(owner="SimplifyJobs", repo="Summer2027-Internships")
            ]
        if self._resolved_feeds is not None:
            return self._resolved_feeds

        merged = list(self._configured_feeds)
        for org in self._discover_orgs:
            try:
                discovered = await discover_org_listing_feeds(
                    self._client, org, max_retries=min(self._max_retries, 1)
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "github_listings.discovery_failed",
                    org=org,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            merged.extend(discovered)

        self._resolved_feeds = dedupe_feeds(merged)
        return self._resolved_feeds

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
