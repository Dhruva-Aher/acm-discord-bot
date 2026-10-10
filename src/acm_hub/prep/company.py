"""Company interview-prep links.

ACM Hub does not invent "most asked" LeetCode lists. It points members at
LeetCode's public company tag page (community-maintained), Reddit searches for
OA / interview threads, and the listing's own focus themes as practice targets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote_plus

from acm_hub.bot.insights import focus_tags_for
from acm_hub.db import models

_LEGAL_SUFFIX = re.compile(
    r"\b(inc|inc\.|llc|ltd|ltd\.|corp|corporation|company|co\.|plc)\b",
    re.IGNORECASE,
)
_NON_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class PrepLink:
    """One clickable prep destination with short human copy."""

    label: str
    url: str
    blurb: str
    section: str


@dataclass(frozen=True, slots=True)
class CompanyPrep:
    company: str
    leetcode_company_url: str | None
    links: tuple[PrepLink, ...]
    practice_themes: tuple[str, ...]
    disclaimer: str


def company_slug(name: str) -> str | None:
    cleaned = _LEGAL_SUFFIX.sub("", name)
    slug = _NON_SLUG.sub("-", cleaned.lower()).strip("-")
    if len(slug) < 2:
        return None
    return slug


def links_for_section(prep: CompanyPrep, section: str) -> tuple[PrepLink, ...]:
    return tuple(link for link in prep.links if link.section == section)


def format_prep_section(links: tuple[PrepLink, ...]) -> str:
    """Render a prep section as scannable markdown lines."""
    return "\n".join(f"**[{link.label}]({link.url})**\n{link.blurb}" for link in links)


def build_company_prep(row: models.Opportunity) -> CompanyPrep:
    company = row.organization.strip() or "Unknown"
    short = company if len(company) <= 28 else company[:27].rstrip() + "…"
    slug = company_slug(company)
    leetcode = f"https://leetcode.com/company/{slug}/" if slug else None
    links: list[PrepLink] = []

    if leetcode:
        links.append(
            PrepLink(
                label=f"LeetCode · {short}",
                url=leetcode,
                blurb="Community-tagged problems people associate with this company.",
                section="Practice",
            )
        )
    links.append(
        PrepLink(
            label="OA & LeetCode threads",
            url=(
                "https://www.reddit.com/search/?q="
                f"{quote_plus(f'{company} leetcode OR OA OR \"online assessment\"')}&sort=new"
            ),
            blurb="Recent Reddit posts about assessments and coding rounds.",
            section="Practice",
        )
    )
    links.append(
        PrepLink(
            label="Interview stories",
            url=(
                "https://www.reddit.com/search/?q="
                f"{quote_plus(f'{company} internship interview')}&sort=relevance"
            ),
            blurb="What candidates say the process felt like — verify dates.",
            section="From candidates",
        )
    )
    if slug:
        links.append(
            PrepLink(
                label="Levels.fyi overview",
                url=f"https://www.levels.fyi/companies/{slug}/overview",
                blurb="Public level and pay snapshots when the company is listed.",
                section="From candidates",
            )
        )

    return CompanyPrep(
        company=company,
        leetcode_company_url=leetcode,
        links=tuple(links),
        practice_themes=focus_tags_for(row),
        disclaimer=(
            "Community sources only — not an official question bank. "
            "Cross-check before you trust a thread."
        ),
    )
