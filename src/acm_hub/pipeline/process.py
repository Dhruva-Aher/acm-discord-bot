"""The pipeline orchestrator.

Wires the stages together in order and records the result as a
``collector_runs`` row:

    COLLECT -> NORMALIZE -> VALIDATE -> DEDUPLICATE -> CLASSIFY -> STORE

Approval and publishing deliberately live outside this module. The pipeline's
only output is persisted rows and statistics, which is what lets it be executed
by the scheduler, by an admin command or by a test with no Discord present.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from acm_hub.collectors.base import Collector, safe_collect
from acm_hub.config.sigs import ClassifierWeights
from acm_hub.db.repositories.collector_runs import CollectorRunRepository
from acm_hub.db.repositories.opportunities import OpportunityRepository
from acm_hub.db.session import Database
from acm_hub.domain.enums import CollectorRunStatus
from acm_hub.observability.logging import get_logger
from acm_hub.pipeline.classify import classify_opportunity
from acm_hub.pipeline.deduplicate import ChangeType, deduplicate_batch
from acm_hub.pipeline.normalize import normalize_opportunity
from acm_hub.pipeline.validate import validate_batch

logger = get_logger(__name__)


@dataclass(slots=True)
class PipelineStats:
    """Counts describing one pipeline execution."""

    collector: str
    fetched: int = 0
    valid: int = 0
    rejected: int = 0
    duplicates: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    duration_seconds: float = 0.0
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class OpportunityPipeline:
    """Runs collectors and persists what they find."""

    def __init__(
        self,
        database: Database,
        *,
        sig_threshold: float = 0.25,
        sig_max_matches: int = 3,
        weights: ClassifierWeights | None = None,
    ) -> None:
        self._database = database
        self._sig_threshold = sig_threshold
        self._sig_max_matches = sig_max_matches
        self._weights = weights

    async def run(self, collector: Collector) -> PipelineStats:
        """Execute one collector end to end.

        A collector failure is recorded and returned, never raised: the caller
        is usually the scheduler running several collectors, and one broken
        source must not stop the others.
        """
        started = time.monotonic()
        stats = PipelineStats(collector=collector.name)

        async with self._database.session() as session:
            runs = CollectorRunRepository(session)
            await runs.upsert_source(collector.name, collector.display_name, collector.source_url)
            run = await runs.start(collector.name)
            run_id = run.id

        logger.info("pipeline.started", collector=collector.name, run_id=str(run_id))

        outcome = await safe_collect(collector)
        stats.fetched = len(outcome.records)

        if not outcome.succeeded:
            stats.error = outcome.error
            stats.duration_seconds = time.monotonic() - started
            await self._finalize(run_id, stats)
            return stats

        try:
            await self._process_records(outcome, stats)
        except Exception as exc:
            stats.error = f"{type(exc).__name__}: {exc}"
            stats.duration_seconds = time.monotonic() - started
            logger.error(
                "pipeline.failed", collector=collector.name, error=stats.error, exc_info=True
            )
            await self._finalize(run_id, stats)
            raise

        stats.duration_seconds = time.monotonic() - started
        await self._finalize(run_id, stats, mark_source_success=collector.name)

        logger.info("pipeline.finished", **stats.as_dict())
        return stats

    async def _process_records(self, outcome: object, stats: PipelineStats) -> None:
        """Normalize, validate, deduplicate, classify and store one batch."""
        records = getattr(outcome, "records", [])
        discovered_at = datetime.now(tz=UTC)

        normalized = [normalize_opportunity(raw, discovered_at=discovered_at) for raw in records]

        validation = validate_batch(normalized)
        stats.rejected = validation.rejected_count
        for rejected, result in validation.rejected[:20]:
            logger.warning(
                "pipeline.record_rejected",
                collector=stats.collector,
                title=rejected.title,
                organization=rejected.organization,
                reason=result.reason,
            )

        deduped = deduplicate_batch(validation.valid)
        stats.duplicates = deduped.duplicate_count
        stats.valid = len(deduped.unique)

        classified = [
            classify_opportunity(
                opportunity,
                weights=self._weights,
                threshold=self._sig_threshold,
                max_matches=self._sig_max_matches,
            )
            for opportunity in deduped.unique
        ]

        async with self._database.session() as session:
            repository = OpportunityRepository(session)
            for opportunity in classified:
                _, change = await repository.upsert(opportunity, now=discovered_at)
                if change is ChangeType.NEW:
                    stats.new += 1
                elif change is ChangeType.CHANGED:
                    stats.updated += 1
                else:
                    stats.unchanged += 1

    async def _finalize(
        self, run_id: object, stats: PipelineStats, *, mark_source_success: str | None = None
    ) -> None:
        """Write the run record. Never raises into the caller's error path."""
        try:
            async with self._database.session() as session:
                runs = CollectorRunRepository(session)
                await runs.finish(
                    run_id,  # type: ignore[arg-type]
                    status=(
                        CollectorRunStatus.SUCCESS if stats.succeeded else CollectorRunStatus.FAILED
                    ),
                    duration_seconds=round(stats.duration_seconds, 3),
                    records_fetched=stats.fetched,
                    records_new=stats.new,
                    records_updated=stats.updated,
                    records_duplicate=stats.duplicates,
                    records_rejected=stats.rejected,
                    error=stats.error,
                )
                if mark_source_success:
                    await runs.mark_source_success(mark_source_success)
        except Exception as exc:
            logger.error(
                "pipeline.run_record_failed",
                collector=stats.collector,
                error=f"{type(exc).__name__}: {exc}",
                exc_info=True,
            )
