"""Enumerations shared by the domain model, the pipeline and persistence.

Every enum is a ``str`` enum so values round-trip through the database and JSON
fixtures unchanged. Persisted values are the lowercase member values; renaming
one requires a migration.
"""

from __future__ import annotations

from enum import StrEnum


class OpportunityType(StrEnum):
    """What kind of thing the member would be applying to."""

    INTERNSHIP = "internship"
    RESEARCH = "research"
    FELLOWSHIP = "fellowship"
    HACKATHON = "hackathon"
    SCHOLARSHIP = "scholarship"
    CONFERENCE = "conference"
    PROGRAM = "program"
    OTHER = "other"


class RemoteStatus(StrEnum):
    """Whether the role can be performed remotely.

    ``UNKNOWN`` is the default and is never inferred away: a source that does
    not say anything about remote work leaves this as ``UNKNOWN``.
    """

    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class SponsorshipStatus(StrEnum):
    """Visa sponsorship posture, only ever set from an explicit source claim."""

    OFFERS_SPONSORSHIP = "offers_sponsorship"
    NO_SPONSORSHIP = "no_sponsorship"
    CITIZENSHIP_REQUIRED = "citizenship_required"
    UNKNOWN = "unknown"


class OpportunityStatus(StrEnum):
    """Lifecycle of an opportunity inside ACM Hub."""

    DISCOVERED = "discovered"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    PUBLISHED = "published"
    REJECTED = "rejected"
    CLOSED = "closed"


class VerificationStatus(StrEnum):
    """How much ACM Hub trusts that this opportunity is real and open.

    ``SOURCE_CLAIMED`` means an upstream aggregator asserted it; ACM Hub did not
    independently confirm it against the employer.
    """

    VERIFIED = "verified"
    SOURCE_CLAIMED = "source_claimed"
    UNKNOWN = "unknown"


class ReviewDecision(StrEnum):
    """Officer decision recorded against an opportunity."""

    APPROVED = "approved"
    REJECTED = "rejected"


class CollectorRunStatus(StrEnum):
    """Outcome of a single collector execution."""

    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class PublicationPolicy(StrEnum):
    """What ACM Hub is allowed to do with what a source produces.

    Stored per source in the ``sources`` table so the decision is data, not a
    branch on the source's name. New sources default to
    :attr:`MANUAL_REVIEW`; nothing promotes a source automatically.
    """

    #: Everything goes to the officer review queue first. The safe default.
    MANUAL_REVIEW = "manual_review"
    #: Newly discovered opportunities are published without officer review.
    #: Only an officer can turn this on, per source, at runtime.
    AUTO_PUBLISH = "auto_publish"
    #: The source is not collected at all.
    DISABLED = "disabled"


#: Statuses that mean "an officer already decided about this".
TERMINAL_REVIEW_STATUSES: frozenset[OpportunityStatus] = frozenset(
    {
        OpportunityStatus.APPROVED,
        OpportunityStatus.PUBLISHED,
        OpportunityStatus.REJECTED,
    }
)

#: Statuses a closure sweep may move an opportunity out of. A rejected
#: opportunity stays rejected: an officer's decision outranks upstream churn.
CLOSEABLE_STATUSES: frozenset[OpportunityStatus] = frozenset(
    {
        OpportunityStatus.DISCOVERED,
        OpportunityStatus.PENDING_REVIEW,
        OpportunityStatus.APPROVED,
        OpportunityStatus.PUBLISHED,
    }
)
