"""Member interest preferences and the UI for editing them.

Preferences exist for exactly one reason: to give ``/acm opportunities`` a
sensible default so a member does not have to type filters to see relevant
postings. They are **not** a notification subscription. ACM Hub does not DM
anyone, and saving interests here never causes a message to be sent.

The two dimensions - opportunity type and SIG - are edited independently. Each
select menu writes only its own dimension, so choosing SIGs cannot silently
erase a type preference set five seconds earlier.

Empty is a real, storable answer and means *no preference on this dimension*,
which widens results rather than narrowing them to nothing. That is spelled out
in the UI, because "I selected nothing" and "I want nothing" are easy to
confuse.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord

from acm_hub.bot.views import OwnedView
from acm_hub.config.sigs import SIG_BY_NAME, SIG_NAMES
from acm_hub.db.repositories.subscriptions import SubscriptionRepository
from acm_hub.domain.enums import OpportunityType
from acm_hub.observability.logging import get_logger

if TYPE_CHECKING:
    from acm_hub.db.session import Database

logger = get_logger(__name__)

#: Discord allows at most 25 options in a select menu.
MAX_SELECT_OPTIONS = 25

TYPE_LABELS: dict[OpportunityType, str] = {
    OpportunityType.INTERNSHIP: "Internships",
    OpportunityType.RESEARCH: "Research positions",
    OpportunityType.FELLOWSHIP: "Fellowships",
    OpportunityType.HACKATHON: "Hackathons",
    OpportunityType.SCHOLARSHIP: "Scholarships",
    OpportunityType.CONFERENCE: "Conferences",
    OpportunityType.PROGRAM: "Programs",
    OpportunityType.OTHER: "Everything else",
}


def type_label(value: str) -> str:
    """Human-readable name for a stored opportunity type value."""
    try:
        return TYPE_LABELS[OpportunityType(value)]
    except ValueError:
        return value


def sig_label(name: str) -> str:
    rule = SIG_BY_NAME.get(name)
    return f"{name} \u2014 {rule.display_name}" if rule else name


def describe_interests(opportunity_types: list[str], sigs: list[str]) -> str:
    """One line summarising what a member has saved.

    Used both in ``/acm subscribe`` and above personalized results, so a member
    always knows why they are seeing what they are seeing.
    """
    parts: list[str] = []
    if opportunity_types:
        parts.append(", ".join(type_label(value) for value in opportunity_types))
    if sigs:
        parts.append(", ".join(sigs))
    return " \u00b7 ".join(parts) if parts else "no filters saved (you see everything)"


class _TypeSelect(discord.ui.Select["SubscriptionView"]):
    """Opportunity-type preferences. Writes only the type dimension."""

    def __init__(self, selected: list[str]) -> None:
        super().__init__(
            placeholder="Kinds of opportunity you care about",
            min_values=0,
            max_values=len(TYPE_LABELS),
            options=[
                discord.SelectOption(
                    label=label, value=member.value, default=member.value in selected
                )
                for member, label in TYPE_LABELS.items()
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        await self.view.save(interaction, opportunity_types=list(self.values))


class _SigSelect(discord.ui.Select["SubscriptionView"]):
    """SIG preferences. Writes only the SIG dimension."""

    def __init__(self, selected: list[str]) -> None:
        options = [
            discord.SelectOption(
                label=name,
                value=name,
                description=(SIG_BY_NAME[name].display_name if name in SIG_BY_NAME else None),
                default=name in selected,
            )
            for name in SIG_NAMES[:MAX_SELECT_OPTIONS]
        ]
        super().__init__(
            placeholder="ACM SIGs you are interested in",
            min_values=0,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        await self.view.save(interaction, sigs=list(self.values))


class SubscriptionView(OwnedView):
    """The ``/acm subscribe`` editor.

    Holds the member's saved values as plain lists and opens a session only
    while writing, so leaving the message on screen does not hold a connection.
    """

    def __init__(
        self,
        owner_id: int,
        *,
        database: Database,
        opportunity_types: list[str],
        sigs: list[str],
    ) -> None:
        super().__init__(owner_id)
        self._database = database
        self._types = opportunity_types
        self._sigs = sigs
        self.add_item(_TypeSelect(opportunity_types))
        self.add_item(_SigSelect(sigs))

    def summary(self) -> str:
        return (
            "**Your ACM interests**\n"
            f"{describe_interests(self._types, self._sigs)}\n\n"
            "These personalize `/acm opportunities`. ACM Hub never sends direct "
            "messages.\n"
            "Each menu saves on its own, so changing one leaves the other alone. "
            "Deselecting everything in a menu means *no preference* for that menu, "
            "which shows you more, not less."
        )

    async def save(
        self,
        interaction: discord.Interaction,
        *,
        opportunity_types: list[str] | None = None,
        sigs: list[str] | None = None,
    ) -> None:
        """Persist one dimension and refresh the message.

        ``None`` means "this menu was not the one submitted", which the
        repository translates into leaving that column untouched.
        """
        async with self._database.session() as session:
            await SubscriptionRepository(session).upsert(
                self._owner_id, opportunity_types=opportunity_types, sigs=sigs
            )

        if opportunity_types is not None:
            self._types = opportunity_types
        if sigs is not None:
            self._sigs = sigs

        logger.info(
            "subscription.saved",
            user_id=self._owner_id,
            types=len(self._types),
            sigs=len(self._sigs),
        )
        await interaction.response.edit_message(content=self.summary(), view=self)

    @discord.ui.button(label="Clear all", style=discord.ButtonStyle.secondary, row=2)
    async def clear(
        self, interaction: discord.Interaction, _button: discord.ui.Button[Any]
    ) -> None:
        """Reset both dimensions, so results are unfiltered again."""
        async with self._database.session() as session:
            await SubscriptionRepository(session).delete(self._owner_id)
        self._types = []
        self._sigs = []
        # Rebuilt so the menus show nothing selected rather than stale ticks.
        self.clear_items()
        self.add_item(_TypeSelect([]))
        self.add_item(_SigSelect([]))
        self.add_item(self.clear)
        await interaction.response.edit_message(content=self.summary(), view=self)
