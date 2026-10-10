"""Resume PDF theme gap analysis (no LLM)."""

from __future__ import annotations

from acm_hub.bot.member_actions import build_company_prep_view, company_prep_embed
from acm_hub.db import models
from acm_hub.db.repositories.opportunities import sig_matches_to_json
from acm_hub.domain.enums import OpportunityStatus
from acm_hub.pipeline.classify import classify_opportunity
from acm_hub.pipeline.normalize import normalize_opportunity
from acm_hub.prep.company import build_company_prep, company_slug, format_prep_section
from acm_hub.prep.resume import analyse_resume_against_listing, extract_pdf_text
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


class TestExtractPdf:
    def test_rejects_empty(self) -> None:
        try:
            extract_pdf_text(b"")
            raise AssertionError("expected ValueError")
        except ValueError as exc:
            assert "empty" in str(exc).lower()


class TestAnalyseResume:
    def test_reports_missing_themes(self) -> None:
        row = build_row(
            title="Machine Learning Intern",
            organization="Microsoft",
            tags=("machine learning", "python"),
        )
        report = analyse_resume_against_listing(
            "Built web apps with JavaScript and HTML at a campus club.",
            row,
        )
        assert report.themes_missing
        assert "python" in {t.lower() for t in report.themes_missing} or any(
            "machine" in t.lower() for t in report.themes_missing
        )

    def test_finds_themes_present_in_resume(self) -> None:
        row = build_row(
            title="Machine Learning Intern",
            tags=("machine learning",),
        )
        report = analyse_resume_against_listing(
            "Projects: machine learning classifiers in Python for SIGAI workshop.",
            row,
        )
        assert report.themes_found
        assert any("machine" in t.lower() or "sigai" in t.lower() for t in report.themes_found)


class TestCompanyPrep:
    def test_slug_and_leetcode_link(self) -> None:
        assert company_slug("Microsoft Corporation") == "microsoft"
        prep = build_company_prep(build_row(organization="Microsoft", title="SWE Intern"))
        assert prep.leetcode_company_url == "https://leetcode.com/company/microsoft/"
        assert any("leetcode" in link.url for link in prep.links)
        assert "invent" in prep.disclaimer.lower() or "community" in prep.disclaimer.lower()

    def test_labels_are_short_and_actionable(self) -> None:
        prep = build_company_prep(build_row(organization="Microsoft", title="SWE Intern"))
        labels = [link.label for link in prep.links]
        assert any(label.startswith("LeetCode") for label in labels)
        assert "OA & LeetCode threads" in labels
        assert "Interview stories" in labels
        assert all(len(label) <= 80 for label in labels)
        assert all(link.blurb for link in prep.links)
        assert {link.section for link in prep.links} >= {"Practice", "From candidates"}

    def test_embed_groups_practice_and_candidate_links(self) -> None:
        row = build_row(organization="Microsoft", title="Software Engineering Intern")
        embed = company_prep_embed(row)
        assert embed.title == "Prep for Microsoft"
        names = {str(field.name) for field in embed.fields}
        assert "Practice" in names
        assert "From candidates" in names
        assert "Links" not in names
        practice = next(str(field.value) for field in embed.fields if field.name == "Practice")
        assert "LeetCode" in practice
        assert "Community-tagged" in practice or "problems" in practice.lower()

    def test_prep_view_exposes_link_buttons(self) -> None:
        prep = build_company_prep(build_row(organization="Microsoft", title="SWE Intern"))
        view = build_company_prep_view(prep)
        assert len(view.children) == len(prep.links)
        assert all(getattr(child, "url", None) for child in view.children)

    def test_section_formatter_keeps_markdown_links(self) -> None:
        prep = build_company_prep(build_row(organization="Google", title="SWE Intern"))
        rendered = format_prep_section(prep.links[:1])
        assert "](https://" in rendered
        assert "**[" in rendered
