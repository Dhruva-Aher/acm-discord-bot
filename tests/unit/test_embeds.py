"""Embed rendering.

The rule these tests enforce is the one that protects ACM's credibility:
an embed states only what a source stated. Unknown eligibility must read as
"Unknown", never be omitted in a way that implies eligibility, and no field may
appear that the pipeline never populated.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import discord
import pytest

from acm_hub.bot.embeds.opportunity import (
    CLOSED_PREFIX,
    MAX_LOCATIONS_SHOWN,
    build_opportunity_embed,
    build_review_embed,
    format_locations,
    glance_line,
    mark_embed_closed,
    title_with_company,
    truncate,
)
from acm_hub.db import models
from acm_hub.db.repositories.opportunities import sig_matches_to_json
from acm_hub.domain.enums import (
    OpportunityStatus,
    OpportunityType,
    RemoteStatus,
    SponsorshipStatus,
    VerificationStatus,
)
from acm_hub.pipeline.classify import classify_opportunity
from acm_hub.pipeline.normalize import normalize_opportunity
from tests.conftest import FROZEN_NOW, make_raw


def build_row(**kwargs: object) -> models.Opportunity:
    """Build an unsaved ORM row from a normalized, classified opportunity."""
    opportunity = classify_opportunity(
        normalize_opportunity(make_raw(**kwargs), discovered_at=FROZEN_NOW)  # type: ignore[arg-type]
    )
    return models.Opportunity(
        id=opportunity.id,
        canonical_key=opportunity.canonical_key,
        content_hash=opportunity.content_hash,
        source_name=opportunity.source_name,
        source_url=opportunity.source_url,
        application_url=opportunity.application_url,
        title=opportunity.title,
        organization=opportunity.organization,
        description=opportunity.description,
        opportunity_type=opportunity.opportunity_type,
        locations=list(opportunity.locations),
        remote_status=opportunity.remote_status,
        tags=list(opportunity.tags),
        sig_matches=sig_matches_to_json(opportunity.sig_matches),
        sponsorship=opportunity.eligibility.sponsorship,
        citizenship_requirement=opportunity.eligibility.citizenship_requirement,
        degree_requirements=list(opportunity.eligibility.degree_requirements),
        discovered_at=FROZEN_NOW,
        status=OpportunityStatus.PUBLISHED,
        verification_status=opportunity.verification_status,
    )


def field_value(embed: discord.Embed, name: str) -> str:
    """Read one field's value, failing the test if the field is absent."""
    return str(next(field.value for field in embed.fields if field.name == name))


