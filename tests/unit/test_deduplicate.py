"""Batch deduplication, merging and change detection."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from acm_hub.domain.enums import SponsorshipStatus
from acm_hub.domain.opportunity import Opportunity
from acm_hub.pipeline.deduplicate import (
    ChangeType,
    classify_change,
    deduplicate_batch,
    merge_duplicates,
)
from acm_hub.pipeline.normalize import normalize_opportunity
from tests.conftest import FROZEN_NOW, make_raw


def _normalize(**kwargs: Any) -> Opportunity:
    """Normalize a test record, so every case starts from a canonical object."""
    return normalize_opportunity(make_raw(**kwargs), discovered_at=FROZEN_NOW)


class TestDeduplicateBatch:
    def test_keeps_distinct_opportunities(self) -> None:
        batch = [
            _normalize(application_url="https://example.com/a"),
            _normalize(application_url="https://example.com/b"),
        ]
        result = deduplicate_batch(batch)
        assert len(result.unique) == 2
        assert result.duplicate_count == 0

    def test_collapses_the_same_posting_listed_twice(self) -> None:
        batch = [
            _normalize(application_url="https://example.com/a"),
            _normalize(application_url="https://example.com/a?utm_source=newsletter"),
        ]
        result = deduplicate_batch(batch)
        assert len(result.unique) == 1
        assert result.duplicate_count == 1

    def test_merges_locations_from_duplicate_rows(self) -> None:
        """One job posted per location upstream should become one multi-location post."""
        batch = [
            _normalize(application_url="https://example.com/a", locations=("Austin, TX",)),
            _normalize(application_url="https://example.com/a", locations=("NYC",)),
        ]
        result = deduplicate_batch(batch)
        assert result.unique[0].locations == ("Austin, TX", "NYC")

    def test_preserves_input_order(self) -> None:
        batch = [
            _normalize(application_url="https://example.com/a"),
            _normalize(application_url="https://example.com/b"),
            _normalize(application_url="https://example.com/a"),
        ]
        result = deduplicate_batch(batch)
        assert [item.application_url for item in result.unique] == [
            "https://example.com/a",
            "https://example.com/b",
        ]

    def test_empty_batch(self) -> None:
        result = deduplicate_batch([])
        assert result.unique == []
        assert result.duplicate_count == 0


class TestMergeDuplicates:
    def test_longer_description_wins(self) -> None:
        short = _normalize(description="Short.")
        long = _normalize(description="A considerably longer description of the role.")
        merged = merge_duplicates(short, long)
        assert merged.description == "A considerably longer description of the role."

    def test_earliest_posted_at_wins(self) -> None:
        later = _normalize(posted_at=datetime(2026, 6, 1, tzinfo=UTC))
        earlier = _normalize(posted_at=datetime(2026, 5, 1, tzinfo=UTC))
        merged = merge_duplicates(later, earlier)
        assert merged.posted_at == datetime(2026, 5, 1, tzinfo=UTC)

    def test_a_stated_restriction_is_not_overwritten_by_silence(self) -> None:
        """A source that says "no sponsorship" must beat a source that says nothing."""
        restrictive = _normalize(sponsorship=SponsorshipStatus.NO_SPONSORSHIP)
        silent = _normalize()
        merged = merge_duplicates(restrictive, silent)
        assert merged.eligibility.sponsorship is SponsorshipStatus.NO_SPONSORSHIP

    def test_silence_is_filled_in_by_a_source_that_states_it(self) -> None:
        silent = _normalize()
        restrictive = _normalize(sponsorship=SponsorshipStatus.NO_SPONSORSHIP)
        merged = merge_duplicates(silent, restrictive)
        assert merged.eligibility.sponsorship is SponsorshipStatus.NO_SPONSORSHIP


class TestClassifyChange:
    def test_unseen_canonical_key_is_new(self) -> None:
        incoming = _normalize()
        assert classify_change(incoming, None) is ChangeType.NEW

    def test_identical_hash_is_unchanged(self) -> None:
        incoming = _normalize()
        assert classify_change(incoming, incoming.content_hash) is ChangeType.UNCHANGED

    def test_different_hash_is_changed(self) -> None:
        incoming = _normalize()
        assert classify_change(incoming, "stale-hash") is ChangeType.CHANGED
