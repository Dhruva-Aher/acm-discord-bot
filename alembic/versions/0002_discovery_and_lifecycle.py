"""Source trust policy, baseline import state, closure state and SIG projection.

Adds the persistence needed for production hardening and member-facing search:

* ``sources.publication_policy`` replaces the never-read ``sources.enabled``
  flag, so the trust decision is one data-driven column instead of two
  overlapping booleans.
* ``sources.initialized_at`` / ``baseline_run_id`` record when a source first
  completed a run, which is what distinguishes pre-existing backlog from news.
* ``opportunities.is_baseline`` / ``closed_at`` carry the two new lifecycle
  facts.
* ``collector_runs.snapshot_complete`` / ``records_closed`` record whether a run
  is safe to reason about absence from, and what it closed.
* ``opportunity_sigs`` projects the SIG classifications into a queryable table.

Existing rows are backfilled so an already-running deployment keeps behaving as
it did: existing sources are marked initialized (they have already pushed their
findings through review, so their history must not be reclassified as baseline)
and existing opportunities are marked non-baseline.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-10

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PUBLICATION_POLICY = sa.Enum(
    "manual_review",
    "auto_publish",
    "disabled",
    name="publication_policy",
    native_enum=False,
    length=32,
)


def upgrade() -> None:
    with op.batch_alter_table("sources") as batch:
        batch.add_column(
            sa.Column(
                "publication_policy",
                PUBLICATION_POLICY,
                nullable=False,
                server_default="manual_review",
            )
        )
        batch.add_column(sa.Column("initialized_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("baseline_run_id", sa.Uuid(), nullable=True))
        batch.drop_column("enabled")

    # A source that already has a successful run has already been through the
    # review queue. Treating it as uninitialized would make the next run import
    # its entire catalogue as baseline and silently drain the queue.
    op.execute(
        sa.text(
            "UPDATE sources SET initialized_at = last_success_at WHERE last_success_at IS NOT NULL"
        )
    )

    with op.batch_alter_table("opportunities") as batch:
        batch.add_column(
            sa.Column("is_baseline", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))

    op.create_index(
        "ix_opportunities_status_type_discovered",
        "opportunities",
        ["status", "opportunity_type", "discovered_at"],
    )
    op.create_index(
        "ix_opportunities_source_status",
        "opportunities",
        ["source_name", "status"],
    )

    with op.batch_alter_table("collector_runs") as batch:
        batch.add_column(
            sa.Column("records_closed", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("snapshot_complete", sa.Boolean(), nullable=False, server_default=sa.false())
        )

    op.create_table(
        "opportunity_sigs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "opportunity_id",
            sa.Uuid(),
            sa.ForeignKey("opportunities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sig", sa.String(length=32), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.UniqueConstraint("opportunity_id", "sig", name="uq_opportunity_sig"),
    )
    op.create_index("ix_opportunity_sigs_sig", "opportunity_sigs", ["sig"])


def downgrade() -> None:
    op.drop_index("ix_opportunity_sigs_sig", table_name="opportunity_sigs")
    op.drop_table("opportunity_sigs")

    with op.batch_alter_table("collector_runs") as batch:
        batch.drop_column("snapshot_complete")
        batch.drop_column("records_closed")

    op.drop_index("ix_opportunities_source_status", table_name="opportunities")
    op.drop_index("ix_opportunities_status_type_discovered", table_name="opportunities")

    with op.batch_alter_table("opportunities") as batch:
        batch.drop_column("closed_at")
        batch.drop_column("is_baseline")

    with op.batch_alter_table("sources") as batch:
        batch.add_column(
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch.drop_column("baseline_run_id")
        batch.drop_column("initialized_at")
        batch.drop_column("publication_policy")
