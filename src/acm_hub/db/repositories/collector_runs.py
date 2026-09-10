"""Collector run history: what ran, what it found, and whether it worked."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from acm_hub.db import models
from acm_hub.domain.enums import CollectorRunStatus


class CollectorRunRepository:
    """Persists one row per collector execution, successful or not."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def start(self, collector: str) -> models.CollectorRun:
        run = models.CollectorRun(
            collector=collector,
            status=CollectorRunStatus.RUNNING,
            started_at=datetime.now(tz=UTC),
        )
        self._session.add(run)
        await self._session.flush()
        return run

    async def finish(
        self,
        run_id: UUID,
        *,
        status: CollectorRunStatus,
        duration_seconds: float,
        records_fetched: int = 0,
        records_new: int = 0,
        records_updated: int = 0,
        records_duplicate: int = 0,
        records_rejected: int = 0,
        records_closed: int = 0,
        snapshot_complete: bool = False,
        error: str | None = None,
    ) -> None:
        await self._session.execute(
            sa.update(models.CollectorRun)
            .where(models.CollectorRun.id == run_id)
            .values(
                status=status,
                finished_at=datetime.now(tz=UTC),
                duration_seconds=duration_seconds,
                records_fetched=records_fetched,
                records_new=records_new,
                records_updated=records_updated,
                records_duplicate=records_duplicate,
                records_rejected=records_rejected,
                records_closed=records_closed,
                snapshot_complete=snapshot_complete,
                error=error,
            )
        )

    async def latest(self, collector: str) -> models.CollectorRun | None:
        result = await self._session.execute(
            sa.select(models.CollectorRun)
            .where(models.CollectorRun.collector == collector)
            .order_by(models.CollectorRun.started_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def latest_per_collector(self) -> list[models.CollectorRun]:
        """Most recent run for each collector, for the health command."""
        newest = (
            sa.select(
                models.CollectorRun.collector,
                sa.func.max(models.CollectorRun.started_at).label("started_at"),
            )
            .group_by(models.CollectorRun.collector)
            .subquery()
        )
        result = await self._session.execute(
            sa.select(models.CollectorRun).join(
                newest,
                sa.and_(
                    models.CollectorRun.collector == newest.c.collector,
                    models.CollectorRun.started_at == newest.c.started_at,
                ),
            )
        )
        return list(result.scalars().all())

    async def last_successful(self, collector: str) -> models.CollectorRun | None:
        """The most recent run that completed successfully.

        The closure sweep compares against this to decide whether the current
        run's volume is plausible, so a truncated feed cannot be mistaken for
        thousands of postings closing at once.
        """
        result = await self._session.execute(
            sa.select(models.CollectorRun)
            .where(
                models.CollectorRun.collector == collector,
                models.CollectorRun.status == CollectorRunStatus.SUCCESS,
            )
            .order_by(models.CollectorRun.started_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
