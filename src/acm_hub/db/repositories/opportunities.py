"""Opportunity persistence, review transitions and publication bookkeeping."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from acm_hub.db import models
from acm_hub.domain.enums import (
    TERMINAL_REVIEW_STATUSES,
    OpportunityStatus,
    OpportunityType,
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

    async def upsert(
        self, opportunity: DomainOpportunity, *, now: datetime | None = None
    ) -> tuple[models.Opportunity, ChangeType]:
        """Insert a new opportunity or refresh an existing one.

        A rediscovered opportunity keeps whatever review state it already has.
        In particular a rejected opportunity is refreshed but stays ``REJECTED``,
        which is what stops the next polling cycle from putting an
        already-declined listing back in front of the officers.
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
            )
            self._session.add(row)
            await self._session.flush()
            return row, ChangeType.NEW

        existing.last_verified_at = timestamp
        if existing.content_hash == opportunity.content_hash:
            return existing, ChangeType.UNCHANGED

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
        existing.sponsorship = opportunity.eligibility.sponsorship
        existing.citizenship_requirement = opportunity.eligibility.citizenship_requirement
        existing.degree_requirements = list(opportunity.eligibility.degree_requirements)
        existing.expires_at = opportunity.expires_at
        existing.verification_status = opportunity.verification_status
        await self._session.flush()
        return existing, ChangeType.CHANGED

    async def list_awaiting_queue(self, limit: int) -> list[models.Opportunity]:
        """Newly discovered opportunities that have not been sent for review yet."""
        result = await self._session.execute(
            sa.select(models.Opportunity)
            .where(
                models.Opportunity.status == OpportunityStatus.DISCOVERED,
                models.Opportunity.review_message_id.is_(None),
            )
            .order_by(models.Opportunity.discovered_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_pending_review(self, limit: int = 25) -> list[models.Opportunity]:
        result = await self._session.execute(
            sa.select(models.Opportunity)
            .where(models.Opportunity.status == OpportunityStatus.PENDING_REVIEW)
            .order_by(models.Opportunity.discovered_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

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

    async def list_published(
        self,
        *,
        limit: int = 10,
        opportunity_types: list[OpportunityType] | None = None,
        organization: str | None = None,
    ) -> list[models.Opportunity]:
        """Most recently discovered published opportunities, newest first."""
        query = sa.select(models.Opportunity).where(
            models.Opportunity.status == OpportunityStatus.PUBLISHED
        )
        if opportunity_types:
            query = query.where(models.Opportunity.opportunity_type.in_(opportunity_types))
        if organization:
            query = query.where(models.Opportunity.organization.ilike(f"%{organization}%"))
        result = await self._session.execute(
            query.order_by(models.Opportunity.discovered_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def count_by_status(self) -> dict[OpportunityStatus, int]:
        result = await self._session.execute(
            sa.select(models.Opportunity.status, sa.func.count()).group_by(
                models.Opportunity.status
            )
        )
        return dict(result.all())  # type: ignore[arg-type]
