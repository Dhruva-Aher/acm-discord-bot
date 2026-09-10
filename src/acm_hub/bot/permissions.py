"""Officer authorization.

ACM Hub does not manage roles and does not need elevated Discord permissions to
decide who an officer is. It checks one of two things:

* ``OFFICER_ROLE_ID`` when configured - the explicit, preferred option, since it
  lets the ACM officer team grant review rights without touching server-wide
  permissions; or
* the built-in **Manage Server** permission as a fallback.

Sapphire remains the source of truth for roles themselves; this is only a read.
"""

from __future__ import annotations

import discord


def is_officer(user: discord.abc.User | discord.Member, officer_role_id: int | None) -> bool:
    """Whether ``user`` may approve or reject opportunities.

    Non-members (for example a DM context) are never officers, because role and
    permission information is only meaningful inside the guild.
    """
    if not isinstance(user, discord.Member):
        return False

    if officer_role_id is not None:
        return any(role.id == officer_role_id for role in user.roles)

    return bool(user.guild_permissions.manage_guild)
