"""Opportunity embeds.

The hard rule these builders exist to enforce: **nothing appears in an embed
that the source did not state**. There is no salary field, no invented deadline,
no "sponsorship available" guess. Where a source is silent, the field is either
omitted or explicitly rendered as ``Unknown``, so a member never reads an ACM
Hub post as a claim ACM never made.

Discord gives us almost no layout control — hierarchy has to come from a short
title, one glance line, and only the fields that earn their space.
"""

from __future__ import annotations

from datetime import UTC, datetime

import discord

from acm_hub.bot.insights import (
    build_insights,
    focus_field_value,
    research_field_value,
)
from acm_hub.db import models
from acm_hub.db.repositories.opportunities import sig_matches_from_json
from acm_hub.domain.enums import (
    OpportunityStatus,
    OpportunityType,
    RemoteStatus,
    SponsorshipStatus,
)

MAX_LOCATIONS_SHOWN = 4

TYPE_HEADINGS: dict[OpportunityType, str] = {
    OpportunityType.INTERNSHIP: "Internship",
    OpportunityType.RESEARCH: "Research",
    OpportunityType.FELLOWSHIP: "Fellowship",
    OpportunityType.HACKATHON: "Hackathon",
    OpportunityType.SCHOLARSHIP: "Scholarship",
    OpportunityType.CONFERENCE: "Conference",
    OpportunityType.PROGRAM: "New grad",
    OpportunityType.OTHER: "Opportunity",
}

REMOTE_LABELS: dict[RemoteStatus, str] = {
    RemoteStatus.REMOTE: "Remote",
    RemoteStatus.HYBRID: "Hybrid",
    RemoteStatus.ONSITE: "On-site",
    RemoteStatus.UNKNOWN: "Unknown",
}

#: Longer labels for digests / detailed surfaces.
SPONSORSHIP_LABELS: dict[SponsorshipStatus, str] = {
    SponsorshipStatus.OFFERS_SPONSORSHIP: "Offers sponsorship (per source)",
    SponsorshipStatus.NO_SPONSORSHIP: "Does not offer sponsorship (per source)",
    SponsorshipStatus.CITIZENSHIP_REQUIRED: "U.S. citizenship required (per source)",
    SponsorshipStatus.UNKNOWN: "Unknown",
}

#: Short forms for the Discord glance line; still explicit, never invents eligibility.
SPONSORSHIP_GLANCE: dict[SponsorshipStatus, str] = {
    SponsorshipStatus.OFFERS_SPONSORSHIP: "Sponsorship offered (source)",
    SponsorshipStatus.NO_SPONSORSHIP: "No sponsorship (source)",
    SponsorshipStatus.CITIZENSHIP_REQUIRED: "U.S. citizenship required (source)",
    SponsorshipStatus.UNKNOWN: "Sponsorship unknown",
}

ACM_GOLD = discord.Colour(0xFFC627)
ACM_MAROON = discord.Colour(0x8C1D40)

ACM_GREY = discord.Colour(0x6E6E6E)

STATUS_COLOURS: dict[OpportunityStatus, discord.Colour] = {
    OpportunityStatus.PUBLISHED: ACM_GOLD,
    OpportunityStatus.APPROVED: ACM_GOLD,
    OpportunityStatus.REJECTED: ACM_GREY,
    OpportunityStatus.CLOSED: ACM_GREY,
}

CLOSED_PREFIX = "[CLOSED] "


def format_locations(locations: list[str], remote_status: RemoteStatus) -> str:
    """Render locations for display, truncating long lists rather than dropping them."""
    if not locations:
        if remote_status is not RemoteStatus.UNKNOWN:
            return REMOTE_LABELS[remote_status]
        return "Unknown"

    shown = locations[:MAX_LOCATIONS_SHOWN]
    text = " / ".join(shown)
    remaining = len(locations) - len(shown)
    if remaining > 0:
        text += f" (+{remaining} more)"
    return text


