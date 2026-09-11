"""Official ACM upcoming-conference collector.

Authoritative source
--------------------
``https://www.acm.org/conferences`` is ACM's public upcoming-conferences page.
``robots.txt`` allows that path. It disallows ``/calendar``,
``/conferences/conference-events`` and ``/conferences/non-acm-events``; this
collector never requests those.

acm.org sits behind Cloudflare. Automated fetches from some networks receive a
block page instead of the listing. ACM Hub does **not** spoof a browser,
solve a challenge or scrape a disallowed calendar to get around that. When
the official page is blocked, this collector raises :class:`CollectorError`
so officers see a failed source rather than a fabricated catalogue.

What is parsed
--------------
1. JSON-LD ``Event`` / ``EducationEvent`` objects embedded in the page. That
   is ACM's own machine-readable markup when present.
2. ``article.acm-conference`` cards with an official link, used when the
   listing is HTML-only.

Conference dates are the event itself, not a CFP deadline. They are stored
in ``extra`` when the source states them and are never written to
``expires_at`` — an end date is not evidence the opportunity has closed.

The listing is a window of upcoming events, not a promise that every ACM
conference that exists is on the page, so ``produces_complete_snapshot``
stays ``False``. Dropping off the page is not treated as a closure.
"""

from __future__ import annotations

import json
import re
from html import unescape
from html.parser import HTMLParser
from typing import Any, Final
from urllib.parse import urlparse

import httpx

from acm_hub.collectors.base import Collector, CollectorError
from acm_hub.collectors.http import build_client, fetch_with_retry
from acm_hub.collectors.protection import looks_like_bot_protection
from acm_hub.domain.enums import OpportunityType, VerificationStatus
from acm_hub.domain.source import RawOpportunity
from acm_hub.observability.logging import get_logger

logger = get_logger(__name__)

DEFAULT_CONFERENCES_URL: Final = "https://www.acm.org/conferences"
SOURCE_PAGE_URL: Final = DEFAULT_CONFERENCES_URL

COLLECTOR_NAME: Final = "acm_official_conferences"
DISPLAY_NAME: Final = "ACM official conferences"

EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {"event", "educationevent", "businessevent", "exhibitionevent"}
)

_JSON_LD_SCRIPT = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


class MalformedConferenceError(ValueError):
    """One conference entry could not be read; the rest of the run continues."""


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _type_names(value: Any) -> set[str]:
    names: set[str] = set()
    for item in _as_list(value):
        if isinstance(item, str):
            names.add(item.rsplit("/", 1)[-1].lower())
    return names


def _text(value: Any) -> str:
    if isinstance(value, str):
        return unescape(value).strip()
    if isinstance(value, dict):
        for key in ("name", "title", "@value"):
            inner = value.get(key)
            if isinstance(inner, str) and inner.strip():
                return unescape(inner).strip()
    return ""


