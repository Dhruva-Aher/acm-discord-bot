"""Officer authorization checks."""

from __future__ import annotations

from unittest.mock import MagicMock

import discord

from acm_hub.bot.permissions import is_officer

OFFICER_ROLE_ID = 444444444444444444


def member(*, role_ids: tuple[int, ...] = (), manage_guild: bool = False) -> MagicMock:
    fake = MagicMock(spec=discord.Member)
    fake.roles = [MagicMock(id=role_id) for role_id in role_ids]
    fake.guild_permissions = discord.Permissions(manage_guild=manage_guild)
    return fake


class TestWithAnOfficerRoleConfigured:
    def test_a_member_with_the_role_is_an_officer(self) -> None:
        assert is_officer(member(role_ids=(OFFICER_ROLE_ID,)), OFFICER_ROLE_ID)

    def test_a_member_without_the_role_is_not(self) -> None:
        assert not is_officer(member(role_ids=(999,)), OFFICER_ROLE_ID)

    def test_the_configured_role_takes_precedence_over_server_permissions(self) -> None:
        """An explicit officer role must not be widened by Discord admin rights."""
        assert not is_officer(member(manage_guild=True), OFFICER_ROLE_ID)

    def test_one_matching_role_among_many_is_enough(self) -> None:
        assert is_officer(member(role_ids=(1, 2, OFFICER_ROLE_ID, 3)), OFFICER_ROLE_ID)


class TestWithoutAnOfficerRoleConfigured:
    def test_manage_guild_is_the_fallback(self) -> None:
        assert is_officer(member(manage_guild=True), None)

    def test_an_ordinary_member_is_not_an_officer(self) -> None:
        assert not is_officer(member(), None)


def test_a_non_member_user_is_never_an_officer() -> None:
    """Outside a guild there is no role information to trust."""
    user = MagicMock(spec=discord.User)
    assert not is_officer(user, OFFICER_ROLE_ID)
    assert not is_officer(user, None)
