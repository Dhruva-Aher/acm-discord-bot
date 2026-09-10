"""The pipeline orchestrator.

Wires the stages together in order and records the result as a
``collector_runs`` row:

    COLLECT -> NORMALIZE -> VALIDATE -> DEDUPLICATE -> CLASSIFY -> STORE
    -> RECONCILE CLOSURES

Approval and publishing deliberately live outside this module. The pipeline's
only output is persisted rows and statistics, which is what lets it be executed
by the scheduler, by an admin command or by a test with no Discord present.
Where a source is trusted enough to skip officer review, the pipeline records
that by storing the row as ``APPROVED``; the publisher, not the pipeline, is
what talks to Discord.

Three policies are applied here, and all three are conservative by default.

**Trust.** What happens to a source's findings is read from
``sources.publication_policy``, never from the source's name. New sources are
``MANUAL_REVIEW``.

**Initial import.** A source's first successful run is stored as baseline: the
rows are real and searchable, but they are held back from the review queue,
because a source's existing catalogue is backlog rather than news. The marker
is ``sources.initialized_at``, so there is no date constant anywhere and the
behaviour survives restarts.

**Closure.** Absence from a feed only means "closed" if the feed promised to be
complete and actually looks complete. See :meth:`OpportunityPipeline._reconcile_closures`.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from acm_hub.collectors.base import CollectionOutcome, Collector, safe_collect
from acm_hub.config.sigs import ClassifierWeights
from acm_hub.db.repositories.collector_runs import CollectorRunRepository
from acm_hub.db.repositories.opportunities import OpportunityRepository
from acm_hub.db.repositories.sources import SourceRepository
from acm_hub.db.session import Database
from acm_hub.domain.enums import CollectorRunStatus, PublicationPolicy
from acm_hub.domain.opportunity import Opportunity as DomainOpportunity
from acm_hub.observability.logging import get_logger
from acm_hub.pipeline.classify import classify_opportunity
from acm_hub.pipeline.deduplicate import ChangeType, deduplicate_batch
from acm_hub.pipeline.normalize import normalize_opportunity
from acm_hub.pipeline.validate import validate_batch

logger = get_logger(__name__)


@dataclass(slots=True)
class ClosureGuard:
    """Limits on how much one run is allowed to conclude has closed.

    Both limits exist because the failure mode is asymmetric: missing a closure
    is a stale post that an officer can delete, while a false mass-closure
    wipes the members' channel. Tripping either limit skips the sweep and logs
    loudly - it never closes "just the safe ones", because a truncated feed
    gives no reason to trust any of its absences.
    """

    #: Reject a run whose record count collapsed relative to the last good run.
    #: A half-downloaded feed is the realistic way thousands of postings
    #: "disappear" at once.
    min_volume_ratio: float = 0.5
    #: Reject a sweep that would close more than this fraction of the source's
    #: currently-open rows, even if the feed size looked fine.
    max_close_ratio: float = 0.3


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
    closed: int = 0
    duration_seconds: float = 0.0
    error: str | None = None
    #: Set when ``publication_policy`` is ``DISABLED``; the source was not
    #: contacted at all.
    skipped: bool = False
    #: Whether this run was the source's initial import.
    baseline_import: bool = False
    #: Why the closure sweep did not run, when it did not run.
    closure_skipped_reason: str | None = None
    #: Opportunities this run closed, for the caller to reflect in Discord.
    closed_ids: list[UUID] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.error is None

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["closed_ids"] = [str(identifier) for identifier in self.closed_ids]
        return data


class OpportunityPipeline:
    """Runs collectors and persists what they find."""

    def __init__(
        self,
        database: Database,
        *,
        sig_threshold: float = 0.25,
        sig_max_matches: int = 3,
        weights: ClassifierWeights | None = None,
        closure_detection_enabled: bool = True,
        closure_guard: ClosureGuard | None = None,
    ) -> None:
        self._database = database
        self._sig_threshold = sig_threshold
        self._sig_max_matches = sig_max_matches
        self._weights = weights
        self._closure_detection_enabled = closure_detection_enabled
        self._closure_guard = closure_guard or ClosureGuard()

    async def run(self, collector: Collector) -> PipelineStats:
        """Execute one collector end to end.

        A collector failure is recorded and returned, never raised: the caller
        is usually the scheduler running several collectors, and one broken
        source must not stop the others.
        """
        started = time.monotonic()
        stats = PipelineStats(collector=collector.name)

        async with self._database.session() as session:
            sources = SourceRepository(session)
            await sources.register(collector.name, collector.display_name, collector.source_url)
            policy = await sources.policy(collector.name)
            stats.baseline_import = not await sources.is_initialized(collector.name)

        if policy is PublicationPolicy.DISABLED:
            stats.skipped = True
            stats.duration_seconds = time.monotonic() - started
            logger.info("pipeline.source_disabled", collector=collector.name)
            return stats

        async with self._database.session() as session:
            run = await CollectorRunRepository(session).start(collector.name)
            run_id = run.id

        logger.info(
            "pipeline.started",
            collector=collector.name,
            run_id=str(run_id),
            policy=policy.value,
            baseline_import=stats.baseline_import,
        )

        outcome = await safe_collect(collector)
        stats.fetched = len(outcome.records)

        if not outcome.succeeded:
            stats.error = outcome.error
            stats.duration_seconds = time.monotonic() - started
            await self._finalize(run_id, stats)
            return stats

        try:
            await self._process_records(collector, outcome, stats, policy=policy)
        except Exception as exc:
            stats.error = f"{type(exc).__name__}: {exc}"
            stats.duration_seconds = time.monotonic() - started
            logger.error(
                "pipeline.failed", collector=collector.name, error=stats.error, exc_info=True
            )
            await self._finalize(run_id, stats)
            raise

        stats.duration_seconds = time.monotonic() - started
        await self._finalize(
            run_id,
            stats,
            mark_source_success=collector.name,
            snapshot_complete=self._is_snapshot(collector),
            initialize_source=collector.name if stats.baseline_import else None,
        )

        logger.info("pipeline.finished", **stats.as_dict())
        return stats

    @staticmethod
    def _is_snapshot(collector: Collector) -> bool:
        return bool(getattr(collector, "produces_complete_snapshot", False))

    async def _process_records(
        self,
        collector: Collector,
        outcome: CollectionOutcome,
        stats: PipelineStats,
        *,
        policy: PublicationPolicy,
    ) -> None:
        """Normalize, validate, deduplicate, classify, store and reconcile."""
        discovered_at = datetime.now(tz=UTC)

        normalized = [
            normalize_opportunity(raw, discovered_at=discovered_at) for raw in outcome.records
        ]
        # Built before validation deliberately. A posting that the source still
        # lists but that we now fail to parse is a parsing problem, not a
        # closure, and must not be inferred away.
        observed_keys = {opportunity.canonical_key for opportunity in normalized}

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

        await self._store(classified, stats, policy=policy, discovered_at=discovered_at)
        await self._reconcile_closures(collector, observed_keys, stats)

    async def _store(
        self,
        classified: list[DomainOpportunity],
        stats: PipelineStats,
        *,
        policy: PublicationPolicy,
        discovered_at: datetime,
    ) -> None:
        # An initial import is never auto-published, whatever the policy says.
        # AUTO_PUBLISH means "we trust this source's new findings", not "post
        # its entire back catalogue to the members' channel at once".
        auto_approve = policy is PublicationPolicy.AUTO_PUBLISH and not stats.baseline_import

        async with self._database.session() as session:
            repository = OpportunityRepository(session)
            for opportunity in classified:
                row, change = await repository.upsert(
                    opportunity, now=discovered_at, is_baseline=stats.baseline_import
                )
                if change is ChangeType.NEW:
                    stats.new += 1
                    if auto_approve:
                        await repository.approve_without_review(row.id)
                elif change is ChangeType.CHANGED:
                    stats.updated += 1
                else:
                    stats.unchanged += 1

    async def _reconcile_closures(
        self, collector: Collector, observed_keys: set[str], stats: PipelineStats
    ) -> None:
        """Close opportunities the source no longer lists.

        Runs only when every one of these holds, and records why when it does
        not:

        * closure detection is enabled;
        * the collector declares its feed a complete snapshot of the open set;
        * this is not the source's initial import, where "previously known"
          is not a meaningful set;
        * the run returned a plausible number of records compared with the last
          successful run;
        * the resulting closure set is a plausible fraction of what is open.

        The row's publication records are kept. Members should still be able to
        see that ACM Hub posted something and that it later closed.
        """
        reason = self._closure_precondition_failure(collector, stats)
        if reason is not None:
            stats.closure_skipped_reason = reason
            logger.info("closure.skipped", collector=collector.name, reason=reason)
            return

        async with self._database.session() as session:
            previous = await CollectorRunRepository(session).last_successful(collector.name)
            open_rows = await OpportunityRepository(session).open_canonical_keys(collector.name)

        if previous is not None and previous.records_fetched > 0:
            floor = previous.records_fetched * self._closure_guard.min_volume_ratio
            if stats.fetched < floor:
                stats.closure_skipped_reason = (
                    f"run returned {stats.fetched} records against "
                    f"{previous.records_fetched} last time, below the "
                    f"{self._closure_guard.min_volume_ratio:.0%} floor"
                )
                logger.warning(
                    "closure.volume_guard_tripped",
                    collector=collector.name,
                    fetched=stats.fetched,
                    previous_fetched=previous.records_fetched,
                )
                return

        missing = [identifier for key, identifier in open_rows.items() if key not in observed_keys]
        if not missing:
            return

        if len(missing) > len(open_rows) * self._closure_guard.max_close_ratio:
            stats.closure_skipped_reason = (
                f"{len(missing)} of {len(open_rows)} open opportunities were absent, "
                f"above the {self._closure_guard.max_close_ratio:.0%} ceiling; "
                "an officer should confirm the source before closing them"
            )
            logger.error(
                "closure.mass_closure_blocked",
                collector=collector.name,
                candidates=len(missing),
                open_rows=len(open_rows),
            )
            return

        async with self._database.session() as session:
            closed = await OpportunityRepository(session).mark_closed(missing)
            stats.closed_ids = [row.id for row in closed]

        stats.closed = len(stats.closed_ids)
        logger.info("closure.applied", collector=collector.name, closed=stats.closed)

    def _closure_precondition_failure(
        self, collector: Collector, stats: PipelineStats
    ) -> str | None:
        if not self._closure_detection_enabled:
            return "closure detection is disabled"
        if not self._is_snapshot(collector):
            return "collector does not produce a complete snapshot"
        if stats.baseline_import:
            return "initial import"
        return None

    async def _finalize(
        self,
        run_id: UUID,
        stats: PipelineStats,
        *,
        mark_source_success: str | None = None,
        snapshot_complete: bool = False,
        initialize_source: str | None = None,
    ) -> None:
        """Write the run record. Never raises into the caller's error path."""
        try:
            async with self._database.session() as session:
                await CollectorRunRepository(session).finish(
                    run_id,
                    status=(
                        CollectorRunStatus.SUCCESS if stats.succeeded else CollectorRunStatus.FAILED
                    ),
                    duration_seconds=round(stats.duration_seconds, 3),
                    records_fetched=stats.fetched,
                    records_new=stats.new,
                    records_updated=stats.updated,
                    records_duplicate=stats.duplicates,
                    records_rejected=stats.rejected,
                    records_closed=stats.closed,
                    snapshot_complete=snapshot_complete and stats.succeeded,
                    error=stats.error,
                )
                sources = SourceRepository(session)
                if mark_source_success:
                    await sources.mark_success(mark_source_success)
                # Stamped only after the run is recorded as successful, so a
                # crashed initial import is retried as an initial import
                # instead of leaking its catalogue into the review queue.
                if initialize_source:
                    await sources.mark_initialized(initialize_source, run_id=run_id)
        except Exception as exc:
            logger.error(
                "pipeline.run_record_failed",
                collector=stats.collector,
                error=f"{type(exc).__name__}: {exc}",
                exc_info=True,
            )
