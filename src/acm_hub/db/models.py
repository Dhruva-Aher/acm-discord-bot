"""SQLAlchemy models.

Portability note: the production target is PostgreSQL, but column types are
chosen so the same metadata also runs on SQLite, which is what the test suite
uses. ``JSONB`` is applied as a PostgreSQL variant of ``JSON``, and enums are
stored as constrained strings rather than native PostgreSQL enum types - adding
a new enum member then needs a constraint change, not a fragile ``ALTER TYPE``.

Correctness note: duplicate protection is enforced by the database, not by
application code. ``opportunities.canonical_key`` is unique, and
``publication_records`` is unique on ``(opportunity_id, channel_id)`` so a
double-approval race cannot post the same opportunity to the same channel twice.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from acm_hub.domain.enums import (
    CollectorRunStatus,
    OpportunityStatus,
    OpportunityType,
    PublicationPolicy,
    RemoteStatus,
    ReviewDecision,
    SponsorshipStatus,
    VerificationStatus,
)

#: JSON on SQLite, JSONB on PostgreSQL.
JSONType = sa.JSON().with_variant(JSONB(), "postgresql")


def _enum(enum_cls: type, name: str) -> sa.Enum:
    """Store a StrEnum as a checked VARCHAR, portable across backends."""
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        values_callable=lambda members: [member.value for member in members],
        length=32,
    )


class Base(DeclarativeBase):
    """Declarative base carrying the shared metadata."""

    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONType}


class Source(Base):
    """One registered collector: its trust policy and its import state.

    Two pieces of state live here rather than in configuration, because both
    must survive a restart and both are edited by officers at runtime:

    ``publication_policy``
        Whether this source's findings go to review, publish straight through,
        or are not collected at all. Keeping it in a column is what makes the
        trust decision data-driven instead of a branch on ``name``.

    ``initialized_at`` / ``baseline_run_id``
        When the source first completed a successful run, and which run that
        was. Everything that first run discovered is pre-existing backlog, not
        news, so it is imported as baseline instead of being pushed at the
        officers. A ``NULL`` here is the only thing that marks a source as
        never-initialized - there is no hardcoded cutover date anywhere.
    """

    __tablename__ = "sources"

    name: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    source_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    publication_policy: Mapped[PublicationPolicy] = mapped_column(
        _enum(PublicationPolicy, "publication_policy"),
        nullable=False,
        default=PublicationPolicy.MANUAL_REVIEW,
        server_default=PublicationPolicy.MANUAL_REVIEW.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    initialized_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    baseline_run_id: Mapped[UUID | None] = mapped_column(sa.Uuid, nullable=True)


class Opportunity(Base):
    """The canonical stored opportunity."""

    __tablename__ = "opportunities"
    __table_args__ = (
        sa.Index("ix_opportunities_status_discovered", "status", "discovered_at"),
        sa.Index("ix_opportunities_source_external", "source_name", "source_external_id"),
        # Serves the members' browse query, which is always filtered to
        # published rows and always ordered newest first.
        sa.Index(
            "ix_opportunities_status_type_discovered",
            "status",
            "opportunity_type",
            "discovered_at",
        ),
        # Serves the closure sweep, which scans one source's still-open rows.
        sa.Index("ix_opportunities_source_status", "source_name", "status"),
    )

    id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid4)

    canonical_key: Mapped[str] = mapped_column(sa.String(128), nullable=False, unique=True)
    content_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    source_name: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    source_external_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    source_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    application_url: Mapped[str] = mapped_column(sa.Text, nullable=False)

    title: Mapped[str] = mapped_column(sa.String(300), nullable=False)
    organization: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    opportunity_type: Mapped[OpportunityType] = mapped_column(
        _enum(OpportunityType, "opportunity_type"),
        nullable=False,
        default=OpportunityType.OTHER,
    )
    locations: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    remote_status: Mapped[RemoteStatus] = mapped_column(
        _enum(RemoteStatus, "remote_status"), nullable=False, default=RemoteStatus.UNKNOWN
    )

    tags: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    #: Serialized ``SigMatch`` objects: ``[{"sig", "score", "matched_terms"}]``.
    sig_matches: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONType, nullable=False, default=list
    )

    sponsorship: Mapped[SponsorshipStatus] = mapped_column(
        _enum(SponsorshipStatus, "sponsorship_status"),
        nullable=False,
        default=SponsorshipStatus.UNKNOWN,
    )
    citizenship_requirement: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    degree_requirements: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)

    posted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    status: Mapped[OpportunityStatus] = mapped_column(
        _enum(OpportunityStatus, "opportunity_status"),
        nullable=False,
        default=OpportunityStatus.DISCOVERED,
        index=True,
    )
    #: Imported by a source's very first successful run, i.e. backlog that
    #: existed before ACM Hub started watching. Baseline rows are real and
    #: searchable, they are simply held back from the review queue so turning a
    #: source on does not dump thousands of cards on the officers. An officer
    #: can release them deliberately.
    is_baseline: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    #: When a closure sweep observed that the source no longer lists this.
    closed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    verification_status: Mapped[VerificationStatus] = mapped_column(
        _enum(VerificationStatus, "verification_status"),
        nullable=False,
        default=VerificationStatus.UNKNOWN,
    )

    #: Where the officer review card for this opportunity lives, so the queue
    #: posting is itself idempotent across restarts.
    review_channel_id: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    review_message_id: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )

    publications: Mapped[list[PublicationRecord]] = relationship(
        back_populates="opportunity", cascade="all, delete-orphan", lazy="selectin"
    )
    reviews: Mapped[list[ReviewAction]] = relationship(
        back_populates="opportunity", cascade="all, delete-orphan", lazy="selectin"
    )
    sigs: Mapped[list[OpportunitySig]] = relationship(
        back_populates="opportunity", cascade="all, delete-orphan", lazy="selectin"
    )


class OpportunitySig(Base):
    """One classifier SIG match, projected out of ``sig_matches`` for querying.

    ``opportunities.sig_matches`` stays the display source of truth - it holds
    the matched terms an officer needs to judge a classification. This table is
    a queryable projection of the same data, written by the same upsert, so
    ``/acm opportunities sig:SIGAI`` is an indexed join instead of a JSON scan
    over every row. A test asserts the two never diverge.
    """

    __tablename__ = "opportunity_sigs"
    __table_args__ = (
        sa.UniqueConstraint("opportunity_id", "sig", name="uq_opportunity_sig"),
        sa.Index("ix_opportunity_sigs_sig", "sig"),
    )

    id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid4)
    opportunity_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False
    )
    sig: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    score: Mapped[float] = mapped_column(sa.Float, nullable=False)

    opportunity: Mapped[Opportunity] = relationship(back_populates="sigs")


class PublicationRecord(Base):
    """Proof that an opportunity was posted to a specific Discord channel.

    The unique constraint on ``(opportunity_id, channel_id)`` is the real
    guarantee against double-publishing: two officers pressing Approve
    simultaneously produce one insert and one ``IntegrityError``.
    """

    __tablename__ = "publication_records"
    __table_args__ = (
        sa.UniqueConstraint(
            "opportunity_id", "channel_id", name="uq_publication_opportunity_channel"
        ),
    )

    id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid4)
    opportunity_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    published_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    opportunity: Mapped[Opportunity] = relationship(back_populates="publications")


class ReviewAction(Base):
    """An officer's approve/reject decision, kept for auditing."""

    __tablename__ = "review_actions"
    __table_args__ = (sa.Index("ix_review_actions_opportunity", "opportunity_id"),)

    id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid4)
    opportunity_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False
    )
    reviewer_discord_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    decision: Mapped[ReviewDecision] = mapped_column(
        _enum(ReviewDecision, "review_decision"), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    opportunity: Mapped[Opportunity] = relationship(back_populates="reviews")


class Subscription(Base):
    """A member's opportunity preferences, keyed by Discord user ID."""

    __tablename__ = "subscriptions"

    discord_user_id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    opportunity_types: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    sigs: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )


class CollectorRun(Base):
    """One execution of one collector, with its counts and outcome."""

    __tablename__ = "collector_runs"
    __table_args__ = (sa.Index("ix_collector_runs_collector_started", "collector", "started_at"),)

    id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid4)
    collector: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    status: Mapped[CollectorRunStatus] = mapped_column(
        _enum(CollectorRunStatus, "collector_run_status"),
        nullable=False,
        default=CollectorRunStatus.RUNNING,
    )
    started_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(sa.Float, nullable=True)

    records_fetched: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    records_new: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    records_updated: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    records_duplicate: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    records_rejected: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    records_closed: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default="0"
    )

    #: Whether this run observed the source's complete set of open listings.
    #: Only a complete snapshot may be used to infer that anything it omits has
    #: closed; a failed, partial or incremental run leaves this ``False`` and
    #: the closure sweep is skipped entirely.
    snapshot_complete: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )

    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
