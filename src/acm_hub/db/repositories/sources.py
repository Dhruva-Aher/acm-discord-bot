"""Source registration, trust policy and initial-import state.

The two questions this repository answers are the ones that decide what happens
to everything a collector finds:

* *May we publish it, and how?* - :attr:`models.Source.publication_policy`.
* *Is this the source's first sight of the world, or is it news?* -
  :attr:`models.Source.initialized_at`.

Both are columns rather than configuration constants so they survive restarts
and can be changed by an officer at runtime without a redeploy.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from acm_hub.db import models
from acm_hub.domain.enums import PublicationPolicy


class SourceRepository:
    """Reads and writes the ``sources`` table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, name: str) -> models.Source | None:
        return await self._session.get(models.Source, name)

    async def list_all(self) -> list[models.Source]:
        result = await self._session.execute(sa.select(models.Source).order_by(models.Source.name))
        return list(result.scalars().all())

    async def register(self, name: str, display_name: str, source_url: str) -> models.Source:
        """Ensure the collector has a row, refreshing its presentation fields.

        Deliberately does **not** touch ``publication_policy`` or
        ``initialized_at`` on an existing row: a redeploy must never silently
        reset a trust decision an officer made, nor re-run an initial import.
        """
        existing = await self.get(name)
        if existing is not None:
            existing.display_name = display_name
            existing.source_url = source_url
            await self._session.flush()
            return existing

        created = models.Source(
            name=name,
            display_name=display_name,
            source_url=source_url,
            publication_policy=PublicationPolicy.MANUAL_REVIEW,
        )
        self._session.add(created)
        await self._session.flush()
        return created

    async def policy(self, name: str) -> PublicationPolicy:
        """The source's trust policy, defaulting to the safe one.

        An unregistered source is treated as ``MANUAL_REVIEW`` rather than as
        an error, so a missing row can never cause auto-publishing.
        """
        result = await self._session.execute(
            sa.select(models.Source.publication_policy).where(models.Source.name == name)
        )
        found = result.scalar_one_or_none()
        return found if found is not None else PublicationPolicy.MANUAL_REVIEW

    async def set_policy(self, name: str, policy: PublicationPolicy) -> bool:
        """Change a source's trust policy.

        Returns:
            Whether a source by that name existed.
        """
        result = await self._session.execute(
            sa.update(models.Source)
            .where(models.Source.name == name)
            .values(publication_policy=policy)
        )
        return bool(cast("CursorResult[Any]", result).rowcount)

    async def is_initialized(self, name: str) -> bool:
        """Whether this source has already completed one successful run.

        ``False`` means the next successful run is the initial import, and what
        it finds is backlog rather than news.
        """
        result = await self._session.execute(
            sa.select(models.Source.initialized_at).where(models.Source.name == name)
        )
        row = result.one_or_none()
        return row is not None and row[0] is not None

    async def mark_initialized(
        self, name: str, *, run_id: UUID, now: datetime | None = None
    ) -> bool:
        """Stamp the source as initialized, once.

        Guarded on ``initialized_at IS NULL`` so two overlapping runs cannot
        both claim to be the initial import, and so a later run can never
        rewrite when initialization happened.

        Returns:
            Whether this call performed the stamping.
        """
        result = await self._session.execute(
            sa.update(models.Source)
            .where(models.Source.name == name, models.Source.initialized_at.is_(None))
            .values(initialized_at=now or datetime.now(tz=UTC), baseline_run_id=run_id)
        )
        return bool(cast("CursorResult[Any]", result).rowcount)

    async def clear_initialization(self, name: str) -> bool:
        """Officer override: treat the source as never initialized again.

        The next successful run then re-imports as baseline. Used to recover
        from an initial import that ran against a broken or partial feed.
        """
        result = await self._session.execute(
            sa.update(models.Source)
            .where(models.Source.name == name)
            .values(initialized_at=None, baseline_run_id=None)
        )
        return bool(cast("CursorResult[Any]", result).rowcount)

    async def mark_success(self, name: str, *, now: datetime | None = None) -> None:
        await self._session.execute(
            sa.update(models.Source)
            .where(models.Source.name == name)
            .values(last_success_at=now or datetime.now(tz=UTC))
        )
