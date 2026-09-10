"""Normalization: raw source records into canonical opportunities.

Two derived values matter most downstream and are computed here:

``canonical_key``
    Stable identity of the opportunity across collection runs *and* across
    sources. Recomputing it from the same posting must always give the same
    string, otherwise the same internship is published twice.

``content_hash``
    Covers only member-visible content. It changes when the posting itself
    changed (new title, new locations, employer now states sponsorship) and
    stays put when we merely re-collected an unchanged record.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from acm_hub.domain.enums import RemoteStatus
from acm_hub.domain.opportunity import Eligibility, Opportunity
from acm_hub.domain.source import RawOpportunity

#: Query parameters that only describe *how the user arrived*, never *which job
#: this is*. Removing them keeps one posting from being ingested repeatedly
#: under different campaign links.
TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "utm_name",
        "gh_src",
        "fbclid",
        "gclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "ref",
        "ref_src",
        "referrer",
        "trk",
        "trackingid",
        "trackid",
        "source_id",
        "campaign_id",
        "_ga",
        "yclid",
        "spm",
    }
)

#: Legal-form suffixes dropped when comparing organization names, so
#: "Cloudflare, Inc." and "Cloudflare" collapse to the same identity.
_ORG_SUFFIXES: frozenset[str] = frozenset(
    {"inc", "incorporated", "llc", "llp", "ltd", "limited", "corp", "corporation", "co", "plc"}
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_WHITESPACE = re.compile(r"\s+")

#: Locations in aggregator feeds encode remote work as free text, e.g.
#: "Remote in USA". Recognising it is a factual read of the location string,
#: not an inference about roles that simply do not mention remote work.
_REMOTE_LOCATION = re.compile(r"\bremote\b", re.IGNORECASE)
_HYBRID_LOCATION = re.compile(r"\bhybrid\b", re.IGNORECASE)


def collapse_whitespace(value: str) -> str:
    """Trim and collapse runs of whitespace, preserving the original casing."""
    return _WHITESPACE.sub(" ", value).strip()


def _slugify(value: str) -> str:
    """Fold text to an ASCII, lowercase, hyphen-free comparison token."""
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return _NON_ALNUM.sub(" ", ascii_only.lower()).strip()


def normalize_url(url: str) -> str:
    """Return a canonical form of ``url`` suitable for identity comparison.

    Applies scheme/host lowercasing, ``www.`` and default-port removal, fragment
    removal, trailing-slash removal, tracking-parameter removal and query
    ordering. Parameters that are not known tracking parameters are preserved,
    because on boards like Greenhouse and iCIMS the query string is what
    identifies the job.

    Input that is not a parseable absolute URL is returned whitespace-trimmed
    rather than mangled, so a broken upstream value stays recognisable in logs.
    """
    trimmed = url.strip()
    if not trimmed:
        return ""

    parts = urlsplit(trimmed)
    if not parts.scheme or not parts.netloc:
        return trimmed

    scheme = parts.scheme.lower()

    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    netloc = host
    default_port = {"http": 80, "https": 443}.get(scheme)
    if parts.port is not None and parts.port != default_port:
        netloc = f"{host}:{parts.port}"

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    query = "&".join(f"{key}={value}" for key, value in sorted(kept))

    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_organization(name: str) -> str:
    """Fold an organization name to a comparison token.

    Drops legal-form suffixes and punctuation: ``"Cloudflare, Inc."`` and
    ``"cloudflare"`` both become ``"cloudflare"``.
    """
    tokens = _slugify(name).split()
    while tokens and tokens[-1] in _ORG_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def normalize_title(title: str) -> str:
    """Fold a role title to a comparison token."""
    return " ".join(_slugify(title).split())


def normalize_location(location: str) -> str:
    """Fold a single location string to a comparison token."""
    return " ".join(_slugify(location).split())


def normalize_locations(locations: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Clean, de-duplicate and order location strings, preserving display casing."""
    seen: dict[str, str] = {}
    for raw_location in locations:
        display = collapse_whitespace(raw_location)
        if not display:
            continue
        key = normalize_location(display)
        if key and key not in seen:
            seen[key] = display
    return tuple(seen[key] for key in sorted(seen))


