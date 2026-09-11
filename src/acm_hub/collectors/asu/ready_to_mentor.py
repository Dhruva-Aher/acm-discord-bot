"""ASU Fulton Forge "Ready to mentor" research radar.

Authoritative source
--------------------
``https://forge.engineering.asu.edu/mentors/`` is the official Fulton Forge
Student Research Expo faculty page. The **Ready to mentor** section states
that the listed faculty "are looking for students to participate in
undergraduate and graduate research projects." That sentence is the only
recruiting claim ACM Hub is allowed to repeat.

The same page has a second section, **Mentors from the current expo**. Those
faculty mentored a past or current expo project. That is not evidence they
are recruiting now, so those cards are ignored.

What this collector will not do
-------------------------------
- Scrape faculty lab pages, Google Scholar, grants or publication lists and
  infer that a professor is looking for students.
- Fetch individual ``/faculty_mentor/{slug}/`` profiles. The listing already
  carries the stated interests, and ``robots.txt`` asks for a 10-second
  crawl-delay between requests.
- Treat the WordPress ``faculty_mentor`` taxonomy as a recruiting list. That
  taxonomy includes expo-only mentors and the public REST API does not expose
  the ready-to-mentor flag.
- Invent a FURI/MORE application deadline. Those programs are tags the page
  itself puts on a card; a closed application cycle is not something this
  HTML states.

robots.txt on this host allows ``/mentors/`` and sets ``Crawl-delay: 10``.
A collection run fetches this one URL and then stops.
"""

from __future__ import annotations

from html import unescape
from html.parser import HTMLParser
from typing import Final

import httpx

from acm_hub.collectors.base import Collector, CollectorError
from acm_hub.collectors.http import build_client, fetch_with_retry
from acm_hub.collectors.protection import looks_like_bot_protection
from acm_hub.domain.enums import OpportunityType, VerificationStatus
from acm_hub.domain.source import RawOpportunity
from acm_hub.observability.logging import get_logger

logger = get_logger(__name__)

DEFAULT_MENTORS_URL: Final = "https://forge.engineering.asu.edu/mentors/"
SOURCE_PAGE_URL: Final = DEFAULT_MENTORS_URL

COLLECTOR_NAME: Final = "asu_ready_to_mentor"
DISPLAY_NAME: Final = "ASU Fulton Forge — Ready to mentor"

PROGRAM_LABELS: Final[dict[str, str]] = {
    "furi": "FURI",
    "more": "MORE",
}

THEME_LABELS: Final[dict[str, str]] = {
    "data": "data",
    "education": "education",
    "energy": "energy",
    "health": "health",
    "security": "security",
    "semiconductor-manufacturing": "semiconductor manufacturing",
    "sustainability": "sustainability",
}

READY_SECTION_MARKERS: Final[tuple[str, ...]] = (
    "ready to mentor",
    "mentor-ready-block",
)


class MalformedMentorError(ValueError):
    """One mentor card could not be read; the rest of the run continues."""


