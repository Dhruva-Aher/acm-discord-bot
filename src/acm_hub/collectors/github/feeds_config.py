"""Configuration for GitHub ``listings.json`` internship feeds.

Feeds are declared in ``INTERNSHIP_GITHUB_FEEDS`` as semicolon-separated entries::

    owner/repo@branch|Human label

Branch defaults to ``dev``. Path defaults to ``.github/scripts/listings.json``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from acm_hub.domain.enums import OpportunityType

DEFAULT_LISTINGS_PATH: Final = ".github/scripts/listings.json"
DEFAULT_BRANCH: Final = "dev"

DEFAULT_INTERNSHIP_GITHUB_FEEDS: Final = (
    "SimplifyJobs/Summer2027-Internships@dev|Summer 2027 internships;"
    "SimplifyJobs/New-Grad-Positions@dev|New grad tech roles"
)

_FEED_PATTERN = re.compile(
    r"^\s*(?P<owner>[^/\s]+)/(?P<repo>[^@\s|]+)(?:@(?P<branch>[^|\s]+))?"
    r"(?:\|(?P<label>[^;]+))?\s*$"
)

_CS_REPO_NAME = re.compile(r"(intern|new-grad|new_grad|positions|tech-jobs)", re.IGNORECASE)
_YEAR_IN_NAME = re.compile(r"(20\d{2})")


@dataclass(frozen=True, slots=True)
class GitHubListingsFeed:
    """One public GitHub repository that publishes a Simplify-style JSON feed."""

    owner: str
    repo: str
    branch: str = DEFAULT_BRANCH
    path: str = DEFAULT_LISTINGS_PATH
    label: str | None = None

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def source_page_url(self) -> str:
        return (
            f"https://github.com/{self.owner}/{self.repo}/blob/{self.branch}/{self.path}"
        )

    def inferred_opportunity_type(self) -> OpportunityType:
        name = self.repo.lower()
        if "new-grad" in name or "new_grad" in name:
            return OpportunityType.PROGRAM
        return OpportunityType.INTERNSHIP


def parse_internship_github_feeds(raw: str) -> list[GitHubListingsFeed]:
    """Parse ``INTERNSHIP_GITHUB_FEEDS`` into feed definitions."""
    if not raw.strip():
        return []
    feeds: list[GitHubListingsFeed] = []
    for chunk in raw.split(";"):
        piece = chunk.strip()
        if not piece:
            continue
        match = _FEED_PATTERN.match(piece)
        if not match:
            raise ValueError(f"invalid internship GitHub feed entry: {piece!r}")
        owner = match.group("owner")
        repo = match.group("repo")
        branch = match.group("branch") or DEFAULT_BRANCH
        label = (match.group("label") or "").strip() or None
        feeds.append(
            GitHubListingsFeed(owner=owner, repo=repo, branch=branch, label=label)
        )
    return feeds


def parse_discover_orgs(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def dedupe_feeds(feeds: Sequence[GitHubListingsFeed]) -> list[GitHubListingsFeed]:
    seen: set[str] = set()
    unique: list[GitHubListingsFeed] = []
    for feed in feeds:
        if feed.slug in seen:
            continue
        seen.add(feed.slug)
        unique.append(feed)
    return unique


def repo_name_is_cs_job_list(name: str, *, current_year: int) -> bool:
    """Whether an org repo name looks like a live CS internship/new-grad list."""
    if not _CS_REPO_NAME.search(name):
        return False
    year_match = _YEAR_IN_NAME.search(name)
    if year_match is None:
        return True
    year = int(year_match.group(1))
    return year >= current_year - 1

