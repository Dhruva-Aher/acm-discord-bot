"""Collector for the SimplifyJobs / Pitt CSC internship list.

Source format
-------------
The repository publishes a machine-readable array at
``.github/scripts/listings.json`` on its default branch (``dev``). ACM Hub reads
that file rather than scraping the rendered README table: the JSON is what
generates the README, so parsing it is both cheaper and far less fragile than
parsing HTML.

Each element observed in the live feed carries these keys, all always present::

    id             str   upstream UUID, stable across runs
    company_name   str
    title          str
    url            str   the employer's application link
    company_url    str   Simplify company page, may be ""
    locations      list[str]
    terms          list[str]   e.g. ["Summer 2027"]
    degrees        list[str]   e.g. ["Bachelor's", "Master's"]
    category       str   one of AI/ML/Data, Software, Hardware, Product, Quant
                         (plus occasional long-form variants)
    sponsorship    str   Other | Offers Sponsorship |
                         Does Not Offer Sponsorship | U.S. Citizenship is Required
    active         bool  posting still open upstream
    is_visible     bool  upstream soft-delete flag
    date_posted    int   unix seconds
    date_updated   int   unix seconds

Because the upstream schema is outside our control, every record is parsed
defensively. A record that is missing required fields, or whose types have
changed, is skipped and counted rather than allowed to become a garbage Discord
post; if *every* record fails to parse the collector raises so the run is marked
failed instead of silently reporting zero opportunities.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Final

import httpx

from acm_hub.collectors.base import Collector, CollectorError
from acm_hub.collectors.http import build_client, fetch_with_retry
from acm_hub.domain.enums import OpportunityType, SponsorshipStatus, VerificationStatus
from acm_hub.domain.source import RawOpportunity
from acm_hub.observability.logging import get_logger

logger = get_logger(__name__)

DEFAULT_LISTINGS_URL: Final = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/"
    ".github/scripts/listings.json"
)
REPOSITORY_URL: Final = "https://github.com/SimplifyJobs/Summer2027-Internships"

COLLECTOR_NAME: Final = "simplify_internships"
DISPLAY_NAME: Final = "Simplify / Pitt CSC"

#: Upstream sponsorship strings mapped onto our enum. "Other" is upstream's
#: value for *unstated*, so it maps to UNKNOWN - never to "eligible".
SPONSORSHIP_MAP: Final[dict[str, SponsorshipStatus]] = {
    "offers sponsorship": SponsorshipStatus.OFFERS_SPONSORSHIP,
    "does not offer sponsorship": SponsorshipStatus.NO_SPONSORSHIP,
    "u.s. citizenship is required": SponsorshipStatus.CITIZENSHIP_REQUIRED,
    "us citizenship is required": SponsorshipStatus.CITIZENSHIP_REQUIRED,
    "other": SponsorshipStatus.UNKNOWN,
}

#: Upstream category strings normalized to ACM Hub tags. Unmapped categories are
#: passed through lowercased so a new upstream category still reaches the
#: classifier instead of being dropped.
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


def _optional_timestamp(record: dict[str, Any], key: str) -> datetime | None:
    """Read a unix-seconds field, tolerating absence and nonsense values."""
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


def parse_listing(record: Any) -> RawOpportunity:
    """Convert one upstream JSON object into a :class:`RawOpportunity`.

    Raises:
        MalformedListingError: if the record is not a usable listing. Callers
            skip and count these rather than aborting the run.
    """
    if not isinstance(record, dict):
        raise MalformedListingError(f"expected an object, got {type(record).__name__}")

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

    return RawOpportunity(
        source_name=COLLECTOR_NAME,
        source_external_id=source_external_id,
        source_url=REPOSITORY_URL,
        application_url=application_url,
        title=title,
        organization=company,
        # The feed carries no prose description. Inventing one would put text in
        # front of members that the employer never wrote.
        description=None,
        opportunity_type=OpportunityType.INTERNSHIP,
        locations=_optional_str_list(record, "locations"),
        tags=tuple(dict.fromkeys((*_category_tags(category_text), *(t.lower() for t in terms)))),
        sponsorship=sponsorship,
        citizenship_requirement=citizenship_requirement,
        degree_requirements=_optional_str_list(record, "degrees"),
        posted_at=_optional_timestamp(record, "date_posted"),
        is_active=active if isinstance(active, bool) else None,
        # An aggregator asserting the job exists is not the same as ACM Hub
        # confirming it against the employer.
        verification_status=VerificationStatus.SOURCE_CLAIMED,
        extra={"upstream_source": str(record.get("source", ""))},
    )


class SimplifyCollector(Collector):
    """Reads open internships from the SimplifyJobs listings feed."""

    name = COLLECTOR_NAME
    display_name = DISPLAY_NAME
    source_url = REPOSITORY_URL

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        listings_url: str = DEFAULT_LISTINGS_URL,
        github_token: str | None = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        include_inactive: bool = False,
    ) -> None:
        """Create the collector.

        Args:
            client: Reused HTTP client. When omitted the collector owns one and
                closes it in :meth:`aclose`.
            listings_url: Overridable so tests can point at a fixture server.
            github_token: Optional; only raises the anonymous rate limit.
            include_inactive: When ``False`` (the default) postings upstream has
                marked closed or hidden are dropped at the source boundary.
        """
        headers = {"Authorization": f"Bearer {github_token}"} if github_token else None
        self._owns_client = client is None
        self._client = client or build_client(timeout_seconds=timeout_seconds, headers=headers)
        self._listings_url = listings_url
        self._max_retries = max_retries
        self._include_inactive = include_inactive

    async def collect(self) -> list[RawOpportunity]:
        """Fetch the feed and parse every listing it contains."""
        payload = await self._fetch_payload()

        if not isinstance(payload, list):
            raise CollectorError(
                self.name,
                f"expected a JSON array of listings, got {type(payload).__name__}",
            )

        opportunities: list[RawOpportunity] = []
        skipped_malformed = 0
        skipped_inactive = 0

        for record in payload:
            try:
                raw = parse_listing(record)
            except MalformedListingError as exc:
                skipped_malformed += 1
                # Logged at debug because a handful of bad rows in a
                # 16k-record community feed is normal; the aggregate counts
                # below are what operators should alert on.
                logger.debug("simplify.record_skipped", reason=str(exc))
                continue

            if not self._include_inactive and not self._is_open(record, raw):
                skipped_inactive += 1
                continue

            opportunities.append(raw)

        if payload and not opportunities and skipped_malformed == len(payload):
            raise CollectorError(
                self.name,
                f"every one of {len(payload)} upstream records failed to parse; "
                "the feed format has probably changed",
            )

        logger.info(
            "simplify.parsed",
            collector=self.name,
            total_records=len(payload),
            usable=len(opportunities),
            skipped_malformed=skipped_malformed,
            skipped_inactive=skipped_inactive,
        )
        return opportunities

    @staticmethod
    def _is_open(record: Any, raw: RawOpportunity) -> bool:
        """Whether upstream still considers the posting open and visible."""
        visible = record.get("is_visible") if isinstance(record, dict) else None
        if visible is False:
            return False
        return raw.is_active is not False

    async def _fetch_payload(self) -> Any:
        try:
            response = await fetch_with_retry(
                self._client, self._listings_url, max_retries=self._max_retries
            )
        except httpx.HTTPStatusError as exc:
            raise CollectorError(
                self.name,
                f"listings feed returned HTTP {exc.response.status_code} for {self._listings_url}",
            ) from exc
        except httpx.RequestError as exc:
            raise CollectorError(
                self.name, f"could not reach {self._listings_url}: {type(exc).__name__}: {exc}"
            ) from exc

        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise CollectorError(self.name, f"listings feed was not valid JSON: {exc}") from exc

    async def aclose(self) -> None:
        """Close the HTTP client when this collector created it."""
        if self._owns_client:
            await self._client.aclose()
