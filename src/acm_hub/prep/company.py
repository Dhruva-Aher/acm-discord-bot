"""Company interview-prep links.

ACM Hub does not invent "most asked" LeetCode lists. It points members at
LeetCode's public company tag page (community-maintained), Reddit searches for
OA / interview threads, and the listing's own focus themes as practice targets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote_plus

from acm_hub.bot.insights import ResearchLink, focus_tags_for
from acm_hub.db import models

_LEGAL_SUFFIX = re.compile(
    r"\b(inc|inc\.|llc|ltd|ltd\.|corp|corporation|company|co\.|plc)\b",
    re.IGNORECASE,
)
_NON_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class CompanyPrep:
    company: str
    leetcode_company_url: str | None
    links: tuple[ResearchLink, ...]
    practice_themes: tuple[str, ...]
    disclaimer: str


def company_slug(name: str) -> str | None:
    cleaned = _LEGAL_SUFFIX.sub("", name)
    slug = _NON_SLUG.sub("-", cleaned.lower()).strip("-")
    if len(slug) < 2:
        return None
    return slug


def build_company_prep(row: models.Opportunity) -> CompanyPrep:
    company = row.organization.strip() or "Unknown"
    slug = company_slug(company)
    leetcode = f"https://leetcode.com/company/{slug}/" if slug else None
    links: list[ResearchLink] = []
    if leetcode:
        links.append(
            ResearchLink(
                label="LeetCode company tag (community)",
                url=leetcode,
            )
        )
    links.append(
        ResearchLink(
            label="Reddit: OA / LeetCode threads",
            url=(
                "https://www.reddit.com/search/?q="
                f"{quote_plus(f'{company} leetcode OR OA OR \"online assessment\"')}&sort=new"
            ),
        )
    )
    links.append(
        ResearchLink(
            label="Reddit: interview experience",
            url=(
                "https://www.reddit.com/search/?q="
                f"{quote_plus(f'{company} internship interview')}&sort=relevance"
            ),
        )
    )
    if slug:
        links.append(
            ResearchLink(
                label="Levels.fyi company search",
                url=f"https://www.levels.fyi/companies/{slug}/overview",
            )
        )
    return CompanyPrep(
        company=company,
        leetcode_company_url=leetcode,
        links=tuple(links),
        practice_themes=focus_tags_for(row),
        disclaimer=(
            "LeetCode company pages and Reddit threads are community-sourced and "
            "incomplete. ACM Hub does not invent a private “most asked” question list."
        ),
    )
