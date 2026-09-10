"""Normalization, canonical keys and content hashing.

These are the tests that protect against double-posting. If URL normalization or
canonical key generation regresses, ACM Hub re-publishes internships members
have already seen, so the coverage here is deliberately dense.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from acm_hub.domain.enums import RemoteStatus, SponsorshipStatus
from acm_hub.domain.opportunity import Eligibility
from acm_hub.pipeline.normalize import (
    build_canonical_key,
    build_content_hash,
    infer_remote_status,
    normalize_locations,
    normalize_opportunity,
    normalize_organization,
    normalize_title,
    normalize_url,
)
from tests.conftest import FROZEN_NOW, make_raw


class TestNormalizeUrl:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("HTTPS://Careers.Google.COM/jobs/123", "https://careers.google.com/jobs/123"),
            ("https://www.google.com/jobs/123", "https://google.com/jobs/123"),
            ("https://google.com/jobs/123/", "https://google.com/jobs/123"),
            ("https://google.com/", "https://google.com/"),
            ("https://google.com/jobs/123#apply-now", "https://google.com/jobs/123"),
            ("https://google.com:443/jobs/123", "https://google.com/jobs/123"),
            ("http://google.com:80/jobs", "http://google.com/jobs"),
            ("https://google.com:8443/jobs", "https://google.com:8443/jobs"),
            ("  https://google.com/jobs/123  ", "https://google.com/jobs/123"),
        ],
    )
    def test_canonicalizes_structure(self, raw: str, expected: str) -> None:
        assert normalize_url(raw) == expected

    def test_strips_tracking_parameters(self) -> None:
        url = "https://jobs.example.com/apply?utm_source=twitter&utm_campaign=spring&gh_src=abc"
        assert normalize_url(url) == "https://jobs.example.com/apply"

    def test_preserves_identifying_parameters(self) -> None:
        """Greenhouse and iCIMS put the job ID in the query string."""
        url = "https://boards.example.com/embed/job?gh_jid=4567&utm_source=email"
        assert normalize_url(url) == "https://boards.example.com/embed/job?gh_jid=4567"

    def test_orders_parameters_so_link_order_does_not_create_duplicates(self) -> None:
        first = normalize_url("https://x.com/apply?b=2&a=1")
        second = normalize_url("https://x.com/apply?a=1&b=2")
        assert first == second == "https://x.com/apply?a=1&b=2"

    def test_keeps_blank_valued_parameters(self) -> None:
        assert normalize_url("https://x.com/apply?icims=") == "https://x.com/apply?icims="

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_empty_input_returns_empty(self, raw: str) -> None:
        assert normalize_url(raw) == ""

    def test_unparseable_input_is_returned_trimmed_not_mangled(self) -> None:
        assert normalize_url("  not a url  ") == "not a url"


class TestNormalizeText:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Cloudflare, Inc.", "cloudflare"),
            ("Cloudflare", "cloudflare"),
            ("Acme Corp", "acme"),
            ("Foo Bar LLC", "foo bar"),
            ("Nestlé", "nestle"),
            ("  Google   ", "google"),
        ],
    )
    def test_organization_folding(self, raw: str, expected: str) -> None:
        assert normalize_organization(raw) == expected

    def test_organization_that_is_only_a_suffix_survives(self) -> None:
        """Stripping suffixes must not erase the entire name."""
        assert normalize_organization("Inc") == ""

    def test_title_folding(self) -> None:
        assert normalize_title("Software  Engineering Intern!") == "software engineering intern"

    def test_locations_are_deduplicated_and_ordered(self) -> None:
        assert normalize_locations(("Austin, TX", "austin tx", "NYC", "")) == (
            "Austin, TX",
            "NYC",
        )


class TestRemoteStatus:
    def test_reads_remote_from_location_text(self) -> None:
        assert infer_remote_status(("Remote in USA",)) is RemoteStatus.REMOTE

    def test_hybrid_wins_over_remote(self) -> None:
        assert infer_remote_status(("Hybrid - Austin, TX", "Remote in USA")) is RemoteStatus.HYBRID

    def test_silence_stays_unknown_rather_than_assuming_onsite(self) -> None:
        assert infer_remote_status(("Austin, TX",)) is RemoteStatus.UNKNOWN

    def test_explicit_collector_value_is_not_overridden(self) -> None:
        assert infer_remote_status(("Remote in USA",), RemoteStatus.ONSITE) is RemoteStatus.ONSITE


class TestCanonicalKey:
    def _key(self, **overrides: object) -> str:
        payload: dict[str, object] = {
            "application_url": "https://careers.google.com/jobs/123",
            "source_name": "simplify",
            "source_external_id": "abc",
            "organization": "Google",
            "title": "Software Engineering Intern",
            "locations": ("Mountain View, CA",),
        }
        payload.update(overrides)
        return build_canonical_key(**payload)  # type: ignore[arg-type]

    def test_is_stable_across_repeated_runs(self) -> None:
        assert self._key() == self._key()

    def test_uses_url_strategy_when_an_application_url_exists(self) -> None:
        assert self._key().startswith("url:")

    def test_tracking_parameters_do_not_change_identity(self) -> None:
        with_tracking = self._key(
            application_url="https://careers.google.com/jobs/123?utm_source=x"
        )
        assert with_tracking == self._key()

    def test_same_posting_from_two_sources_collides(self) -> None:
        """Cross-source dedup: the shared apply link is what makes them equal."""
        from_simplify = self._key(source_name="simplify", source_external_id="a")
        from_other = self._key(source_name="another_board", source_external_id="z")
        assert from_simplify == from_other

    def test_different_jobs_do_not_collide(self) -> None:
        assert self._key() != self._key(application_url="https://careers.google.com/jobs/124")

    def test_falls_back_to_external_id_without_a_url(self) -> None:
        key = self._key(application_url="")
        assert key.startswith("extid:")
        assert key == self._key(application_url="", title="Totally Different Title")

    def test_falls_back_to_fuzzy_identity_without_url_or_external_id(self) -> None:
        key = self._key(application_url="", source_external_id=None)
        assert key.startswith("fuzzy:")
        # Legal-suffix and casing differences must not split the identity.
        assert key == self._key(
            application_url="", source_external_id=None, organization="google inc."
        )

    def test_fuzzy_identity_separates_different_locations(self) -> None:
        base = self._key(application_url="", source_external_id=None)
        other = self._key(application_url="", source_external_id=None, locations=("Austin, TX",))
        assert base != other


class TestContentHash:
    def _hash(self, **overrides: object) -> str:
        payload: dict[str, object] = {
            "title": "Software Engineering Intern",
            "organization": "Cloudflare",
            "description": "Work on the edge network.",
            "locations": ("Austin, TX",),
            "remote_status": RemoteStatus.UNKNOWN,
            "application_url": "https://example.com/apply",
            "eligibility": Eligibility(),
            "expires_at": None,
        }
        payload.update(overrides)
        return build_content_hash(**payload)  # type: ignore[arg-type]

    def test_is_deterministic(self) -> None:
        assert self._hash() == self._hash()

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("title", "Senior Software Engineering Intern"),
            ("organization", "Fastly"),
            ("description", "Work on something else."),
            ("locations", ("Austin, TX", "Remote in USA")),
            ("remote_status", RemoteStatus.REMOTE),
            ("application_url", "https://example.com/apply-v2"),
            ("expires_at", datetime(2027, 1, 1, tzinfo=UTC)),
        ],
    )
    def test_member_visible_changes_are_detected(self, field: str, value: object) -> None:
        assert self._hash(**{field: value}) != self._hash()

    def test_newly_stated_sponsorship_is_detected(self) -> None:
        """An employer clarifying eligibility is a real change members should see."""
        changed = self._hash(eligibility=Eligibility(sponsorship=SponsorshipStatus.NO_SPONSORSHIP))
        assert changed != self._hash()

    def test_cosmetic_differences_do_not_count_as_changes(self) -> None:
        assert (
            self._hash(
                title="Software   Engineering Intern",
                application_url="https://example.com/apply?utm_source=x",
            )
            == self._hash()
        )


class TestNormalizeOpportunity:
    def test_populates_derived_fields(self) -> None:
        result = normalize_opportunity(make_raw(), discovered_at=FROZEN_NOW)
        assert result.canonical_key.startswith("url:")
        assert len(result.content_hash) == 64
        assert result.discovered_at == FROZEN_NOW

    def test_does_not_invent_eligibility(self) -> None:
        result = normalize_opportunity(make_raw(), discovered_at=FROZEN_NOW)
        assert result.eligibility.sponsorship is SponsorshipStatus.UNKNOWN
        assert result.eligibility.citizenship_requirement is None

    def test_blank_description_becomes_none_rather_than_empty_text(self) -> None:
        result = normalize_opportunity(make_raw(description="   "), discovered_at=FROZEN_NOW)
        assert result.description is None

    def test_repeated_normalization_is_idempotent(self) -> None:
        first = normalize_opportunity(make_raw(), discovered_at=FROZEN_NOW)
        second = normalize_opportunity(make_raw(), discovered_at=FROZEN_NOW)
        assert first.canonical_key == second.canonical_key
        assert first.content_hash == second.content_hash
