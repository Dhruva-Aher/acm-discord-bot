"""Interactive components shared by the member and officer commands.

Two rules are enforced here rather than being re-implemented per command.

**Ownership.** A view attached to one person's command response is controlled by
that person only. Discord delivers button presses from anyone who can see the
message, so without this check a bystander could page through someone else's
results or confirm someone else's bulk action.

**No held sessions.** Views live for minutes and are driven by user input, so
they never hold a database session, connection or ORM row. They hold plain
values, and each interaction opens its own short-lived session through a
callback. A view that kept a session open would pin a connection from the pool
for the whole time a member left the message sitting on screen.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import discord

from acm_hub.observability.logging import get_logger

logger = get_logger(__name__)

#: Views stop responding after this long. Discord greys out the components and
#: the member can simply re-run the command.
DEFAULT_TIMEOUT_SECONDS = 180.0


class OwnedView(discord.ui.View):
    """A view only its requesting user may operate."""

    def __init__(self, owner_id: int, *, timeout: float | None = DEFAULT_TIMEOUT_SECONDS) -> None:
        super().__init__(timeout=timeout)
        self._owner_id = owner_id
        self._message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self._owner_id:
            return True
        await interaction.response.send_message(
            "These controls belong to whoever ran the command. Run it yourself to browse.",
            ephemeral=True,
        )
        return False

    def bind(self, message: discord.Message | None) -> None:
        """Remember the message so :meth:`on_timeout` can disable the controls."""
        self._message = message

    def _disable_all(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button | discord.ui.Select):
                child.disabled = True

    async def on_timeout(self) -> None:
        """Grey out the controls so nobody clicks a dead button."""
        self._disable_all()
        if self._message is None:
            return
        try:
            await self._message.edit(view=self)
        except discord.HTTPException:
            # Purely cosmetic. An expired token or deleted message is fine.
            logger.debug("view.timeout_edit_failed", message_id=self._message.id)


@dataclass(slots=True)
class Page[T]:
    """One page of results plus the totals needed to render the controls."""

    items: list[T]
    total: int
    offset: int
    page_size: int

    @property
    def page_number(self) -> int:
        return self.offset // self.page_size + 1

    @property
    def page_count(self) -> int:
        return max(1, -(-self.total // self.page_size))

    @property
    def has_previous(self) -> bool:
        return self.offset > 0

    @property
    def has_next(self) -> bool:
        return self.offset + self.page_size < self.total


#: Fetches one page. Called fresh on every button press, which is also what
#: keeps a long-lived view from serving stale results.
PageFetcher = Callable[[int], Awaitable[Page[Any]]]
#: Renders a page into an embed.
PageRenderer = Callable[[Page[Any]], discord.Embed]


class PaginatedView(OwnedView):
    """Previous / Next controls over a database-paged result set.

    The view stores only an offset. Each press re-queries through
    ``fetch_page``, so nothing is cached in the bot process and no session
    outlives the interaction that opened it.
    """

    def __init__(
        self,
        owner_id: int,
        *,
        fetch_page: PageFetcher,
        render: PageRenderer,
        page: Page[Any],
    ) -> None:
        super().__init__(owner_id)
        self._fetch_page = fetch_page
        self._render = render
        self._page = page
        self._sync_buttons()

    @property
    def page(self) -> Page[Any]:
        return self._page

    def embed(self) -> discord.Embed:
        return self._render(self._page)

    def _sync_buttons(self) -> None:
        self.previous_page.disabled = not self._page.has_previous
        self.next_page.disabled = not self._page.has_next

    async def _move(self, interaction: discord.Interaction, offset: int) -> None:
        self._page = await self._fetch_page(max(0, offset))
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous_page(
        self, interaction: discord.Interaction, _button: discord.ui.Button[Any]
    ) -> None:
        await self._move(interaction, self._page.offset - self._page.page_size)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next_page(
        self, interaction: discord.Interaction, _button: discord.ui.Button[Any]
    ) -> None:
        await self._move(interaction, self._page.offset + self._page.page_size)


#: Runs the confirmed action and returns a report to show the officer.
ConfirmedAction = Callable[[], Awaitable[str]]


class ConfirmationView(OwnedView):
    """A two-step gate in front of an action that affects many rows at once.

    Bulk approval publishes to a channel every member reads, and it cannot be
    undone with one click, so it is never a single press.
    """

    def __init__(self, owner_id: int, *, action: ConfirmedAction, confirm_label: str) -> None:
        super().__init__(owner_id, timeout=60.0)
        self._action = action
        self.confirm.label = confirm_label

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, _button: discord.ui.Button[Any]
    ) -> None:
        self._disable_all()
        # Edited before the action runs so a slow bulk publish cannot be
        # double-confirmed by an impatient second click.
        await interaction.response.edit_message(content="Working\u2026", view=self)
        report = await self._action()
        await interaction.edit_original_response(content=report, view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, _button: discord.ui.Button[Any]
    ) -> None:
        self._disable_all()
        await interaction.response.edit_message(content="Cancelled. Nothing changed.", view=None)
        self.stop()
