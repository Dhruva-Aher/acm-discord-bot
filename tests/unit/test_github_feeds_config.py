"""GitHub internship feed configuration parsing."""

from __future__ import annotations

import pytest

from acm_hub.collectors.github.feeds_config import (
    GitHubListingsFeed,
    dedupe_feeds,
    parse_internship_github_feeds,
    repo_name_is_cs_job_list,
)
from acm_hub.domain.enums import OpportunityType


def test_parse_default_style_entries() -> None:
    feeds = parse_internship_github_feeds(
        "SimplifyJobs/Summer2027-Internships@dev|Summer 2027;"
        "SimplifyJobs/New-Grad-Positions|New grad"
    )
    assert len(feeds) == 2
    assert feeds[0].slug == "SimplifyJobs/Summer2027-Internships"
    assert feeds[0].label == "Summer 2027"
    assert feeds[1].branch == "dev"


def test_invalid_entry_raises() -> None:
    with pytest.raises(ValueError, match="invalid internship GitHub feed"):
        parse_internship_github_feeds("not-a-valid-entry")


def test_dedupe_feeds() -> None:
    feed = GitHubListingsFeed(owner="SimplifyJobs", repo="Summer2027-Internships")
    assert len(dedupe_feeds([feed, feed])) == 1


def test_opportunity_type_inference() -> None:
    intern = GitHubListingsFeed(owner="SimplifyJobs", repo="Summer2027-Internships")
    grad = GitHubListingsFeed(owner="SimplifyJobs", repo="New-Grad-Positions")
    assert intern.inferred_opportunity_type() is OpportunityType.INTERNSHIP
    assert grad.inferred_opportunity_type() is OpportunityType.PROGRAM


@pytest.mark.parametrize(
    ("name", "year", "expected"),
    [
        ("Summer2027-Internships", 2026, True),
        ("2024-New-Grad-Tech-Jobs", 2026, False),
        ("New-Grad-Positions", 2026, True),
        ("random-tooling", 2026, False),
    ],
)
def test_repo_name_filter(name: str, year: int, expected: bool) -> None:
    assert repo_name_is_cs_job_list(name, current_year=year) is expected