class _ReadyMentorParser(HTMLParser):
    """Walk the mentors page and keep only ``ready-mentor`` cards.

    Expo cards reuse some of the same inner markup (``person-name``, a
    ``faculty_mentor`` link) but they do not carry the ``ready-mentor`` class.
    Starting a card only on that class is what keeps expo mentors out.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[dict[str, str]] = []
        self._card: dict[str, str] | None = None
        self._card_depth = 0
        self._capture: str | None = None
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: (value or "") for key, value in attrs}
        classes = attributes.get("class", "").split()

        if self._card is None:
            if tag == "div" and "ready-mentor" in classes:
                self._card = {
                    "asurite": attributes.get("data-asurite", "").strip(),
                    "programs": attributes.get("data-programs", "").strip(),
                    "projects": attributes.get("data-projects", "").strip(),
                    "href": "",
                    "name": "",
                    "dept": "",
                    "text": "",
                }
                self._card_depth = 1
            return

        if tag == "div":
            self._card_depth += 1

        if tag == "a" and not self._card["href"] and "faculty_mentor" in attributes.get("href", ""):
            self._card["href"] = attributes["href"].strip()
            self._capture = "name"
            self._buffer = []
        elif tag == "p" and "person-dept" in classes:
            self._capture = "dept"
            self._buffer = []
        elif tag == "div" and "mentor-ready-text" in classes:
            self._capture = "text"
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if self._card is None:
            return

        if self._capture is not None:
            if self._capture == "name" and tag == "a":
                self._card["name"] = unescape("".join(self._buffer)).strip()
                self._capture = None
            elif self._capture == "dept" and tag == "p":
                self._card["dept"] = unescape("".join(self._buffer)).strip()
                self._capture = None
            elif self._capture == "text" and tag == "div":
                self._card["text"] = unescape("".join(self._buffer)).strip()
                self._capture = None

        if tag == "div":
            self._card_depth -= 1
            if self._card_depth <= 0:
                self.cards.append(self._card)
                self._card = None
                self._capture = None

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._buffer.append(data)


def _csv_tokens(raw: str) -> tuple[str, ...]:
    return tuple(token.strip() for token in raw.split(",") if token.strip())


def _program_labels(raw: str) -> tuple[str, ...]:
    labels: list[str] = []
    for token in _csv_tokens(raw):
        labels.append(PROGRAM_LABELS.get(token.lower(), token.upper()))
    return tuple(dict.fromkeys(labels))


def _theme_tags(raw: str) -> tuple[str, ...]:
    tags: list[str] = []
    for token in _csv_tokens(raw):
        tags.append(THEME_LABELS.get(token.lower(), token.lower().replace("-", " ")))
    return tuple(dict.fromkeys(tags))


def _description(interests: str, programs: tuple[str, ...]) -> str | None:
    """Member-visible prose, using only text the page printed."""
    parts: list[str] = []
    if interests:
        parts.append(interests)
    if programs:
        parts.append("Listed for: " + ", ".join(programs))
    return "\n\n".join(parts) if parts else None


def parse_ready_mentor_page(html: str) -> list[dict[str, str]]:
    """Return the raw ready-to-mentor cards found in ``html``."""
    parser = _ReadyMentorParser()
    parser.feed(html)
    parser.close()
    return parser.cards


def parse_mentor_card(card: dict[str, str]) -> RawOpportunity:
    """Turn one parsed card into a :class:`RawOpportunity`.

    Raises:
        MalformedMentorError: the card is missing a name or official profile URL.
    """
    name = card.get("name", "").strip()
    href = card.get("href", "").strip()
    if not name or not href:
        raise MalformedMentorError("mentor card is missing a name or profile URL")

    programs = _program_labels(card.get("programs", ""))
    themes = _theme_tags(card.get("projects", ""))
    department = card.get("dept", "").strip() or None
    asurite = card.get("asurite", "").strip() or None
    interests = card.get("text", "").strip()

    locations = (department,) if department else ("Ira A. Fulton Schools of Engineering",)
    tags = tuple(dict.fromkeys((*[label.lower() for label in programs], *themes)))

    extra: dict[str, str] = {"recruiting_basis": "ready_to_mentor"}
    if asurite:
        extra["asurite"] = asurite
    if department:
        extra["department"] = department
    if programs:
        extra["programs"] = ",".join(programs)

    return RawOpportunity(
        source_name=COLLECTOR_NAME,
        source_external_id=asurite,
        source_url=SOURCE_PAGE_URL,
        application_url=href,
        title=f"Ready to mentor: {name}",
        organization=name,
        description=_description(interests, programs),
        opportunity_type=OpportunityType.RESEARCH,
        locations=locations,
        tags=tags,
        verification_status=VerificationStatus.SOURCE_CLAIMED,
        extra=extra,
    )


def _page_looks_like_mentors(html: str) -> bool:
    lowered = html.lower()
    return any(marker in lowered for marker in READY_SECTION_MARKERS)


class AsuReadyToMentorCollector(Collector):
    """Reads faculty ASU has explicitly labelled ready to mentor."""

    name = COLLECTOR_NAME
    display_name = DISPLAY_NAME
    source_url = SOURCE_PAGE_URL

    #: The Ready-to-mentor section is the complete current recruiting set
    #: published on this page. Faculty who drop off it are no longer labelled
    #: recruiting by the authoritative source.
    produces_complete_snapshot = True

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        mentors_url: str = DEFAULT_MENTORS_URL,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self._owns_client = client is None
        self._client = client or build_client(timeout_seconds=timeout_seconds)
        self._mentors_url = mentors_url
        self._max_retries = max_retries

    async def collect(self) -> list[RawOpportunity]:
        """Fetch the mentors page and return only Ready-to-mentor faculty."""
        html = await self._fetch_html()

        if looks_like_bot_protection(html):
            raise CollectorError(
                self.name,
                "mentors page returned a bot-protection interstitial; ACM Hub will not bypass it",
            )

        if not _page_looks_like_mentors(html):
            raise CollectorError(
                self.name,
                "page is missing the Ready to mentor section; the source layout "
                "has probably changed",
            )

        cards = parse_ready_mentor_page(html)
        opportunities: list[RawOpportunity] = []
        skipped_malformed = 0

        for card in cards:
            try:
                opportunities.append(parse_mentor_card(card))
            except MalformedMentorError as exc:
                skipped_malformed += 1
                logger.debug("asu_ready_to_mentor.card_skipped", reason=str(exc))

        if cards and not opportunities:
            raise CollectorError(
                self.name,
                f"every one of {len(cards)} ready-to-mentor cards failed to parse; "
                "the source layout has probably changed",
            )

        logger.info(
            "asu_ready_to_mentor.parsed",
            collector=self.name,
            ready_cards=len(cards),
            usable=len(opportunities),
            skipped_malformed=skipped_malformed,
        )
        return opportunities

    async def _fetch_html(self) -> str:
        try:
            response = await fetch_with_retry(
                self._client, self._mentors_url, max_retries=self._max_retries
            )
        except httpx.HTTPStatusError as exc:
            raise CollectorError(
                self.name,
                f"mentors page returned HTTP {exc.response.status_code} for {self._mentors_url}",
            ) from exc
        except httpx.RequestError as exc:
            raise CollectorError(
                self.name, f"could not reach {self._mentors_url}: {type(exc).__name__}: {exc}"
            ) from exc

        return response.text

    async def aclose(self) -> None:
        """Close the HTTP client when this collector created it."""
        if self._owns_client:
            await self._client.aclose()
