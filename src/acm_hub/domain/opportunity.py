"""The canonical opportunity: one record, independent of source and of Discord."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from acm_hub.domain.enums import (
    OpportunityStatus,
    OpportunityType,
    RemoteStatus,
    SponsorshipStatus,
    VerificationStatus,
)


class Eligibility(BaseModel):
    """Eligibility constraints, recorded only where a source states them.

    Absence of a restriction is *not* evidence of eligibility. ``sponsorship``
    defaults to :attr:`SponsorshipStatus.UNKNOWN` and callers must render that
    as "Unknown" rather than treating the member as eligible.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sponsorship: SponsorshipStatus = SponsorshipStatus.UNKNOWN
    citizenship_requirement: str | None = None
    degree_requirements: tuple[str, ...] = ()


class SigMatch(BaseModel):
    """A Special Interest Group the classifier believes an opportunity suits."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sig: str
    score: float = Field(ge=0.0, le=1.0)
    matched_terms: tuple[str, ...] = ()


class Opportunity(BaseModel):
    """A normalized, deduplicated, classified opportunity.

    ``canonical_key`` identifies the opportunity across collection runs and
    across sources; ``content_hash`` covers only the member-visible fields so a
    changed hash means the posting itself changed, not that we re-collected it.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)

    source_name: str
    source_external_id: str | None = None
    source_url: str
    application_url: str

    title: str
    organization: str
    description: str | None = None

    opportunity_type: OpportunityType = OpportunityType.OTHER
    locations: tuple[str, ...] = ()
    remote_status: RemoteStatus = RemoteStatus.UNKNOWN

    tags: tuple[str, ...] = ()
    sig_matches: tuple[SigMatch, ...] = ()

    eligibility: Eligibility = Eligibility()

    posted_at: datetime | None = None
    discovered_at: datetime | None = None
    expires_at: datetime | None = None
    last_verified_at: datetime | None = None

    status: OpportunityStatus = OpportunityStatus.DISCOVERED
    verification_status: VerificationStatus = VerificationStatus.UNKNOWN

    content_hash: str
    canonical_key: str

    @property
    def sig_names(self) -> tuple[str, ...]:
        """SIG identifiers, highest scoring first."""
        return tuple(
            match.sig for match in sorted(self.sig_matches, key=lambda m: m.score, reverse=True)
        )
