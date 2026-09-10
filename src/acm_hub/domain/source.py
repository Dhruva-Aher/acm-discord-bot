"""The contract between a collector and the rest of the pipeline.

A :class:`RawOpportunity` is deliberately permissive: it is whatever a collector
could honestly extract from its upstream source, with no cleanup, no inference
and no defaults invented on the source's behalf. Turning it into a canonical
:class:`~acm_hub.domain.opportunity.Opportunity` is the normalizer's job.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from acm_hub.domain.enums import (
    OpportunityType,
    RemoteStatus,
    SponsorshipStatus,
    VerificationStatus,
)


class RawOpportunity(BaseModel):
    """One opportunity as reported by a single upstream source.

    Collectors populate only what the source actually states. Fields left at
    their defaults mean "the source did not say", which downstream code must
    keep representing as unknown rather than collapsing into a positive or
    negative claim.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_name: str = Field(min_length=1)
    source_external_id: str | None = None
    source_url: str = Field(min_length=1)
    application_url: str = Field(min_length=1)

    title: str = Field(min_length=1)
    organization: str = Field(min_length=1)
    description: str | None = None

    opportunity_type: OpportunityType = OpportunityType.OTHER
    locations: tuple[str, ...] = ()
    remote_status: RemoteStatus = RemoteStatus.UNKNOWN

    tags: tuple[str, ...] = ()

    sponsorship: SponsorshipStatus = SponsorshipStatus.UNKNOWN
    citizenship_requirement: str | None = None
    degree_requirements: tuple[str, ...] = ()

    posted_at: datetime | None = None
    expires_at: datetime | None = None
    is_active: bool | None = None

    verification_status: VerificationStatus = VerificationStatus.UNKNOWN

    #: Free-form source-specific fields preserved for auditing. Never rendered
    #: to Discord and never used to make eligibility claims.
    extra: dict[str, str] = Field(default_factory=dict)
