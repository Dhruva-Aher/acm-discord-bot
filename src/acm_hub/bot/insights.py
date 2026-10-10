"""Stand-out research helpers for published opportunities.

ACM Hub does **not** invent interview skills, recruiter names, or alumni
lists. What this module builds:

1. Freshness from ``discovered_at`` (same-day callout when applicable).
2. Focus tags from source category tags and SIG matches — themes the listing
   already carried, not a scraped job description and not "previous hire" skills.
3. Prefixed search links (Reddit interview threads, LinkedIn recruiters,
   LinkedIn ASU alumni at the company). Those are starting points for the
   member to verify, not claims about who got an offer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote_plus

from acm_hub.db import models
from acm_hub.db.repositories.opportunities import sig_matches_from_json
from acm_hub.domain.enums import OpportunityType

MAX_FOCUS_TAGS = 6
MAX_LINK_LABEL = 80

#: Tags that are plumbing, not stand-out themes.
_SKIP_TAGS = frozenset(
    {
        "github",
        "new grad",
        "summer 2025",
        "summer 2026",
        "summer 2027",
        "fall 2025",
        "fall 2026",
        "spring 2026",
        "spring 2027",
    }
)

_TYPE_SEARCH_HINT: dict[OpportunityType, str] = {
    OpportunityType.INTERNSHIP: "internship interview",
    OpportunityType.PROGRAM: "new grad interview",
    OpportunityType.RESEARCH: "research internship interview",
    OpportunityType.FELLOWSHIP: "fellowship interview",
    OpportunityType.OTHER: "interview experience",
}


@dataclass(frozen=True, slots=True)
class ResearchLink:
    label: str
    url: str


@dataclass(frozen=True, slots=True)
class OpportunityInsights:
    """Member-facing stand-out panel for one opportunity."""

    freshness: str | None
    focus_tags: tuple[str, ...]
    research_links: tuple[ResearchLink, ...]
    disclaimer: str

    @property
    def has_panel(self) -> bool:
        return bool(self.freshness or self.focus_tags or self.research_links)


def _is_same_day(discovered_at: datetime | None, *, now: datetime) -> bool:
    if discovered_at is None:
        return False
    when = discovered_at if discovered_at.tzinfo else discovered_at.replace(tzinfo=UTC)
    return when.astimezone(UTC).date() == now.astimezone(UTC).date()


def focus_tags_for(row: models.Opportunity) -> tuple[str, ...]:
    """Themes from SIGs and source tags — never scraped JD skill lists."""
    tags: list[str] = []
    for match in sig_matches_from_json(row.sig_matches):
        if match.sig and match.sig not in tags:
            tags.append(match.sig)
    for tag in row.tags:
        cleaned = tag.strip()
        if not cleaned:
            continue
        lower = cleaned.lower()
        if lower.startswith("github:"):
            continue
        if lower in _SKIP_TAGS:
            continue
        if any(existing.lower() == lower for existing in tags):
            continue
        tags.append(cleaned)
        if len(tags) >= MAX_FOCUS_TAGS:
            break
    return tuple(tags[:MAX_FOCUS_TAGS])


def research_links_for(row: models.Opportunity) -> tuple[ResearchLink, ...]:
    """Prefixed search links. Labels make clear these are searches, not directories."""
    company = row.organization.strip()
    if not company:
        return ()
    role_hint = _TYPE_SEARCH_HINT.get(row.opportunity_type, "interview experience")
    title_token = row.title.strip().replace("\N{EN DASH}", " - ").split(" - ")[0].strip()
    title_token = title_token[:60] if title_token else role_hint

    reddit_q = f'{company} {role_hint}'
    recruiter_q = f'{company} (recruiter OR "talent acquisition" OR "university recruiter")'
    asu_q = f'{company} ("Arizona State University" OR ASU OR "Arizona State")'

    return (
        ResearchLink(
            label="Interview notes (Reddit search)",
            url=f"https://www.reddit.com/search/?q={quote_plus(reddit_q)}&type=link&sort=new",
        ),
        ResearchLink(
            label="Recruiters (LinkedIn search)",
            url=f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(recruiter_q)}",
        ),
        ResearchLink(
            label="ASU connections (LinkedIn search)",
            url=f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(asu_q)}",
        ),
        ResearchLink(
            label="Role threads (Reddit)",
            url=(
                "https://www.reddit.com/search/?q="
                f"{quote_plus(f'{company} {title_token}')}&type=link&sort=relevance"
            ),
        ),
    )


def build_insights(
    row: models.Opportunity, *, now: datetime | None = None
) -> OpportunityInsights:
    reference = now or datetime.now(tz=UTC)
    freshness = None
    if _is_same_day(row.discovered_at, now=reference):
        freshness = "Opened today — apply while the link is live"
    return OpportunityInsights(
        freshness=freshness,
        focus_tags=focus_tags_for(row),
        research_links=research_links_for(row),
        disclaimer=(
            "Focus tags come from the listing and ACM SIG matches — not a job "
            "description dump and not interview outcomes. Research links are "
            "searches; verify before you treat anything as fact."
        ),
    )


def insights_embed_field_value(insights: OpportunityInsights) -> str:
    """Compact Discord field body for the stand-out panel."""
    lines: list[str] = []
    if insights.freshness:
        lines.append(f"**{insights.freshness}**")
    if insights.focus_tags:
        lines.append("Focus: " + " · ".join(f"`{tag}`" for tag in insights.focus_tags))
    for link in insights.research_links[:3]:
        lines.append(f"[{link.label}]({link.url})")
    short_disclaimer = (
        "Focus tags are listing themes + ACM SIGs, not JD dumps or interview outcomes. "
        "Links are searches — verify before you trust them."
    )
    lines.append(f"_{short_disclaimer}_")
    text = "\n".join(lines)
    if len(text) > 1024:
        return text[:1023] + "…"
    return text

