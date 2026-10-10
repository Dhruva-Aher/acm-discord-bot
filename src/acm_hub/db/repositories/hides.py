"""Per-member hidden opportunities."""

from __future__ import annotations

from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from acm_hub.db import models


class MemberHideRepository:
    """Hide a posting from one member's browse / digest without closing it."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def hide(
        self, discord_user_id: int, opportunity_id: UUID, *, reason: str | None = None
    ) -> bool:
        """Record a personal hide. Returns ``True`` when a new row was created."""
        bind = self._session.get_bind()
        dialect = bind.dialect.name if bind is not None else ""
        if dialect == "postgresql":
            stmt = (
                pg_insert(models.MemberHide)
                .values(
                    discord_user_id=discord_user_id,
                    opportunity_id=opportunity_id,
                    reason=reason,
                )
                .on_conflict_do_nothing(
                    index_elements=["discord_user_id", "opportunity_id"]
                )
            )
            result = await self._session.execute(stmt)
            await self._session.flush()
            return bool(result.rowcount)

        existing = await self._session.get(
            models.MemberHide, (discord_user_id, opportunity_id)
        )
        if existing is not None:
            return False
        self._session.add(
            models.MemberHide(
                discord_user_id=discord_user_id,
                opportunity_id=opportunity_id,
                reason=reason,
            )
        )
        await self._session.flush()
        return True

    async def hidden_ids(self, discord_user_id: int) -> set[UUID]:
        result = await self._session.execute(
            sa.select(models.MemberHide.opportunity_id).where(
                models.MemberHide.discord_user_id == discord_user_id
            )
        )
        return set(result.scalars().all())
