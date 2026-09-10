"""Member subscription preferences."""

from __future__ import annotations

from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from acm_hub.db import models


class SubscriptionRepository:
    """Reads and writes a member's opportunity preferences."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, discord_user_id: int) -> models.Subscription | None:
        return await self._session.get(models.Subscription, discord_user_id)

    async def upsert(
        self,
        discord_user_id: int,
        *,
        opportunity_types: list[str] | None = None,
        sigs: list[str] | None = None,
    ) -> models.Subscription:
        """Create or update preferences.

        ``None`` means "leave this dimension alone", which lets the two select
        menus in ``/acm subscribe`` be submitted independently without one
        wiping the other.
        """
        existing = await self.get(discord_user_id)
        if existing is None:
            existing = models.Subscription(
                discord_user_id=discord_user_id,
                opportunity_types=opportunity_types or [],
                sigs=sigs or [],
            )
            self._session.add(existing)
            await self._session.flush()
            return existing

        if opportunity_types is not None:
            existing.opportunity_types = opportunity_types
        if sigs is not None:
            existing.sigs = sigs
        await self._session.flush()
        return existing

    async def delete(self, discord_user_id: int) -> bool:
        result = await self._session.execute(
            sa.delete(models.Subscription).where(
                models.Subscription.discord_user_id == discord_user_id
            )
        )
        return bool(cast("CursorResult[Any]", result).rowcount)

    async def count(self) -> int:
        result = await self._session.execute(
            sa.select(sa.func.count()).select_from(models.Subscription)
        )
        return int(result.scalar_one())
