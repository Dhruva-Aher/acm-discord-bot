"""Deduplication and change detection.

Two separate questions, answered separately:

*Within a batch* - one collection run can return the same posting more than
once (the same job listed under several locations, or two upstream contributors
adding it). :func:`deduplicate_batch` collapses those before they reach the
database.

*Against what we already stored* - :func:`classify_change` compares a freshly
collected record with the stored one to decide NEW / CHANGED / UNCHANGED.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from acm_hub.domain.opportunity import Opportunity


class ChangeType(StrEnum):
    """Whether a collected record is new, materially changed, or the same."""

    NEW = "new"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


@dataclass(slots=True)
class DeduplicationResult:
    """Outcome of collapsing duplicates inside a single batch."""

    unique: list[Opportunity] = field(default_factory=list)
    duplicates: list[Opportunity] = field(default_factory=list)

    @property
    def duplicate_count(self) -> int:
        return len(self.duplicates)


def merge_duplicates(primary: Opportunity, other: Opportunity) -> Opportunity:
    """Combine two records of the same opportunity, preferring richer content.

    Union of locations and tags; the longer description wins; the earlier
    ``posted_at`` wins. Eligibility is only taken from ``other`` when ``primary``
    is unknown, so a source that stated a restriction is never overwritten by a
    source that stayed silent.
    """
    merged_locations = tuple(dict.fromkeys((*primary.locations, *other.locations)))
    merged_tags = tuple(dict.fromkeys((*primary.tags, *other.tags)))

    description = primary.description
    if other.description and (description is None or len(other.description) > len(description)):
        description = other.description

    posted_at = primary.posted_at
    if other.posted_at and (posted_at is None or other.posted_at < posted_at):
        posted_at = other.posted_at

    eligibility = primary.eligibility
    if eligibility.sponsorship.value == "unknown" and other.eligibility.sponsorship.value != (
        "unknown"
    ):
        eligibility = other.eligibility

    return primary.model_copy(
        update={
            "locations": merged_locations,
            "tags": merged_tags,
            "description": description,
            "posted_at": posted_at,
            "eligibility": eligibility,
        }
    )


def deduplicate_batch(opportunities: list[Opportunity]) -> DeduplicationResult:
    """Collapse records that share a ``canonical_key`` within one batch.

    Order is preserved: the first occurrence of a key is the primary record and
    later occurrences are merged into it.
    """
    result = DeduplicationResult()
    index: dict[str, int] = {}

    for opportunity in opportunities:
        existing_position = index.get(opportunity.canonical_key)
        if existing_position is None:
            index[opportunity.canonical_key] = len(result.unique)
            result.unique.append(opportunity)
            continue
        result.duplicates.append(opportunity)
        result.unique[existing_position] = merge_duplicates(
            result.unique[existing_position], opportunity
        )

    return result


def classify_change(incoming: Opportunity, stored_content_hash: str | None) -> ChangeType:
    """Decide whether a collected record is new, changed, or unchanged.

    ``stored_content_hash`` is ``None`` when the canonical key has never been
    seen before.
    """
    if stored_content_hash is None:
        return ChangeType.NEW
    if stored_content_hash != incoming.content_hash:
        return ChangeType.CHANGED
    return ChangeType.UNCHANGED
