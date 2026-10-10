"""Discover CS job-list repositories inside a GitHub organization."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from acm_hub.collectors.github.feeds_config import (
    DEFAULT_BRANCH,
    GitHubListingsFeed,
    repo_name_is_cs_job_list,
)
from acm_hub.collectors.http import fetch_with_retry
from acm_hub.observability.logging import get_logger

logger = get_logger(__name__)

GITHUB_API_VERSION = "2022-11-28"


async def discover_org_listing_feeds(
    client: httpx.AsyncClient,
    org: str,
    *,
    max_retries: int = 1,
) -> list[GitHubListingsFeed]:
    """Return feeds for org repos that expose ``listings.json`` on ``dev``."""
    repos = await _list_org_repos(client, org, max_retries=max_retries)
    current_year = datetime.now(tz=UTC).year
    feeds: list[GitHubListingsFeed] = []

    for repo in repos:
        if repo.get("archived"):
            continue
        name = repo.get("name")
        if not isinstance(name, str):
            continue
        if not repo_name_is_cs_job_list(name, current_year=current_year):
            continue
        feed = GitHubListingsFeed(owner=org, repo=name, branch=DEFAULT_BRANCH)
        if await _listings_exist(client, feed, max_retries=max_retries):
            feeds.append(feed)
            logger.info("github_listings.discovered_feed", org=org, repo=name)

    return feeds


async def _list_org_repos(
    client: httpx.AsyncClient, org: str, *, max_retries: int
) -> list[dict[str, object]]:
    url = f"https://api.github.com/orgs/{org}/repos?per_page=100&type=public"
    response = await fetch_with_retry(
        client,
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
        max_retries=max_retries,
    )
    payload = response.json()
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


async def _listings_exist(
    client: httpx.AsyncClient, feed: GitHubListingsFeed, *, max_retries: int
) -> bool:
    url = (
        f"https://api.github.com/repos/{feed.owner}/{feed.repo}/contents/"
        f"{feed.path}?ref={feed.branch}"
    )
    try:
        response = await fetch_with_retry(
            client,
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
            max_retries=max_retries,
        )
    except httpx.HTTPStatusError:
        return False
    except httpx.RequestError:
        return False
    return response.status_code == 200