def _absolute_http_url(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return text


def _location_name(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return unescape(value).strip()
    if not isinstance(value, dict):
        return None
    name = _text(value.get("name"))
    if name:
        return name
    address = value.get("address")
    if isinstance(address, str) and address.strip():
        return unescape(address).strip()
    if isinstance(address, dict):
        parts = [
            _text(address.get(key))
            for key in ("addressLocality", "addressRegion", "addressCountry")
        ]
        joined = ", ".join(part for part in parts if part)
        return joined or None
    return None


def _walk_json_ld(node: Any) -> list[dict[str, Any]]:
    """Collect Event-typed objects from a JSON-LD document, including @graph."""
    events: list[dict[str, Any]] = []
    for item in _as_list(node):
        if not isinstance(item, dict):
            continue
        if _type_names(item.get("@type")) & EVENT_TYPES:
            events.append(item)
        if "@graph" in item:
            events.extend(_walk_json_ld(item["@graph"]))
    return events


def parse_json_ld_events(html: str) -> list[dict[str, Any]]:
    """Read schema.org Event objects out of JSON-LD script tags."""
    events: list[dict[str, Any]] = []
    for match in _JSON_LD_SCRIPT.finditer(html):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("acm_official_conferences.json_ld_unreadable")
            continue
        events.extend(_walk_json_ld(payload))
    return events


class _ConferenceCardParser(HTMLParser):
    """Collect ``article.acm-conference`` cards with a single official link."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[dict[str, str]] = []
        self._card: dict[str, str] | None = None
        self._depth = 0
        self._capture: str | None = None
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: (value or "") for key, value in attrs}
        classes = attributes.get("class", "").split()

        if self._card is None:
            if tag == "article" and "acm-conference" in classes:
                self._card = {"href": "", "name": "", "location": ""}
                self._depth = 1
            return

        if tag == "article":
            self._depth += 1

        if tag == "a" and not self._card["href"] and attributes.get("href"):
            href = attributes["href"].strip()
            if urlparse(href).scheme in {"http", "https"}:
                self._card["href"] = href
                self._capture = "name"
                self._buffer = []
        elif tag in {"p", "span"} and "location" in classes:
            self._capture = "location"
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if self._card is None:
            return
        if self._capture == "name" and tag == "a":
            self._card["name"] = unescape("".join(self._buffer)).strip()
            self._capture = None
        elif self._capture == "location" and tag in {"p", "span"}:
            self._card["location"] = unescape("".join(self._buffer)).strip()
            self._capture = None
        if tag == "article":
            self._depth -= 1
            if self._depth <= 0:
                self.cards.append(self._card)
                self._card = None
                self._capture = None

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._buffer.append(data)


def parse_html_conference_cards(html: str) -> list[dict[str, str]]:
    parser = _ConferenceCardParser()
    parser.feed(html)
    parser.close()
    return parser.cards


def _from_json_ld(event: dict[str, Any]) -> RawOpportunity:
    title = _text(event.get("name"))
    url = _absolute_http_url(event.get("url"))
    if not title or url is None:
        raise MalformedConferenceError("JSON-LD event is missing a name or official URL")

    location = _location_name(event.get("location"))
    organizer = _text(event.get("organizer")) or "ACM"
    start = _text(event.get("startDate"))
    end = _text(event.get("endDate"))

    extra: dict[str, str] = {"extracted_from": "json_ld"}
    if start:
        extra["start_date"] = start
    if end:
        extra["end_date"] = end

    return RawOpportunity(
        source_name=COLLECTOR_NAME,
        source_external_id=url,
        source_url=SOURCE_PAGE_URL,
        application_url=url,
        title=title,
        organization=organizer,
        description=None,
        opportunity_type=OpportunityType.CONFERENCE,
        locations=(location,) if location else (),
        tags=("acm", "conference"),
        verification_status=VerificationStatus.SOURCE_CLAIMED,
        extra=extra,
    )


def _from_html_card(card: dict[str, str]) -> RawOpportunity:
    title = card.get("name", "").strip()
    url = _absolute_http_url(card.get("href"))
    if not title or url is None:
        raise MalformedConferenceError("conference card is missing a name or official URL")

    location = card.get("location", "").strip()
    return RawOpportunity(
        source_name=COLLECTOR_NAME,
        source_external_id=url,
        source_url=SOURCE_PAGE_URL,
        application_url=url,
        title=title,
        organization="ACM",
        description=None,
        opportunity_type=OpportunityType.CONFERENCE,
        locations=(location,) if location else (),
        tags=("acm", "conference"),
        verification_status=VerificationStatus.SOURCE_CLAIMED,
        extra={"extracted_from": "html_card"},
    )


def parse_official_conferences(html: str) -> list[RawOpportunity]:
    """Parse official conference records from an ACM conferences page.

    JSON-LD wins when the same official URL appears in both formats, so a
    conference is never stored twice from one page.
    """
    seen_urls: set[str] = set()
    records: list[RawOpportunity] = []
    skipped = 0

    for event in parse_json_ld_events(html):
        try:
            raw = _from_json_ld(event)
        except MalformedConferenceError as exc:
            skipped += 1
            logger.debug("acm_official_conferences.event_skipped", reason=str(exc))
            continue
        if raw.application_url in seen_urls:
            continue
        seen_urls.add(raw.application_url)
        records.append(raw)

    for card in parse_html_conference_cards(html):
        try:
            raw = _from_html_card(card)
        except MalformedConferenceError as exc:
            skipped += 1
            logger.debug("acm_official_conferences.card_skipped", reason=str(exc))
            continue
        if raw.application_url in seen_urls:
            continue
        seen_urls.add(raw.application_url)
        records.append(raw)

    logger.info(
        "acm_official_conferences.parsed",
        usable=len(records),
        skipped_malformed=skipped,
    )
    return records


class AcmOfficialConferencesCollector(Collector):
    """Reads upcoming conferences from ACM's official public listing."""

    name = COLLECTOR_NAME
    display_name = DISPLAY_NAME
    source_url = SOURCE_PAGE_URL
    produces_complete_snapshot = False

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        conferences_url: str = DEFAULT_CONFERENCES_URL,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self._owns_client = client is None
        self._client = client or build_client(timeout_seconds=timeout_seconds)
        self._conferences_url = conferences_url
        self._max_retries = max_retries

    async def collect(self) -> list[RawOpportunity]:
        """Fetch the official listing and parse only what it actually states."""
        html = await self._fetch_html()

        if looks_like_bot_protection(html):
            raise CollectorError(
                self.name,
                "official ACM conferences page was blocked by bot protection; "
                "ACM Hub will not bypass Cloudflare or scrape a disallowed calendar",
            )

        records = parse_official_conferences(html)
        if not records:
            raise CollectorError(
                self.name,
                "official ACM conferences page contained no parseable events; "
                "the listing format has probably changed, or the page is a "
                "challenge interstitial we did not recognise",
            )
        return records

    async def _fetch_html(self) -> str:
        try:
            response = await fetch_with_retry(
                self._client, self._conferences_url, max_retries=self._max_retries
            )
        except httpx.HTTPStatusError as exc:
            raise CollectorError(
                self.name,
                f"conferences page returned HTTP {exc.response.status_code} "
                f"for {self._conferences_url}",
            ) from exc
        except httpx.RequestError as exc:
            raise CollectorError(
                self.name,
                f"could not reach {self._conferences_url}: {type(exc).__name__}: {exc}",
            ) from exc
        return response.text

    async def aclose(self) -> None:
        """Close the HTTP client when this collector created it."""
        if self._owns_client:
            await self._client.aclose()
