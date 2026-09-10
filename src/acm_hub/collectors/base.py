"""The collector contract and the failure isolation around it."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from acm_hub.domain.source import RawOpportunity
from acm_hub.observability.logging import get_logger

logger = get_logger(__name__)


class CollectorError(RuntimeError):
    """Raised by a collector when its source is unusable this run.

    Carries the collector name so the failure is attributable in logs and in the
    ``collector_runs`` table.
    """

    def __init__(self, collector: str, message: str) -> None:
        super().__init__(f"{collector}: {message}")
        self.collector = collector
        self.message = message


class Collector(ABC):
    """One external source of opportunities.

    Implementations keep all source-specific parsing inside themselves. When the
    upstream format changes, exactly one class needs updating and the rest of
    the pipeline is unaffected.
    """

    #: Stable identifier persisted on every record this collector produces.
    #: Changing it orphans previously collected rows, so treat it as permanent.
    name: str

    #: Human-readable attribution shown to members in Discord embeds.
    display_name: str

    #: Where a human can go to inspect the source themselves.
    source_url: str

    #: Whether a successful :meth:`collect` returns *every* currently-open
    #: opportunity the source knows about, not a page or a recent slice.
    #:
    #: Only a complete snapshot licenses reasoning about absence: if a source
    #: promises the whole list and a posting is not in it, the posting is gone.
    #: A paginated or incremental collector must leave this ``False``, because
    #: for it "not returned" means nothing at all. Defaulting to ``False`` means
    #: a new collector cannot accidentally close anything.
    produces_complete_snapshot: bool = False

    @abstractmethod
    async def collect(self) -> list[RawOpportunity]:
        """Fetch and parse the source.

        Raises:
            CollectorError: when the source could not be fetched or parsed at
                all. Individual malformed records must be skipped and counted
                instead of aborting the whole run.
        """

    async def aclose(self) -> None:  # noqa: B027 - optional hook, not every collector holds resources
        """Release any resources held by the collector. Override when needed."""


@dataclass(slots=True)
class CollectionOutcome:
    """What happened when a collector ran, whether or not it succeeded."""

    collector: str
    records: list[RawOpportunity] = field(default_factory=list)
    error: str | None = None
    duration_seconds: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.error is None


async def safe_collect(collector: Collector) -> CollectionOutcome:
    """Run ``collector``, converting any failure into a reported outcome.

    This is the isolation boundary that keeps one broken source from stopping
    the others: the exception is logged with its traceback and returned as an
    error string rather than propagating into the scheduler. Nothing is silently
    swallowed - every failure is logged at ``error`` and recorded on the run.
    """
    started = time.monotonic()
    try:
        records = await collector.collect()
    except CollectorError as exc:
        duration = time.monotonic() - started
        logger.error(
            "collector.failed",
            collector=collector.name,
            error=exc.message,
            duration_seconds=round(duration, 3),
            exc_info=True,
        )
        return CollectionOutcome(
            collector=collector.name, error=exc.message, duration_seconds=duration
        )
    except Exception as exc:
        duration = time.monotonic() - started
        logger.error(
            "collector.crashed",
            collector=collector.name,
            error=f"{type(exc).__name__}: {exc}",
            duration_seconds=round(duration, 3),
            exc_info=True,
        )
        return CollectionOutcome(
            collector=collector.name,
            error=f"{type(exc).__name__}: {exc}",
            duration_seconds=duration,
        )

    duration = time.monotonic() - started
    logger.info(
        "collector.finished",
        collector=collector.name,
        records=len(records),
        duration_seconds=round(duration, 3),
    )
    return CollectionOutcome(collector=collector.name, records=records, duration_seconds=duration)
