"""Officer approval buttons.

Two properties matter here.

**Persistence.** Review cards outlive the process. The buttons are
:class:`discord.ui.DynamicItem` subclasses whose ``custom_id`` embeds the
opportunity UUID, so after a restart the bot reconstructs the handler from the
button itself instead of needing an in-memory view registry.

**Idempotency.** Two officers can press Approve within the same second. The
decision is applied by a conditional UPDATE in
:meth:`OpportunityRepository.record_decision`, so exactly one caller wins; the
loser is told the opportunity was already decided and nothing is published
twice. The publish step is guarded a second time by a unique constraint, so even
a decision race cannot produce two member-facing posts.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from uuid import UUID

import discord

from acm_hub.bot.permissions import is_officer
from acm_hub.db.repositories.opportunities import OpportunityRepository
from acm_hub.domain.enums import ReviewDecision
from acm_hub.observability.logging import get_logger
from acm_hub.publishing.discord import ChannelUnavailableError

if TYPE_CHECKING:
    from acm_hub.bot.client import AcmHubBot

logger = get_logger(__name__)

APPROVE_PREFIX = "acmhub:review:approve:"
REJECT_PREFIX = "acmhub:review:reject:"
_UUID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

MAX_REJECTION_REASON_LENGTH = 300


def _bot(interaction: discord.Interaction) -> AcmHubBot:
    """Narrow the interaction's client to our bot type."""
    from acm_hub.bot.client import AcmHubBot

    client = interaction.client
    assert isinstance(client, AcmHubBot)
    return client


async def _reject_unauthorized(interaction: discord.Interaction) -> bool:
    """Reply and return ``True`` when the presser is not an officer."""
    bot = _bot(interaction)
    if is_officer(interaction.user, bot.settings.officer_role_id):
        return False
    await interaction.response.send_message(
        "Only ACM officers can review opportunities.", ephemeral=True
    )
    logger.info(
        "review.unauthorized_press",
        user_id=interaction.user.id,
        message_id=interaction.message.id if interaction.message else None,
    )
    return True


async def _finalize_card(interaction: discord.Interaction, summary: str) -> None:
    """Disable the buttons on the review card and stamp it with the outcome."""
    message = interaction.message
    if message is None:
        return
    embed = message.embeds[0] if message.embeds else discord.Embed()
    embed.add_field(name="Decision", value=summary, inline=False)
    try:
        await message.edit(embed=embed, view=None)
    except discord.HTTPException as exc:
        # The decision is already committed; a failed cosmetic edit must not
        # look like a failed approval.
        logger.warning(
            "review.card_edit_failed",
            message_id=message.id,
            error=f"{type(exc).__name__}: {exc}",
        )


async def _apply_decision(
    interaction: discord.Interaction,
    opportunity_id: UUID,
    decision: ReviewDecision,
    *,
    reason: str | None = None,
) -> None:
    """Record a decision and, when approved, publish the opportunity."""
    bot = _bot(interaction)

    async with bot.database.session() as session:
        accepted = await OpportunityRepository(session).record_decision(
            opportunity_id,
            decision=decision,
            reviewer_discord_id=interaction.user.id,
            reason=reason,
        )

    if not accepted:
        await interaction.followup.send(
            "This opportunity was already reviewed by another officer.", ephemeral=True
        )
        await _finalize_card(interaction, "Already reviewed")
        return

    if decision is ReviewDecision.REJECTED:
        await interaction.followup.send("Rejected. It will not be re-queued.", ephemeral=True)
        await _finalize_card(
            interaction,
            f"Rejected by {interaction.user.mention}" + (f"\nReason: {reason}" if reason else ""),
        )
        return

    try:
        published = await bot.publisher.publish_approved(opportunity_id)
    except (discord.HTTPException, ChannelUnavailableError) as exc:
        await interaction.followup.send(
            "Approved, but publishing to the opportunities channel failed. "
            "It will stay approved and can be retried with `/acm-admin sync`.",
            ephemeral=True,
        )
        logger.error(
            "review.publish_failed",
            opportunity_id=str(opportunity_id),
            error=f"{type(exc).__name__}: {exc}",
            exc_info=True,
        )
        return

    if published is None:
        await interaction.followup.send(
            "Approved, but it had already been published.", ephemeral=True
        )
    else:
        await interaction.followup.send("Approved and published.", ephemeral=True)

    await _finalize_card(interaction, f"Approved by {interaction.user.mention}")


class RejectionReasonModal(discord.ui.Modal, title="Reject opportunity"):
    """Collects an optional reason so rejections are auditable."""

    reason: discord.ui.TextInput[discord.ui.Modal] = discord.ui.TextInput(
        label="Reason (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=MAX_REJECTION_REASON_LENGTH,
        placeholder="e.g. not relevant to ACM members, or duplicate of an existing post",
    )

    def __init__(self, opportunity_id: UUID) -> None:
        super().__init__()
        self._opportunity_id = opportunity_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _apply_decision(
            interaction,
            self._opportunity_id,
            ReviewDecision.REJECTED,
            reason=str(self.reason.value).strip() or None,
        )


class ApproveButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]],
    template=re.escape(APPROVE_PREFIX) + f"(?P<opportunity_id>{_UUID_PATTERN})",
):
    """Approve and publish an opportunity."""

    def __init__(self, opportunity_id: UUID) -> None:
        self.opportunity_id = opportunity_id
        super().__init__(
            discord.ui.Button(
                label="Approve",
                style=discord.ButtonStyle.success,
                custom_id=f"{APPROVE_PREFIX}{opportunity_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> ApproveButton:
        return cls(UUID(match["opportunity_id"]))

    async def callback(self, interaction: discord.Interaction) -> Any:
        if await _reject_unauthorized(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _apply_decision(interaction, self.opportunity_id, ReviewDecision.APPROVED)


class RejectButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]],
    template=re.escape(REJECT_PREFIX) + f"(?P<opportunity_id>{_UUID_PATTERN})",
):
    """Reject an opportunity, capturing an optional reason."""

    def __init__(self, opportunity_id: UUID) -> None:
        self.opportunity_id = opportunity_id
        super().__init__(
            discord.ui.Button(
                label="Reject",
                style=discord.ButtonStyle.danger,
                custom_id=f"{REJECT_PREFIX}{opportunity_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> RejectButton:
        return cls(UUID(match["opportunity_id"]))

    async def callback(self, interaction: discord.Interaction) -> Any:
        if await _reject_unauthorized(interaction):
            return
        await interaction.response.send_modal(RejectionReasonModal(self.opportunity_id))


def build_review_view(opportunity_id: UUID, source_url: str) -> discord.ui.View:
    """Build the Approve / Reject / Open Source control row.

    ``timeout=None`` keeps the view usable indefinitely; the dynamic items make
    it work after a restart as well.
    """
    view: discord.ui.View = discord.ui.View(timeout=None)
    view.add_item(ApproveButton(opportunity_id))
    view.add_item(RejectButton(opportunity_id))
    view.add_item(
        discord.ui.Button(label="Open Source", style=discord.ButtonStyle.link, url=source_url)
    )
    return view
