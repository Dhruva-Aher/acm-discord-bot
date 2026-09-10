"""Opportunity persistence, review transitions and publication bookkeeping."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from acm_hub.db import models
from acm_hub.domain.enums import (
    CLOSEABLE_STATUSES,
    TERMINAL_REVIEW_STATUSES,
    OpportunityStatus,
    OpportunityType,
    RemoteStatus,
    ReviewDecision,
)
from acm_hub.domain.opportunity import Opportunity as DomainOpportunity
from acm_hub.domain.opportunity import SigMatch
from acm_hub.observability.logging import get_logger
from acm_hub.pipeline.deduplicate import ChangeType

logger = get_logger(__name__)


def sig_matches_to_json(matches: tuple[SigMatch, ...]) -> list[dict[str, object]]:
    """Serialize SIG matches for the ``sig_matches`` JSON column."""
    return [
        {"sig": match.sig, "score": match.score, "matched_terms": list(match.matched_terms)}
        for match in matches
    ]


def sig_matches_from_json(payload: list[dict[str, Any]] | None) -> tuple[SigMatch, ...]:
    """Rebuild SIG matches from stored JSON, skipping rows we cannot read.

    Tolerant on purpose: a schema change should degrade the SIG badges on an old
    row, not make the whole opportunity unreadable.
    """
    if not payload:
        return ()
    matches: list[SigMatch] = []
    for entry in payload:
        try:
            matches.append(
                SigMatch(
                    sig=str(entry["sig"]),
                    score=float(entry["score"]),
                    matched_terms=tuple(str(term) for term in entry.get("matched_terms", [])),
                )
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("opportunity.sig_match_unreadable", entry=str(entry))
    return tuple(matches)


@dataclass(frozen=True, slots=True)
class OpportunityFilters:
    """A set of narrowing criteria, all optional and all combined with AND.

    Exists so the review queue and the member-facing browse command share one
    tested translation into SQL instead of each growing its own ``if`` ladder.
    Every field defaults to "no constraint", so an empty instance matches
    everything and adding a criterion is purely additive.
    """

    opportunity_types: tuple[OpportunityType, ...] = ()
    sigs: tuple[str, ...] = ()
    organization: str | None = None
    remote_only: bool = False
    query: str | None = None

    @property
    def is_empty(self) -> bool:
        return not (
            self.opportunity_types
            or self.sigs
            or self.organization
            or self.remote_only
            or self.query
        )


def _escape_like(value: str) -> str:
    r"""Neutralize ``LIKE`` wildcards in user input.

    Without this, a member searching for ``100%`` would match every row, and a
    ``_`` would silently become "any character". Paired with
    ``escape="\\"`` at the call site.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def apply_filters(statement: Select[Any], filters: OpportunityFilters) -> Select[Any]:
    """Add ``filters`` to ``statement`` as SQL ``WHERE`` clauses.

    Filtering happens in the database, not in Python: a member browsing
    ``sig:SIGAI`` must not cause every published opportunity to be loaded into
    the bot process. SIG membership uses a subquery against the indexed
    ``opportunity_sigs`` projection rather than a join, so combining it with
    other criteria cannot duplicate rows.
    """
    if filters.opportunity_types:
        statement = statement.where(
            models.Opportunity.opportunity_type.in_(list(filters.opportunity_types))
        )

    if filters.sigs:
        statement = statement.where(
            models.Opportunity.id.in_(
                sa.select(models.OpportunitySig.opportunity_id).where(
                    models.OpportunitySig.sig.in_(list(filters.sigs))
                )
            )
        )

    if filters.organization:
        statement = statement.where(
            models.Opportunity.organization.ilike(
                f"%{_escape_like(filters.organization)}%", escape="\\"
            )
        )

    if filters.remote_only:
        # UNKNOWN is excluded on purpose. A posting that says nothing about
        # remote work is not evidence that it is remote.
        statement = statement.where(
            models.Opportunity.remote_status.in_([RemoteStatus.REMOTE, RemoteStatus.HYBRID])
        )

    if filters.query:
        pattern = f"%{_escape_like(filters.query)}%"
        statement = statement.where(
            sa.or_(
                models.Opportunity.title.ilike(pattern, escape="\\"),
                models.Opportunity.organization.ilike(pattern, escape="\\"),
            )
        )

    return statement


