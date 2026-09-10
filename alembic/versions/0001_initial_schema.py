"""Initial ACM Hub schema.

Creates the opportunity pipeline tables. Migrations are intentionally
self-contained: they declare their own types rather than importing the ORM
models, so a future model change cannot retroactively alter what this revision
does.

Revision ID: 0001
Revises:
Create Date: 2026-09-10

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: JSONB on PostgreSQL, portable JSON elsewhere.
JSONType = sa.JSON().with_variant(JSONB(), "postgresql")


def _enum(name: str, *values: str) -> sa.Enum:
    """Constrained VARCHAR rather than a native PostgreSQL enum.

    Adding a value later is then a CHECK constraint change, which is far easier
    to reason about in a migration than ``ALTER TYPE ... ADD VALUE``.
    """
    return sa.Enum(*values, name=name, native_enum=False, length=32)


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("name", sa.String(length=64), primary_key=True),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "opportunities",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("canonical_key", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("source_name", sa.String(length=64), nullable=False),
        sa.Column("source_external_id", sa.String(length=128), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("application_url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("organization", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "opportunity_type",
            _enum(
                "opportunity_type",
                "internship",
                "research",
                "fellowship",
                "hackathon",
                "scholarship",
                "conference",
                "program",
                "other",
            ),
            nullable=False,
        ),
        sa.Column("locations", JSONType, nullable=False),
        sa.Column(
            "remote_status",
            _enum("remote_status", "remote", "hybrid", "onsite", "unknown"),
            nullable=False,
        ),
        sa.Column("tags", JSONType, nullable=False),
        sa.Column("sig_matches", JSONType, nullable=False),
        sa.Column(
            "sponsorship",
            _enum(
                "sponsorship_status",
                "offers_sponsorship",
                "no_sponsorship",
                "citizenship_required",
                "unknown",
            ),
            nullable=False,
        ),
        sa.Column("citizenship_requirement", sa.Text(), nullable=True),
        sa.Column("degree_requirements", JSONType, nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            _enum(
                "opportunity_status",
                "discovered",
                "pending_review",
                "approved",
                "published",
                "rejected",
                "closed",
            ),
            nullable=False,
        ),
        sa.Column(
            "verification_status",
            _enum("verification_status", "verified", "source_claimed", "unknown"),
            nullable=False,
        ),
        sa.Column("review_channel_id", sa.BigInteger(), nullable=True),
        sa.Column("review_message_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("canonical_key", name="uq_opportunities_canonical_key"),
    )
    op.create_index("ix_opportunities_source_name", "opportunities", ["source_name"])
    op.create_index("ix_opportunities_status", "opportunities", ["status"])
    op.create_index(
        "ix_opportunities_status_discovered", "opportunities", ["status", "discovered_at"]
    )
    op.create_index(
        "ix_opportunities_source_external", "opportunities", ["source_name", "source_external_id"]
    )

    op.create_table(
        "publication_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "opportunity_id",
            sa.Uuid(),
            sa.ForeignKey("opportunities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # The database-level guarantee that one opportunity is published to one
        # channel at most once, whatever the application does.
        sa.UniqueConstraint(
            "opportunity_id", "channel_id", name="uq_publication_opportunity_channel"
        ),
    )

    op.create_table(
        "review_actions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "opportunity_id",
            sa.Uuid(),
            sa.ForeignKey("opportunities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reviewer_discord_id", sa.BigInteger(), nullable=False),
        sa.Column("decision", _enum("review_decision", "approved", "rejected"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_review_actions_opportunity", "review_actions", ["opportunity_id"])

    op.create_table(
        "subscriptions",
        sa.Column("discord_user_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("opportunity_types", JSONType, nullable=False),
        sa.Column("sigs", JSONType, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "collector_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("collector", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            _enum("collector_run_status", "running", "success", "failed"),
            nullable=False,
        ),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("records_fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_new", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_duplicate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_rejected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_collector_runs_collector_started", "collector_runs", ["collector", "started_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_collector_runs_collector_started", table_name="collector_runs")
    op.drop_table("collector_runs")
    op.drop_table("subscriptions")
    op.drop_index("ix_review_actions_opportunity", table_name="review_actions")
    op.drop_table("review_actions")
    op.drop_table("publication_records")
    op.drop_index("ix_opportunities_source_external", table_name="opportunities")
    op.drop_index("ix_opportunities_status_discovered", table_name="opportunities")
    op.drop_index("ix_opportunities_status", table_name="opportunities")
    op.drop_index("ix_opportunities_source_name", table_name="opportunities")
    op.drop_table("opportunities")
    op.drop_table("sources")