class TestOpportunityEmbed:
    def test_shows_the_core_facts(self) -> None:
        embed = build_opportunity_embed(build_row(), now=FROZEN_NOW)
        assert embed.title == "Software Engineering Intern @ Cloudflare"
        assert embed.url == "https://boards.example.com/jobs/123"
        assert embed.description is not None
        assert "Cloudflare" in (embed.title or "")
        assert "Sponsorship unknown" in embed.description

    def test_unknown_sponsorship_is_shown_as_unknown(self) -> None:
        """Silence must never render as eligibility."""
        embed = build_opportunity_embed(build_row(), now=FROZEN_NOW)
        assert embed.description is not None
        assert "Sponsorship unknown" in embed.description

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (SponsorshipStatus.OFFERS_SPONSORSHIP, "Sponsorship offered (source)"),
            (SponsorshipStatus.NO_SPONSORSHIP, "No sponsorship (source)"),
            (SponsorshipStatus.CITIZENSHIP_REQUIRED, "U.S. citizenship required (source)"),
        ],
    )
    def test_stated_sponsorship_is_attributed_to_the_source(
        self, status: SponsorshipStatus, expected: str
    ) -> None:
        embed = build_opportunity_embed(build_row(sponsorship=status), now=FROZEN_NOW)
        assert embed.description is not None
        assert expected in embed.description

    def test_no_salary_or_deadline_fields_are_invented(self) -> None:
        embed = build_opportunity_embed(build_row(), now=FROZEN_NOW)
        names = {str(field.name).lower() for field in embed.fields}
        assert not names & {"salary", "compensation", "pay", "deadline", "requirements"}

    def test_card_uses_compact_hierarchy_without_stand_out(self) -> None:
        embed = build_opportunity_embed(
            build_row(organization="Microsoft", title="Software Engineering Intern"),
            now=FROZEN_NOW,
        )
        assert embed.author is not None
        assert embed.author.name is not None
        assert "Opened today" in embed.author.name
        assert "Internship" in embed.author.name
        assert embed.title == "Software Engineering Intern @ Microsoft"
        names = {str(field.name) for field in embed.fields}
        assert "Stand out" not in names
        assert "Eligibility" not in names
        assert "Organization" not in names
        assert "Relevant SIGs" not in names
        assert "Look up" in names
        look_up = field_value(embed, "Look up")
        assert "Interviews" in look_up
        assert "Recruiters" in look_up
        assert "ASU alumni" in look_up
        assert embed.footer is not None
        assert embed.footer.text is not None
        assert "searches" in embed.footer.text.lower()

    def test_glance_line_carries_location_and_eligibility(self) -> None:
        row = build_row(locations=("Austin, TX",), degree_requirements=("Bachelor's",))
        line = glance_line(row)
        assert "Austin, TX" in line
        assert "Sponsorship unknown" in line
        assert "Bachelor's" in line

    def test_no_dense_field_grid_when_sigs_are_absent(self) -> None:
        embed = build_opportunity_embed(
            build_row(title="Marketing Communications Intern"), now=FROZEN_NOW
        )
        names = {str(field.name) for field in embed.fields}
        assert "SIGs" not in names
        assert "Company" not in names
        assert "Location" not in names

    def test_matched_sigs_appear_in_the_glance_line(self) -> None:
        embed = build_opportunity_embed(build_row(title="Machine Learning Intern"), now=FROZEN_NOW)
        assert embed.description is not None
        assert "SIGAI" in embed.description

    def test_the_source_is_attributed_in_the_footer(self) -> None:
        embed = build_opportunity_embed(
            build_row(), source_display_name="Simplify / Pitt CSC", now=FROZEN_NOW
        )
        assert embed.footer.text is not None
        assert "Simplify / Pitt CSC" in embed.footer.text

    @pytest.mark.parametrize(
        ("days_ago", "expected"),
        [(0, "Today"), (1, "Yesterday"), (5, "5 days ago")],
    )
    def test_discovery_recency_is_human_readable(self, days_ago: int, expected: str) -> None:
        row = build_row()
        row.discovered_at = FROZEN_NOW - timedelta(days=days_ago)
        embed = build_opportunity_embed(row, now=FROZEN_NOW)
        assert embed.footer.text is not None
        assert expected in embed.footer.text

    def test_a_naive_stored_timestamp_does_not_crash_rendering(self) -> None:
        row = build_row()
        row.discovered_at = datetime(2026, 9, 10, 12, 0)
        embed = build_opportunity_embed(row, now=FROZEN_NOW)
        assert embed.footer.text is not None

    def test_the_embed_stays_within_discord_limits(self) -> None:
        row = build_row(
            title="x" * 500,
            organization="y" * 400,
            description="z" * 5000,
            locations=tuple(f"City {index}, ST" for index in range(50)),
        )
        embed = build_opportunity_embed(row, now=FROZEN_NOW)
        assert embed.title is not None and len(embed.title) <= 256
        assert len(embed) <= 6000
        for field in embed.fields:
            assert field.value is not None and len(field.value) <= 1024


class TestTitleWithCompany:
    def test_joins_role_and_company(self) -> None:
        assert title_with_company("Firmware Intern", "Zipline") == "Firmware Intern @ Zipline"

    def test_falls_back_when_company_is_blank(self) -> None:
        assert title_with_company("Firmware Intern", "  ") == "Firmware Intern"

    def test_respects_discord_title_limit(self) -> None:
        result = title_with_company("x" * 200, "y" * 80, limit=250)
        assert len(result) <= 250
        assert result.endswith(" @ " + "y" * 80) or result.endswith("…")