class OpportunityRepository:
    """All reads and writes for opportunities and their review/publish state."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, opportunity_id: UUID) -> models.Opportunity | None:
        return await self._session.get(models.Opportunity, opportunity_id)

    async def get_by_canonical_key(self, canonical_key: str) -> models.Opportunity | None:
        result = await self._session.execute(
            sa.select(models.Opportunity).where(models.Opportunity.canonical_key == canonical_key)
        )
        return result.scalar_one_or_none()

    async def content_hashes_for(self, canonical_keys: list[str]) -> dict[str, str]:
        """Bulk-load stored hashes so change detection needs one query per batch."""
        if not canonical_keys:
            return {}
        result = await self._session.execute(
            sa.select(models.Opportunity.canonical_key, models.Opportunity.content_hash).where(
                models.Opportunity.canonical_key.in_(canonical_keys)
            )
        )
        return dict(result.all())  # type: ignore[arg-type]

    @staticmethod
    def _sig_rows(opportunity: DomainOpportunity) -> list[models.OpportunitySig]:
        """Build the queryable projection of a classification result.

        Kept in one place so the JSON column and the association table can only
        ever be written together.
        """
        return [
            models.OpportunitySig(sig=match.sig, score=match.score)
            for match in opportunity.sig_matches
        ]

    async def _replace_sigs(self, row: models.Opportunity, opportunity: DomainOpportunity) -> None:
        """Swap a stored row's SIG projection for a freshly classified one.

        The clear is flushed before the new rows are attached. Assigning the
        collection in one step lets SQLAlchemy emit the inserts before the
        orphan deletes, which trips the ``(opportunity_id, sig)`` unique
        constraint whenever a re-classification keeps any SIG it already had -
        the common case.
        """
        if not row.sigs and not opportunity.sig_matches:
            return
        row.sigs.clear()
        await self._session.flush()
        row.sigs.extend(self._sig_rows(opportunity))

    async def upsert(
        self,
        opportunity: DomainOpportunity,
        *,
        now: datetime | None = None,
        is_baseline: bool = False,
    ) -> tuple[models.Opportunity, ChangeType]:
        """Insert a new opportunity or refresh an existing one.

        A rediscovered opportunity keeps whatever review state it already has.
        In particular a rejected opportunity is refreshed but stays ``REJECTED``,
        which is what stops the next polling cycle from putting an
        already-declined listing back in front of the officers.

        A rediscovered opportunity that a closure sweep had marked ``CLOSED``
        is reopened: the source listing it again is direct evidence that the
        earlier inference from absence was wrong.

        Args:
            is_baseline: Mark a newly inserted row as pre-existing backlog,
                which keeps it out of the review queue. Only ever applied on
                insert - an already-known opportunity is never retroactively
                demoted to baseline.
        """
        timestamp = now or datetime.now(tz=UTC)
        existing = await self.get_by_canonical_key(opportunity.canonical_key)

        if existing is None:
            row = models.Opportunity(
                id=opportunity.id,
                canonical_key=opportunity.canonical_key,
                content_hash=opportunity.content_hash,
                source_name=opportunity.source_name,
                source_external_id=opportunity.source_external_id,
                source_url=opportunity.source_url,
                application_url=opportunity.application_url,
                title=opportunity.title,
                organization=opportunity.organization,
                description=opportunity.description,
                opportunity_type=opportunity.opportunity_type,
                locations=list(opportunity.locations),
                remote_status=opportunity.remote_status,
                tags=list(opportunity.tags),
                sig_matches=sig_matches_to_json(opportunity.sig_matches),
                sponsorship=opportunity.eligibility.sponsorship,
                citizenship_requirement=opportunity.eligibility.citizenship_requirement,
                degree_requirements=list(opportunity.eligibility.degree_requirements),
                posted_at=opportunity.posted_at,
                discovered_at=opportunity.discovered_at or timestamp,
                expires_at=opportunity.expires_at,
                last_verified_at=timestamp,
                status=OpportunityStatus.DISCOVERED,
                verification_status=opportunity.verification_status,
                is_baseline=is_baseline,
                sigs=self._sig_rows(opportunity),
            )
            self._session.add(row)
            await self._session.flush()
            return row, ChangeType.NEW

        existing.last_verified_at = timestamp
        reopened = existing.status is OpportunityStatus.CLOSED
        if reopened:
            existing.status = OpportunityStatus.DISCOVERED
            existing.closed_at = None
            existing.review_message_id = None
            existing.review_channel_id = None

        if existing.content_hash == opportunity.content_hash:
            return existing, ChangeType.CHANGED if reopened else ChangeType.UNCHANGED

        existing.content_hash = opportunity.content_hash
        existing.title = opportunity.title
        existing.organization = opportunity.organization
        existing.description = opportunity.description
        existing.application_url = opportunity.application_url
        existing.source_url = opportunity.source_url
        existing.locations = list(opportunity.locations)
        existing.remote_status = opportunity.remote_status
        existing.tags = list(opportunity.tags)
        existing.sig_matches = sig_matches_to_json(opportunity.sig_matches)
        await self._replace_sigs(existing, opportunity)
        existing.sponsorship = opportunity.eligibility.sponsorship
        existing.citizenship_requirement = opportunity.eligibility.citizenship_requirement
        existing.degree_requirements = list(opportunity.eligibility.degree_requirements)
        existing.expires_at = opportunity.expires_at
        existing.verification_status = opportunity.verification_status
        await self._session.flush()
        return existing, ChangeType.CHANGED

    async def list_awaiting_queue(self, limit: int) -> list[models.Opportunity]:
        """Newly discovered opportunities that have not been sent for review yet.

        Baseline rows are excluded. They are the backlog a source already had
        when ACM Hub first saw it; posting a review card for each one would
        mean thousands of Discord messages the first time a source is enabled.
        """
        result = await self._session.execute(
            sa.select(models.Opportunity)
            .where(
                models.Opportunity.status == OpportunityStatus.DISCOVERED,
                models.Opportunity.review_message_id.is_(None),
                models.Opportunity.is_baseline.is_(False),
            )
            .order_by(models.Opportunity.discovered_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_pending_review(
        self,
        limit: int = 25,
        *,
        offset: int = 0,
        opportunity_type: OpportunityType | None = None,
        sig: str | None = None,
        organization: str | None = None,
        newest_first: bool = False,
    ) -> list[models.Opportunity]:
        """The officer review queue, optionally narrowed.

        Defaults to oldest first so nothing starves at the bottom of a long
        queue; ``newest_first`` is for triaging what just arrived.
        """
        query = apply_filters(
            sa.select(models.Opportunity).where(
                models.Opportunity.status == OpportunityStatus.PENDING_REVIEW
            ),
            OpportunityFilters(
                opportunity_types=(opportunity_type,) if opportunity_type else (),
                sigs=(sig,) if sig else (),
                organization=organization,
            ),
        )
        order = (
            models.Opportunity.discovered_at.desc()
            if newest_first
            else models.Opportunity.discovered_at.asc()
        )
        result = await self._session.execute(query.order_by(order).offset(offset).limit(limit))
        return list(result.scalars().all())

    async def count_pending_review(
        self,
        *,
        opportunity_type: OpportunityType | None = None,
        sig: str | None = None,
        organization: str | None = None,
    ) -> int:
        query = apply_filters(
            sa.select(sa.func.count())
            .select_from(models.Opportunity)
            .where(models.Opportunity.status == OpportunityStatus.PENDING_REVIEW),
            OpportunityFilters(
                opportunity_types=(opportunity_type,) if opportunity_type else (),
                sigs=(sig,) if sig else (),
                organization=organization,
            ),
        )
        result = await self._session.execute(query)
        return int(result.scalar_one())

    async def mark_queued_for_review(
        self, opportunity_id: UUID, *, channel_id: int, message_id: int
    ) -> bool:
        """Record that the review card was posted, moving the row to pending review.

        Guarded on the current status so a concurrent run cannot overwrite a
        decision an officer has already made.

        Returns:
            Whether this call performed the transition.
        """
        result = await self._session.execute(
            sa.update(models.Opportunity)
            .where(
                models.Opportunity.id == opportunity_id,
                models.Opportunity.status == OpportunityStatus.DISCOVERED,
            )
            .values(
                status=OpportunityStatus.PENDING_REVIEW,
                review_channel_id=channel_id,
                review_message_id=message_id,
            )
        )
        return bool(cast("CursorResult[Any]", result).rowcount)

    async def record_decision(
        self,
        opportunity_id: UUID,
        *,
        decision: ReviewDecision,
        reviewer_discord_id: int,
        reason: str | None = None,
    ) -> bool:
        """Apply an officer decision exactly once.

        The status change is a conditional ``UPDATE`` restricted to rows that
        have not yet been decided. Two officers pressing Approve at the same
        time both run this; the database serializes them and only one sees
        ``rowcount == 1``, so only one publishes and only one audit row is
        written.

        Returns:
            ``True`` if this caller owns the decision, ``False`` if the
            opportunity was already decided.
        """
        target_status = (
            OpportunityStatus.APPROVED
            if decision is ReviewDecision.APPROVED
            else OpportunityStatus.REJECTED
        )
        result = await self._session.execute(
            sa.update(models.Opportunity)
            .where(
                models.Opportunity.id == opportunity_id,
                models.Opportunity.status.notin_(list(TERMINAL_REVIEW_STATUSES)),
            )
            .values(status=target_status)
        )
        if not cast("CursorResult[Any]", result).rowcount:
            logger.info(
                "review.decision_ignored",
                opportunity_id=str(opportunity_id),
                decision=decision.value,
                reviewer_id=reviewer_discord_id,
                reason="already decided",
            )
            return False

        self._session.add(
            models.ReviewAction(
                opportunity_id=opportunity_id,
                reviewer_discord_id=reviewer_discord_id,
                decision=decision,
                reason=reason,
            )
        )
        await self._session.flush()
        return True

    async def approve_without_review(self, opportunity_id: UUID) -> bool:
        """Approve an opportunity from a source trusted to skip officer review.

        No ``review_actions`` row is written: nobody reviewed it, and recording
        a decision that no human made would corrupt the audit trail. The
        source's policy in the ``sources`` table is the record of why this
        happened.

        Guarded on the current status for the same reason as
        :meth:`record_decision`: a concurrent officer decision wins.
        """
        result = await self._session.execute(
            sa.update(models.Opportunity)
            .where(
                models.Opportunity.id == opportunity_id,
                models.Opportunity.status == OpportunityStatus.DISCOVERED,
            )
            .values(status=OpportunityStatus.APPROVED)
        )
        return bool(cast("CursorResult[Any]", result).rowcount)

    async def list_awaiting_publication(self, limit: int) -> list[models.Opportunity]:
        """Approved opportunities that have not reached the members' channel.

        Covers both auto-approved rows and rows whose publish previously failed
        and was rolled back to ``APPROVED``.
        """
        result = await self._session.execute(
            sa.select(models.Opportunity)
            .where(models.Opportunity.status == OpportunityStatus.APPROVED)
            .order_by(models.Opportunity.discovered_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def record_publication(
        self, opportunity_id: UUID, *, channel_id: int, message_id: int | None
    ) -> bool:
        """Claim the right to publish an opportunity to a channel.

        Idempotency is enforced by the unique constraint on
        ``(opportunity_id, channel_id)``: a duplicate insert raises
        ``IntegrityError``, which we translate into "someone already published
        this" rather than a crash.

        Returns:
            ``True`` when this call created the publication record.
        """
        savepoint = await self._session.begin_nested()
        try:
            self._session.add(
                models.PublicationRecord(
                    opportunity_id=opportunity_id, channel_id=channel_id, message_id=message_id
                )
            )
            await self._session.flush()
        except IntegrityError:
            await savepoint.rollback()
            logger.info(
                "publication.duplicate_suppressed",
                opportunity_id=str(opportunity_id),
                channel_id=channel_id,
            )
            return False
        await savepoint.commit()

        await self._session.execute(
            sa.update(models.Opportunity)
            .where(models.Opportunity.id == opportunity_id)
            .values(status=OpportunityStatus.PUBLISHED)
        )
        return True

    async def release_publication(
        self, opportunity_id: UUID, *, channel_id: int, revert_status: OpportunityStatus
    ) -> None:
        """Undo a publication claim whose Discord send failed.

        Without this, a transient Discord error would leave an opportunity
        marked published with no message anywhere, and the unique constraint
        would then block every retry.
        """
        await self._session.execute(
            sa.delete(models.PublicationRecord).where(
                models.PublicationRecord.opportunity_id == opportunity_id,
                models.PublicationRecord.channel_id == channel_id,
            )
        )
        await self._session.execute(
            sa.update(models.Opportunity)
            .where(
                models.Opportunity.id == opportunity_id,
                models.Opportunity.status == OpportunityStatus.PUBLISHED,
            )
            .values(status=revert_status)
        )

    async def set_publication_message(
        self, opportunity_id: UUID, *, channel_id: int, message_id: int
    ) -> None:
        """Attach the Discord message ID to an existing publication record."""
        await self._session.execute(
            sa.update(models.PublicationRecord)
            .where(
                models.PublicationRecord.opportunity_id == opportunity_id,
                models.PublicationRecord.channel_id == channel_id,
            )
            .values(message_id=message_id)
        )

    async def is_published_to(self, opportunity_id: UUID, channel_id: int) -> bool:
        result = await self._session.execute(
            sa.select(sa.func.count())
            .select_from(models.PublicationRecord)
            .where(
                models.PublicationRecord.opportunity_id == opportunity_id,
                models.PublicationRecord.channel_id == channel_id,
            )
        )
        return bool(result.scalar_one())

    #: Statuses a member is allowed to see. Baseline rows are included: they
    #: are genuine open opportunities that simply predate ACM Hub, and hiding
    #: them would make the very first weeks of the bot look empty.
    MEMBER_VISIBLE_STATUSES = (OpportunityStatus.PUBLISHED,)

    def _member_visible(self, filters: OpportunityFilters) -> Select[Any]:
        return apply_filters(
            sa.select(models.Opportunity).where(
                models.Opportunity.status.in_(self.MEMBER_VISIBLE_STATUSES)
            ),
            filters,
        )

    async def search_published(
        self, filters: OpportunityFilters, *, limit: int, offset: int = 0
    ) -> list[models.Opportunity]:
        """One page of published opportunities matching ``filters``.

        ``LIMIT``/``OFFSET`` are applied by the database. Paginating in Python
        would mean loading the entire published set to show ten rows, which
        stops working long before the catalogue does.

        The sort is ``discovered_at`` descending with ``id`` as a tiebreaker.
        Without the tiebreaker two rows discovered in the same batch share a
        timestamp, their relative order is undefined, and a member paging
        forward can see the same posting twice or miss one.
        """
        result = await self._session.execute(
            self._member_visible(filters)
            .order_by(models.Opportunity.discovered_at.desc(), models.Opportunity.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def count_published(self, filters: OpportunityFilters) -> int:
        """How many published opportunities match ``filters``.

        Counted in SQL so the paginator can show "page 2 of 9" without
        fetching page 9.
        """
        result = await self._session.execute(
            apply_filters(
                sa.select(sa.func.count())
                .select_from(models.Opportunity)
                .where(models.Opportunity.status.in_(self.MEMBER_VISIBLE_STATUSES)),
                filters,
            )
        )
        return int(result.scalar_one())

    async def open_canonical_keys(self, source_name: str) -> dict[str, UUID]:
        """Canonical keys of this source's rows that are not closed or rejected.

        The closure sweep diffs this against what the source just returned.
        """
        result = await self._session.execute(
            sa.select(models.Opportunity.canonical_key, models.Opportunity.id).where(
                models.Opportunity.source_name == source_name,
                models.Opportunity.status.in_(list(CLOSEABLE_STATUSES)),
            )
        )
        return dict(result.all())  # type: ignore[arg-type]

    async def mark_closed(
        self, opportunity_ids: Sequence[UUID], *, now: datetime | None = None
    ) -> list[models.Opportunity]:
        """Close the given opportunities and return the rows that changed.

        Guarded on :data:`CLOSEABLE_STATUSES` so a row an officer rejected, or
        one another sweep already closed, is left alone. The rows are returned
        so the caller can edit their published Discord messages; nothing is
        deleted, so the publication history stays intact.
        """
        if not opportunity_ids:
            return []

        identifiers = list(opportunity_ids)
        result = await self._session.execute(
            sa.select(models.Opportunity).where(
                models.Opportunity.id.in_(identifiers),
                models.Opportunity.status.in_(list(CLOSEABLE_STATUSES)),
            )
        )
        rows = list(result.scalars().all())
        timestamp = now or datetime.now(tz=UTC)
        for row in rows:
            row.status = OpportunityStatus.CLOSED
            row.closed_at = timestamp
        await self._session.flush()
        return rows

    async def publication_messages(self, opportunity_id: UUID) -> list[tuple[int, int]]:
        """``(channel_id, message_id)`` for every message we actually sent.

        Records whose send never completed have a ``NULL`` message ID and are
        skipped rather than guessed at.
        """
        result = await self._session.execute(
            sa.select(
                models.PublicationRecord.channel_id, models.PublicationRecord.message_id
            ).where(
                models.PublicationRecord.opportunity_id == opportunity_id,
                models.PublicationRecord.message_id.is_not(None),
            )
        )
        return [(channel, message) for channel, message in result.all() if message is not None]

    async def reopen_for_review(self, opportunity_id: UUID) -> bool:
        """Put a rejected opportunity back into the review queue.

        This is the supported way to undo a mis-click: it clears the stale
        review card pointer so the next queue pass posts a fresh card, and it
        leaves the original rejection in ``review_actions`` as history. Officers
        never need to touch the database.

        Returns:
            Whether a rejected opportunity by that ID existed.
        """
        result = await self._session.execute(
            sa.update(models.Opportunity)
            .where(
                models.Opportunity.id == opportunity_id,
                models.Opportunity.status == OpportunityStatus.REJECTED,
            )
            .values(
                status=OpportunityStatus.DISCOVERED,
                review_channel_id=None,
                review_message_id=None,
                is_baseline=False,
            )
        )
        return bool(cast("CursorResult[Any]", result).rowcount)

    async def list_rejected(self, *, limit: int = 10, offset: int = 0) -> list[models.Opportunity]:
        """Most recently rejected opportunities, so an officer can reconsider one."""
        result = await self._session.execute(
            sa.select(models.Opportunity)
            .where(models.Opportunity.status == OpportunityStatus.REJECTED)
            .order_by(models.Opportunity.updated_at.desc(), models.Opportunity.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def release_baseline(self, source_name: str, *, limit: int) -> int:
        """Move baseline rows into the normal review flow.

        Bounded by ``limit`` because the caller then posts a review card per
        row; releasing an entire backlog in one call is exactly the Discord
        flood the baseline mechanism exists to prevent.

        Returns:
            How many rows were released.
        """
        selected = await self._session.execute(
            sa.select(models.Opportunity.id)
            .where(
                models.Opportunity.source_name == source_name,
                models.Opportunity.is_baseline.is_(True),
                models.Opportunity.status == OpportunityStatus.DISCOVERED,
            )
            .order_by(models.Opportunity.discovered_at.desc())
            .limit(limit)
        )
        identifiers = list(selected.scalars().all())
        if not identifiers:
            return 0
        result = await self._session.execute(
            sa.update(models.Opportunity)
            .where(models.Opportunity.id.in_(identifiers))
            .values(is_baseline=False)
        )
        return int(cast("CursorResult[Any]", result).rowcount)

    async def count_baseline(self, source_name: str) -> int:
        result = await self._session.execute(
            sa.select(sa.func.count())
            .select_from(models.Opportunity)
            .where(
                models.Opportunity.source_name == source_name,
                models.Opportunity.is_baseline.is_(True),
            )
        )
        return int(result.scalar_one())

    async def count_by_status(self) -> dict[OpportunityStatus, int]:
        result = await self._session.execute(
            sa.select(models.Opportunity.status, sa.func.count()).group_by(
                models.Opportunity.status
            )
        )
        return dict(result.all())  # type: ignore[arg-type]
