"""HTML digest rendering.

The digest must mirror Discord embeds: only source-stated facts, with Unknown
where the source was silent, and escaped HTML so titles cannot break the page.
"""

from __future__ import annotations

from acm_hub.db import models
from acm_hub.db.repositories.opportunities import sig_matches_to_json
from acm_hub.digest.html import DEFAULT_DIGEST_LIMIT, build_opportunities_html
from acm_hub.domain.enums import (
    OpportunityStatus,
    OpportunityType,
    SponsorshipStatus,
)
from acm_hub.pipeline.classify import classify_opportunity
from acm_hub.pipeline.normalize import normalize_opportunity
from tests.conftest import FROZEN_NOW, make_raw


def build_row(**kwargs: object) -> models.Opportunity:
    opportunity = classify_opportunity(
        normalize_opportunity(make_raw(**kwargs), discovered_at=FROZEN_NOW)  # type: ignore[arg-type]
    )
    return models.Opportunity(
        id=opportunity.id,
        canonical_key=opportunity.canonical_key,
        content_hash=opportunity.content_hash,
        source_name=opportunity.source_name,
        source_external_id=opportunity.source_external_id,
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
        posted_at=opportunity.posted_at,
        discovered_at=opportunity.discovered_at,
        expires_at=opportunity.expires_at,
        status=OpportunityStatus.PUBLISHED,
        verification_status=opportunity.verification_status,
    )


class TestBuildOpportunitiesHtml:
    def test_includes_source_stated_metadata(self) -> None:
        row = build_row(
            title="Data Science Intern",
            organization="Lyft",
            application_url="https://example.test/lyft",
            sponsorship=SponsorshipStatus.UNKNOWN,
            degree_requirements=("Master's", "PhD"),
            opportunity_type=OpportunityType.INTERNSHIP,
        )
        result = build_opportunities_html(
            [row],
            total_matching=1,
            source_display_names={"test_source": "Simplify / Pitt CSC"},
            generated_at=FROZEN_NOW,
        )
        assert "Data Science Intern" in result.html
        assert "https://example.test/lyft" in result.html
        assert "Lyft" in result.html
        assert "Sponsorship: Unknown" in result.html
        assert "Degrees: Master&#x27;s, PhD" in result.html
        assert "Simplify / Pitt CSC" in result.html
        assert result.included == 1
        assert result.truncated is False

    def test_escapes_html_in_titles(self) -> None:
        row = build_row(title='Intern <script>alert("x")</script>', organization="Acme & Co")
        result = build_opportunities_html([row], total_matching=1, generated_at=FROZEN_NOW)
        assert "<script>" not in result.html
        assert "&lt;script&gt;" in result.html
        assert "Acme &amp; Co" in result.html

    def test_empty_catalogue_is_explicit(self) -> None:
        result = build_opportunities_html([], total_matching=0, generated_at=FROZEN_NOW)
        assert "No published opportunities match." in result.html
        assert result.included == 0

    def test_truncation_is_disclosed(self) -> None:
        rows = [
            build_row(
                title=f"Role {index}",
                application_url=f"https://example.test/{index}",
                source_external_id=f"ext-{index}",
            )
            for index in range(3)
        ]
        result = build_opportunities_html(rows, total_matching=10, generated_at=FROZEN_NOW)
        assert result.truncated is True
        assert "3 most recently discovered of 10" in result.html

    def test_default_limit_is_documented(self) -> None:
        assert DEFAULT_DIGEST_LIMIT == 200
