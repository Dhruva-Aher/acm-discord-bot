"""Shared HTTP behaviour for collectors: timeouts, retries and a polite UA.

Retries cover only failures that can plausibly succeed later - connection
errors, timeouts, 429 and 5xx. A 404 means the upstream path moved and no amount
of retrying will fix it, so it fails immediately and loudly.
"""

from __future__ import annotations

import asyncio
from typing import Final

import httpx

from acm_hub.observability.logging import get_logger

logger = get_logger(__name__)

USER_AGENT: Final = (
    "ACM-Hub/0.1 (+https://github.com/acm-asu; ACM at Arizona State University opportunity bot)"
)

RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({408, 425, 429, 500, 502, 503, 504})


def build_client(
    *,
    timeout_seconds: float = 30.0,
    headers: dict[str, str] | None = None,
) -> httpx.AsyncClient:
    """Create an async HTTP client with ACM Hub's defaults.

    The caller owns the client and is responsible for closing it, so a
    long-lived collector can reuse connections instead of reconnecting each run.
    """
    merged_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds),
        headers=merged_headers,
        follow_redirects=True,
    )


async def fetch_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    max_retries: int = 3,
    backoff_base_seconds: float = 1.0,
    sleep: object = None,
) -> httpx.Response:
    """GET ``url``, retrying transient failures with exponential backoff.

    Args:
        max_retries: Additional attempts after the first. ``0`` means try once.
        backoff_base_seconds: Delay before the first retry; doubles each time.
        sleep: Injectable async sleep, so tests do not wait in real time.

    Raises:
        httpx.HTTPStatusError: on a non-retryable status, or after the retry
            budget is exhausted.
        httpx.RequestError: when the final attempt fails at the transport level.
    """
    sleeper = sleep if sleep is not None else asyncio.sleep
    attempts = max_retries + 1
    last_exception: Exception | None = None

    for attempt in range(attempts):
        try:
            response = await client.get(url, headers=headers)
        except httpx.RequestError as exc:
            last_exception = exc
            if attempt == attempts - 1:
                raise
            logger.warning(
                "http.request_error",
                url=url,
                attempt=attempt + 1,
                error=f"{type(exc).__name__}: {exc}",
            )
        else:
            if response.status_code not in RETRYABLE_STATUS:
                response.raise_for_status()
                return response
            if attempt == attempts - 1:
                response.raise_for_status()
                return response
            logger.warning(
                "http.retryable_status",
                url=url,
                attempt=attempt + 1,
                status_code=response.status_code,
            )

        await sleeper(backoff_base_seconds * (2**attempt))  # type: ignore[operator]

    # Unreachable: the final iteration either returns or raises.
    raise RuntimeError(f"retry loop exited without a result for {url}") from last_exception