def truncate(text: str, limit: int) -> str:
    """Shorten ``text`` to ``limit`` characters with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"


def title_with_company(title: str, organization: str, *, limit: int = 250) -> str:
    """``Role @ Company`` so a scrolling feed answers who and where in one glance."""
    role = title.strip()
    company = organization.strip()
    if not company:
        return truncate(role, limit)
    separator = " @ "
    if len(role) + len(separator) + len(company) <= limit:
        return f"{role}{separator}{company}"
    # Prefer keeping a readable company over an ultra-long role string.
    company_part = truncate(company, min(len(company), 72))
    role_budget = limit - len(separator) - len(company_part)
    return f"{truncate(role, max(role_budget, 16))}{separator}{company_part}"


def _sig_labels(row: models.Opportunity) -> str | None:
    matches = sig_matches_from_json(row.sig_matches)
    if not matches:
        return None
    return " · ".join(match.sig for match in matches)


def glance_line(row: models.Opportunity) -> str:
    """Single description line: place, eligibility, SIGs when matched."""
    parts: list[str] = []
    location = format_locations(list(row.locations), row.remote_status)
    parts.append(f"**{location}**" if location != "Unknown" else "Location unknown")
    parts.append(SPONSORSHIP_GLANCE[row.sponsorship])
    if row.citizenship_requirement:
        parts.append(row.citizenship_requirement)
    if row.degree_requirements:
        parts.append(", ".join(row.degree_requirements))
    sigs = _sig_labels(row)
    if sigs:
        parts.append(sigs)
    return " · ".join(parts)


def _discovered_label(discovered_at: datetime | None, *, now: datetime | None = None) -> str:
    if discovered_at is None:
        return "Unknown"
    reference = now or datetime.now(tz=UTC)
    if discovered_at.tzinfo is None:
        discovered_at = discovered_at.replace(tzinfo=UTC)
    delta_days = (reference.date() - discovered_at.date()).days
    if delta_days <= 0:
        return "Today"
    if delta_days == 1:
        return "Yesterday"
    return f"{delta_days} days ago"


def build_opportunity_embed(
    row: models.Opportunity,
    *,
    source_display_name: str | None = None,
    now: datetime | None = None,
) -> discord.Embed:
    """Build the member-facing embed for a published opportunity."""
    heading = TYPE_HEADINGS.get(row.opportunity_type, TYPE_HEADINGS[OpportunityType.OTHER])
    insights = build_insights(row, now=now)
    if insights.freshness:
        heading = f"{heading} · Opened today"

    embed = discord.Embed(
        title=title_with_company(row.title, row.organization),
        url=row.application_url,
        colour=STATUS_COLOURS.get(row.status, ACM_GOLD),
        description=glance_line(row),
    )
    embed.set_author(name=heading)

    focus = focus_field_value(insights)
    if focus:
        embed.add_field(name="Focus", value=focus, inline=False)

    research = research_field_value(insights)
    if research:
        embed.add_field(name="Look up", value=research, inline=False)

    footer = source_display_name or row.source_name
    discovered = _discovered_label(row.discovered_at, now=now)
    embed.set_footer(text=f"{footer} · {discovered} · Look-up links are searches")
    return embed


def mark_embed_closed(embed: discord.Embed) -> discord.Embed:
    """Restyle a published embed to say the posting is no longer open.

    The title is prefixed rather than the post deleted, so a member who
    remembers seeing the opportunity can still find it and see what happened
    instead of wondering whether they imagined it. The application link is
    removed because following it is no longer useful, and the accent colour
    drops to grey so a closed post is distinguishable while scrolling.
    """
    closed = embed.copy()
    title = closed.title or ""
    if not title.startswith(CLOSED_PREFIX):
        closed.title = truncate(f"{CLOSED_PREFIX}{title}", 250)
    closed.url = None
    closed.colour = ACM_GREY
    closed.set_author(name="Closed")
    closed.add_field(
        name="Status",
        value="This listing is no longer published by the source.",
        inline=False,
    )
    return closed


def build_review_embed(
    row: models.Opportunity,
    *,
    source_display_name: str | None = None,
    now: datetime | None = None,
) -> discord.Embed:
    """Build the officer-facing review card.

    Same facts as the public embed plus the routing information an officer needs
    to make a decision: the SIG scores that drove classification, and how much
    ACM Hub actually verified.
    """
    embed = build_opportunity_embed(row, source_display_name=source_display_name, now=now)
    embed.set_author(name="Pending Review")
    embed.colour = ACM_MAROON

    matches = sig_matches_from_json(row.sig_matches)
    if matches:
        embed.add_field(
            name="Classifier",
            value="\n".join(
                f"{match.sig}: {match.score:.2f} ({', '.join(match.matched_terms[:4])})"
                for match in matches
            ),
            inline=False,
        )
    else:
        embed.add_field(name="Classifier", value="No SIG matched", inline=False)

    embed.add_field(
        name="Verification",
        value=f"{row.verification_status.value.replace('_', ' ')}",
        inline=True,
    )
    embed.add_field(name="Type", value=row.opportunity_type.value, inline=True)
    return embed
