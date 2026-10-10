"""Member hide list and default auto-publish.

Members report dead application links instead of officers approving every
posting up front. Sources default to auto-publish; existing manual_review
sources are flipped so deployments pick up the new default without a
manual ``/acm-admin source policy`` pass.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-10

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "member_hides",
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("opportunity_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("discord_user_id", "opportunity_id"),
    )
    op.create_index(
        "ix_member_hides_opportunity_id", "member_hides", ["opportunity_id"], unique=False
    )

    op.execute(
        sa.text(
            "UPDATE sources SET publication_policy = 'auto_publish' "
            "WHERE publication_policy = 'manual_review'"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_member_hides_opportunity_id", table_name="member_hides")
    op.drop_table("member_hides")
