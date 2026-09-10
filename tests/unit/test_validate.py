"""Validation of normalized opportunities."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from acm_hub.domain.opportunity import Opportunity
from acm_hub.pipeline.normalize import normalize_opportunity
from acm_hub.pipeline.validate import validate_batch, validate_opportunity
from tests.conftest import FROZEN_NOW, make_raw


def _opportunity(**kwargs: Any) -> Opportunity:
    return normalize_opportunity(make_raw(**kwargs), discovered_at=FROZEN_NOW)


class TestValidateOpportunity:
    def test_a_well_formed_record_passes(self) -> None:
        assert validate_opportunity(_opportunity()).is_valid

    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "ftp://example.com/job",
            "data:text/html,hi",
        ],
    )
    def test_non_http_application_urls_are_rejected(self, url: str) -> None:
        """A member clicking Apply must never be handed a non-web scheme."""
        result = validate_opportunity(_opportunity(application_url=url))
        assert not result.is_valid
        assert "application_url" in result.reason

    def test_url_without_a_host_is_rejected(self) -> None:
        result = validate_opportunity(_opportunity(application_url="https:///jobs/1"))
        assert not result.is_valid

    def test_overlong_title_is_rejected(self) -> None:
        result = validate_opportunity(_opportunity(title="x" * 400))
        assert not result.is_valid
        assert "title" in result.reason

    def test_expiry_before_posting_is_rejected(self) -> None:
        result = validate_opportunity(
            _opportunity(
                posted_at=datetime(2026, 6, 1, tzinfo=UTC),
                expires_at=datetime(2026, 5, 1, tzinfo=UTC),
            )
        )
        assert not result.is_valid

    def test_failures_are_marked_permanent_so_they_are_not_retried_forever(self) -> None:
        result = validate_opportunity(_opportunity(application_url="ftp://example.com"))
        assert result.is_permanent

    def test_all_problems_are_reported_at_once(self) -> None:
        result = validate_opportunity(
            _opportunity(title="x" * 400, application_url="ftp://example.com")
        )
        assert len(result.errors) >= 2


class TestValidateBatch:
    def test_partitions_valid_and_invalid_records(self) -> None:
        batch = [
            _opportunity(application_url="https://example.com/good"),
            _opportunity(application_url="ftp://example.com/bad"),
        ]
        report = validate_batch(batch)
        assert len(report.valid) == 1
        assert report.rejected_count == 1

    def test_one_bad_record_does_not_discard_the_batch(self) -> None:
        batch = [_opportunity(application_url="ftp://bad")] + [
            _opportunity(application_url=f"https://example.com/{index}") for index in range(5)
        ]
        report = validate_batch(batch)
        assert len(report.valid) == 5
