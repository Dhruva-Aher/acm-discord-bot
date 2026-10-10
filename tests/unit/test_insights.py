"""Stand-out insights: freshness, focus tags, and research search links."""

from __future__ import annotations

from datetime import timedelta

from acm_hub.bot.insights import build_insights, focus_tags_for, research_links_for
from acm_hub.bot.member_actions import build_member_action_view
from acm_hub.db import models
from acm_hub.db.repositories.opportunities import sig_matches_to_json
from acm_hub.domain.enums import OpportunityStatus
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
        discovered_at=kwargs.get("discovered_at", FROZEN_NOW),  # type: ignore[arg-type]
        status=OpportunityStatus.PUBLISHED,
        verification_status=opportunity.verification_status,
    )


class TestFocusTags:
    def test_includes_sig_and_source_tags_not_github_plumbing(self) -> None:
        row = build_row(
            title="Machine Learning Intern",
            tags=("machine learning", "github:SimplifyJobs/Summer2027-Internships", "Summer 2027"),
        )
        tags = focus_tags_for(row)
        assert "SIGAI" in tags or any("machine" in t.lower() for t in tags)
        assert not any(t.startswith("github:") for t in tags)
        assert "Summer 2027" not in tags


class TestResearchLinks:
    def test_builds_search_urls_not_named_people(self) -> None:
        row = build_row(organization="Microsoft", title="Research Intern - BCI")
        links = research_links_for(row)
        labels = [link.label for link in links]
        assert any("Reddit" in label for label in labels)
        assert any("Recruiter" in label for label in labels)
        assert any("ASU" in label for label in labels)
        assert all("Microsoft" in link.url for link in links)
        assert all("linkedin.com" in link.url or "reddit.com" in link.url for link in links)


class TestFreshness:
    def test_same_day_is_called_out(self) -> None:
        insights = build_insights(build_row(), now=FROZEN_NOW)
        assert insights.freshness is not None
        assert "today" in insights.freshness.lower()

    def test_older_listings_have_no_freshness_banner(self) -> None:
        older = FROZEN_NOW - timedelta(days=3)
        row = build_row()
        row.discovered_at = older
        insights = build_insights(row, now=FROZEN_NOW)
        assert insights.freshness is None


class TestMemberActionView:
    def test_apply_button_points_at_application_url(self) -> None:
        row = build_row(application_url="https://example.test/apply/42")
        view = build_member_action_view(row)
        labels = []
        for item in view.children:
            label = getattr(item, "label", None)
            if label is None and hasattr(item, "item"):
                label = getattr(item.item, "label", None)
            labels.append(label)
        assert "Apply" in labels
        assert "Resume check" in labels
        assert "Company prep" in labels
        assert "Report link" in labels
        apply = next(
            item
            for item in view.children
            if getattr(item, "label", None) == "Apply"
            or getattr(getattr(item, "item", None), "label", None) == "Apply"
        )
        assert apply.url == "https://example.test/apply/42"
