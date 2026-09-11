"""Detect anti-bot challenge pages so collectors fail instead of parsing them.

ACM Hub does not bypass Cloudflare, CAPTCHAs or login walls. When a source
returns a challenge page, the collector must raise rather than invent an empty
catalogue or scrape a block message as if it were listings.
"""

from __future__ import annotations

from typing import Final

#: Substrings that mean the response is an interstitial, not the source.
BLOCK_MARKERS: Final[tuple[str, ...]] = (
    "sorry, you have been blocked",
    "attention required! | cloudflare",
    "cf-browser-verification",
    "cdn-cgi/challenge-platform",
    "enable javascript and cookies to continue",
    "you are unable to access",
)


def looks_like_bot_protection(body: str, *, content_type: str | None = None) -> bool:
    """Return True when ``body`` is a bot-protection page rather than source data."""
    del content_type
    lowered = body.lower()
    if any(marker in lowered for marker in BLOCK_MARKERS):
        return True
    # Cloudflare's JS challenge is often a 200 with this title, not the hard block.
    return "just a moment" in lowered and "cloudflare" in lowered
