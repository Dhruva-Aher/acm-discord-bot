"""Background collection jobs.

Overlap protection is the important detail. A collection cycle over a large
upstream feed can take longer than its own interval, and two concurrent runs of
the same collector would race on the ``canonical_key`` unique constraint. Jobs
are therefore registered with ``max_instances=1`` and ``coalesce=True``, so a
still-running job is skipped rather than stacked, and a backlog of missed fires
collapses into one.
"""

from __future__ import annotations

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from acm_hub.collectors.base import Collector
from acm_hub.observability.logging import get_logger
from acm_hub.pipeline.process import OpportunityPipeline, PipelineStats
from acm_hub.publishing.discord import ChannelUnavailableError, OpportunityPublisher

logger = get_logger(__name__)

INTERNSHIP_JOB_ID = "collect_internships"


class CollectionScheduler:
    """Runs collectors on a cadence and queues what they find for review."""

    def __init__(
        self,
        *,
        pipeline: OpportunityPipeline,
        publisher: OpportunityPublisher,
        collectors: list[Collector],
        review_batch_limit: int = 10,
        scheduler: AsyncIOScheduler | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._publisher = publisher
        self._collectors = collectors
        self._review_batch_limit = review_batch_limit
        self._scheduler = scheduler or AsyncIOScheduler(timezone="UTC")
        self._manual_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)

    def start(self, *, internship_interval_minutes: int) -> None:
        """Register jobs and start the scheduler.

        The first run is not scheduled immediately on boot: it waits one full
        interval so a crash-loop cannot hammer the upstream source.
        """
        self._scheduler.add_job(
            self.run_all_collectors,
            trigger=IntervalTrigger(minutes=internship_interval_minutes),
            id=INTERNSHIP_JOB_ID,
            name="Collect internships and queue them for officer review",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=600,
            replace_existing=True,
        )
        self._scheduler.start()
        logger.info(
            "scheduler.started",
            internship_interval_minutes=internship_interval_minutes,
            collectors=[collector.name for collector in self._collectors],
        )

    async def run_all_collectors(self) -> list[PipelineStats]:
        """Run every collector, then push newly discovered records to review.

        Serialized behind a lock so a manual ``/acm-admin sync`` cannot overlap
        with a scheduled cycle. Each collector is independent: a failure is
        recorded and the loop continues.
        """
        async with self._manual_lock:
            results: list[PipelineStats] = []
            for collector in self._collectors:
                try:
                    stats = await self._pipeline.run(collector)
                except Exception as exc:
                    logger.error(
                        "scheduler.collector_error",
                        collector=collector.name,
                        error=f"{type(exc).__name__}: {exc}",
                        exc_info=True,
                    )
                    results.append(
                        PipelineStats(collector=collector.name, error=f"{type(exc).__name__}")
                    )
                    continue
                results.append(stats)

            await self._queue_reviews()
            return results

    async def _queue_reviews(self) -> None:
        """Post review cards, tolerating a misconfigured or unreachable channel."""
        try:
            await self._publisher.queue_pending_reviews(self._review_batch_limit)
        except ChannelUnavailableError as exc:
            logger.error("scheduler.review_channel_unavailable", error=str(exc))

    def shutdown(self) -> None:
        """Stop the scheduler without waiting for a running job to finish."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("scheduler.stopped")