class TestLocationRendering:
    def test_multiple_locations_are_joined(self) -> None:
        assert format_locations(["Austin, TX", "NYC"], RemoteStatus.UNKNOWN) == "Austin, TX / NYC"

    def test_long_location_lists_are_summarized(self) -> None:
        locations = [f"City {index}" for index in range(10)]
        rendered = format_locations(locations, RemoteStatus.UNKNOWN)
        assert rendered.endswith(f"(+{10 - MAX_LOCATIONS_SHOWN} more)")

    def test_no_location_and_no_remote_signal_reads_unknown(self) -> None:
        assert format_locations([], RemoteStatus.UNKNOWN) == "Unknown"

    def test_remote_only_role_reads_remote(self) -> None:
        assert format_locations([], RemoteStatus.REMOTE) == "Remote"


class TestReviewEmbed:
    def test_shows_the_classifier_reasoning(self) -> None:
        embed = build_review_embed(build_row(title="Machine Learning Intern"), now=FROZEN_NOW)
        classifier = field_value(embed, "Classifier")
        assert "SIGAI" in classifier
        assert "machine learning" in classifier

    def test_states_how_much_was_actually_verified(self) -> None:
        row = build_row()
        row.verification_status = VerificationStatus.SOURCE_CLAIMED
        embed = build_review_embed(row, now=FROZEN_NOW)
        assert field_value(embed, "Verification") == "source claimed"

    def test_marks_the_card_as_pending(self) -> None:
        embed = build_review_embed(build_row(), now=FROZEN_NOW)
        assert embed.author.name == "Pending Review"

    def test_shows_the_opportunity_type(self) -> None:
        row = build_row()
        row.opportunity_type = OpportunityType.RESEARCH
        assert field_value(build_review_embed(row, now=FROZEN_NOW), "Type") == "research"

    def test_stays_within_discord_limits_for_a_maximal_record(self) -> None:
        row = build_row(
            title="Machine Learning Deep Learning Computer Vision Security Graphics Intern " * 5,
            description="machine learning " * 300,
        )
        embed = build_review_embed(row, now=FROZEN_NOW)
        assert len(embed) <= 6000


class TestTruncate:
    def test_short_text_is_untouched(self) -> None:
        assert truncate("hello", 10) == "hello"

    def test_long_text_is_ellipsized_within_the_limit(self) -> None:
        result = truncate("x" * 50, 10)
        assert len(result) <= 10
        assert result.endswith("\u2026")


def test_unreadable_sig_json_degrades_gracefully() -> None:
    """A schema change should cost the SIG badges, not the whole post."""
    row = build_row()
    row.sig_matches = [{"unexpected": "shape"}]
    embed = build_opportunity_embed(row, now=datetime.now(tz=UTC))
    assert embed.description is not None
    assert "SIG" not in embed.description or "Sponsorship" in embed.description
    assert "Software Engineering Intern" in (embed.title or "")


class TestClosedEmbed:
    def test_the_title_is_prefixed_and_the_link_is_removed(self) -> None:
        original = build_opportunity_embed(build_row(), now=FROZEN_NOW)
        closed = mark_embed_closed(original)
        assert closed.title is not None
        assert closed.title.startswith(CLOSED_PREFIX)
        assert closed.url is None
        assert closed.author.name == "Closed"
        assert "no longer published" in field_value(closed, "Status")

    def test_prefixing_twice_does_not_stack(self) -> None:
        original = build_opportunity_embed(build_row(), now=FROZEN_NOW)
        twice = mark_embed_closed(mark_embed_closed(original))
        assert twice.title is not None
        assert twice.title.count(CLOSED_PREFIX) == 1

    def test_the_closed_embed_stays_within_discord_limits(self) -> None:
        row = build_row(title="x" * 500, description="z" * 5000)
        embed = mark_embed_closed(build_opportunity_embed(row, now=FROZEN_NOW))
        assert embed.title is not None and len(embed.title) <= 256
        assert len(embed) <= 6000
