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
    """One registered collector, and when it last ran successfully."""

    __tablename__ = "sources"

    name: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    source_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class Opportunity(Base):
    """The canonical stored opportunity."""

    __tablename__ = "opportunities"
    __table_args__ = (
        sa.Index("ix_opportunities_status_discovered", "status", "discovered_at"),
        sa.Index("ix_opportunities_source_external", "source_name", "source_external_id"),
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

    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
