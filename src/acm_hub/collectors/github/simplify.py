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

DEFAULT_LISTINGS_API_URL: Final = (
    "https://api.github.com/repos/SimplifyJobs/Summer2027-Internships/"
    "contents/.github/scripts/listings.json?ref=dev"
)
DEFAULT_LISTINGS_RAW_URL: Final = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/"
    ".github/scripts/listings.json"
)
#: Primary fetch URL for tests and custom overrides that expect a single endpoint.
DEFAULT_LISTINGS_URL: Final = DEFAULT_LISTINGS_API_URL
LISTINGS_SOURCE_URL: Final = (
    "https://github.com/SimplifyJobs/Summer2027-Internships/blob/dev/"
    ".github/scripts/listings.json"
)
REPOSITORY_URL: Final = "https://github.com/SimplifyJobs/Summer2027-Internships"

GITHUB_RAW_ACCEPT: Final = "application/vnd.github.raw+json"
GITHUB_API_VERSION: Final = "2022-11-28"

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
        source_url=LISTINGS_SOURCE_URL,
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
    source_url = LISTINGS_SOURCE_URL

    #: The feed is a single JSON array containing the repository's entire
    #: catalogue, so one successful fetch is the whole open set. When
    #: ``include_inactive`` is on, closed postings are returned too and the
    #: result is no longer a snapshot of what is *open*; the constructor turns
    #: this off in that case.
    produces_complete_snapshot = True

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        listings_url: str | None = None,
        github_token: str | None = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        include_inactive: bool = False,
    ) -> None:
        """Create the collector.

        Args:
            client: Reused HTTP client. When omitted the collector owns one and
                closes it in :meth:`aclose`.
            listings_url: When set, only this URL is fetched. When omitted, the
                GitHub REST API is tried first, then the raw host as fallback.
            github_token: Optional; raises rate limits for both API and raw.
            include_inactive: When ``False`` (the default) postings upstream has
                marked closed or hidden are dropped at the source boundary.
        """
        client_headers: dict[str, str] = {}
        if github_token:
            client_headers["Authorization"] = f"Bearer {github_token}"
        self._owns_client = client is None
        self._client = client or build_client(
            timeout_seconds=timeout_seconds, headers=client_headers or None
        )
        self._listings_url = listings_url
        self._max_retries = max_retries
        self._include_inactive = include_inactive
        if include_inactive:
            # The closure sweep asks "which of our rows are missing from the
            # open set?". Including closed postings makes them present, which
            # would suppress every legitimate closure.
            self.produces_complete_snapshot = False

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

    def _feed_attempts(self) -> list[tuple[str, dict[str, str]]]:
        if self._listings_url is not None:
            return [(self._listings_url, {})]
        return [
            (
                DEFAULT_LISTINGS_API_URL,
                {
                    "Accept": GITHUB_RAW_ACCEPT,
                    "X-GitHub-Api-Version": GITHUB_API_VERSION,
                },
            ),
            (DEFAULT_LISTINGS_RAW_URL, {}),
        ]

    async def _fetch_payload(self) -> Any:
        errors: list[str] = []
        for url, request_headers in self._feed_attempts():
            try:
                response = await fetch_with_retry(
                    self._client,
                    url,
                    headers=request_headers or None,
                    max_retries=self._max_retries,
                )
            except httpx.HTTPStatusError as exc:
                detail = (
                    f"listings feed returned HTTP {exc.response.status_code} for {url}"
                )
                if exc.response.status_code in {403, 404, 429}:
                    errors.append(detail)
                    logger.warning(
                        "simplify.feed_attempt_failed",
                        url=url,
                        status_code=exc.response.status_code,
                    )
                    continue
                raise CollectorError(self.name, detail) from exc
            except httpx.RequestError as exc:
                errors.append(
                    f"could not reach {url}: {type(exc).__name__}: {exc}"
                )
                logger.warning(
                    "simplify.feed_attempt_failed",
                    url=url,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            else:
                try:
                    return response.json()
                except json.JSONDecodeError as exc:
                    raise CollectorError(
                        self.name, f"listings feed was not valid JSON: {exc}"
                    ) from exc

        hint = (
            " Set GITHUB_TOKEN in .env (no scopes needed) if anonymous GitHub "
            "limits are blocking the feed."
        )
        joined = "; ".join(errors) if errors else "no feed URLs configured"
        raise CollectorError(self.name, f"{joined}.{hint}")

    async def aclose(self) -> None:
        """Close the HTTP client when this collector created it."""
        if self._owns_client:
            await self._client.aclose()