def infer_remote_status(
    locations: tuple[str, ...], declared: RemoteStatus = RemoteStatus.UNKNOWN
) -> RemoteStatus:
    """Read remote status out of location strings when the source states it there.

    A collector's explicit ``declared`` value always wins. Locations that say
    nothing about remote work leave the status ``UNKNOWN`` - we do not assume
    on-site.
    """
    if declared is not RemoteStatus.UNKNOWN:
        return declared
    if any(_HYBRID_LOCATION.search(location) for location in locations):
        return RemoteStatus.HYBRID
    if any(_REMOTE_LOCATION.search(location) for location in locations):
        return RemoteStatus.REMOTE
    return RemoteStatus.UNKNOWN


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def build_canonical_key(
    *,
    application_url: str,
    source_name: str,
    source_external_id: str | None,
    organization: str,
    title: str,
    locations: tuple[str, ...],
) -> str:
    """Derive the deterministic identity key for an opportunity.

    Strategies are tried in descending order of confidence:

    1. ``url``  - the normalized application URL. Two sources advertising the
       same posting agree here, which is what makes cross-source dedup work.
    2. ``extid`` - source name plus the source's own stable identifier, used
       when the application URL is unusable.
    3. ``fuzzy`` - normalized organization, title and primary location.

    The returned value is prefixed with the strategy so operators can tell from
    a log line *why* two records collided.
    """
    normalized_url = normalize_url(application_url)
    if normalized_url:
        return f"url:{_digest(normalized_url)}"

    if source_external_id:
        return f"extid:{_digest(source_name.lower(), source_external_id)}"

    primary_location = normalize_locations(locations)
    location_token = normalize_location(primary_location[0]) if primary_location else ""
    return "fuzzy:" + _digest(
        normalize_organization(organization), normalize_title(title), location_token
    )


def build_content_hash(
    *,
    title: str,
    organization: str,
    description: str | None,
    locations: tuple[str, ...],
    remote_status: RemoteStatus,
    application_url: str,
    eligibility: Eligibility,
    expires_at: datetime | None,
) -> str:
    """Hash the member-visible content of an opportunity.

    Deliberately excludes ``discovered_at``, ``last_verified_at``, review status
    and anything else that changes just because we ran the collector again.
    """
    return _digest(
        normalize_title(title),
        normalize_organization(organization),
        collapse_whitespace(description or ""),
        "|".join(normalize_locations(locations)),
        remote_status.value,
        normalize_url(application_url),
        eligibility.sponsorship.value,
        collapse_whitespace(eligibility.citizenship_requirement or ""),
        "|".join(sorted(_slugify(degree) for degree in eligibility.degree_requirements)),
        expires_at.astimezone(UTC).isoformat() if expires_at else "",
    )


def normalize_opportunity(raw: RawOpportunity, *, discovered_at: datetime) -> Opportunity:
    """Convert a :class:`RawOpportunity` into a canonical :class:`Opportunity`.

    Adds no facts the source did not provide: unknown eligibility stays unknown
    and empty descriptions stay ``None``.
    """
    locations = normalize_locations(raw.locations)
    remote_status = infer_remote_status(locations, raw.remote_status)
    description = collapse_whitespace(raw.description) if raw.description else None

    eligibility = Eligibility(
        sponsorship=raw.sponsorship,
        citizenship_requirement=(
            collapse_whitespace(raw.citizenship_requirement)
            if raw.citizenship_requirement
            else None
        ),
        degree_requirements=tuple(
            collapse_whitespace(degree) for degree in raw.degree_requirements if degree.strip()
        ),
    )

    title = collapse_whitespace(raw.title)
    organization = collapse_whitespace(raw.organization)
    application_url = normalize_url(raw.application_url)

    return Opportunity(
        source_name=raw.source_name,
        source_external_id=raw.source_external_id,
        source_url=normalize_url(raw.source_url) or raw.source_url,
        application_url=application_url or raw.application_url,
        title=title,
        organization=organization,
        description=description or None,
        opportunity_type=raw.opportunity_type,
        locations=locations,
        remote_status=remote_status,
        tags=tuple(dict.fromkeys(collapse_whitespace(tag) for tag in raw.tags if tag.strip())),
        eligibility=eligibility,
        posted_at=raw.posted_at,
        discovered_at=discovered_at,
        expires_at=raw.expires_at,
        verification_status=raw.verification_status,
        content_hash=build_content_hash(
            title=title,
            organization=organization,
            description=description,
            locations=locations,
            remote_status=remote_status,
            application_url=application_url or raw.application_url,
            eligibility=eligibility,
            expires_at=raw.expires_at,
        ),
        canonical_key=build_canonical_key(
            application_url=raw.application_url,
            source_name=raw.source_name,
            source_external_id=raw.source_external_id,
            organization=organization,
            title=title,
            locations=locations,
        ),
    )
