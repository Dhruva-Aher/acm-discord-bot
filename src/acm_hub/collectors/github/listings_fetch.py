"""HTTP fetch helpers for GitHub ``listings.json`` feeds."""

from __future__ import annotations

import json
from typing import Any

import httpx

from acm_hub.collectors.github.feeds_config import GitHubListingsFeed
from acm_hub.collectors.http import fetch_with_retry
from acm_hub.observability.logging import get_logger

logger = get_logger(__name__)

GITHUB_RAW_ACCEPT = "application/vnd.github.raw+json"
GITHUB_API_VERSION = "2022-11-28"


def feed_api_url(feed: GitHubListingsFeed) -> str:
    return (
        f"https://api.github.com/repos/{feed.owner}/{feed.repo}/contents/"
        f"{feed.path}?ref={feed.branch}"
    )


def feed_raw_url(feed: GitHubListingsFeed) -> str:
    return f"https://raw.githubusercontent.com/{feed.owner}/{feed.repo}/{feed.branch}/{feed.path}"


async def fetch_listings_payload(
    client: httpx.AsyncClient,
    feed: GitHubListingsFeed,
    *,
    max_retries: int,
    single_url: str | None = None,
) -> Any:
    """Fetch and decode one feed's JSON array."""
    if single_url is not None:
        attempts = [(single_url, {})]
    else:
        attempts = [
            (
                feed_api_url(feed),
                {
                    "Accept": GITHUB_RAW_ACCEPT,
                    "X-GitHub-Api-Version": GITHUB_API_VERSION,
                },
            ),
            (feed_raw_url(feed), {}),
        ]

    errors: list[str] = []
    for url, request_headers in attempts:
        try:
            response = await fetch_with_retry(
                client,
                url,
                headers=request_headers or None,
                max_retries=max_retries,
            )
        except httpx.HTTPStatusError as exc:
            detail = f"HTTP {exc.response.status_code} for {url}"
            if exc.response.status_code in {403, 404, 429}:
                errors.append(detail)
                logger.warning(
                    "github_listings.feed_attempt_failed",
                    feed=feed.slug,
                    url=url,
                    status_code=exc.response.status_code,
                )
                continue
            raise
        except httpx.RequestError as exc:
            errors.append(f"{type(exc).__name__} for {url}: {exc}")
            logger.warning(
                "github_listings.feed_attempt_failed",
                feed=feed.slug,
                url=url,
                error=f"{type(exc).__name__}: {exc}",
            )
            continue
        else:
            try:
                return response.json()
            except json.JSONDecodeError as exc:
                raise ValueError(f"feed was not valid JSON: {exc}") from exc

    raise RuntimeError("; ".join(errors) if errors else f"could not fetch {feed.slug}")
