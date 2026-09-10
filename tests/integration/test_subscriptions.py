"""Subscription preference persistence.

Milestone 5 builds the ``/acm subscribe`` UI on top of this. Persisting
preferences correctly now means that command is a presentation change rather
than a data-model change later.
"""

from __future__ import annotations

from acm_hub.config.sigs import SIG_NAMES
from acm_hub.db.repositories.subscriptions import SubscriptionRepository
from acm_hub.db.session import Database

MEMBER_ID = 777777777777777777


class TestSubscriptionPersistence:
    async def test_preferences_are_created(self, database: Database) -> None:
        async with database.session() as session:
            await SubscriptionRepository(session).upsert(
                MEMBER_ID, opportunity_types=["internship"], sigs=["SIGAI", "SIGSOFT"]
            )

        async with database.session() as session:
            stored = await SubscriptionRepository(session).get(MEMBER_ID)
        assert stored is not None
        assert stored.opportunity_types == ["internship"]
        assert stored.sigs == ["SIGAI", "SIGSOFT"]

    async def test_preferences_survive_a_new_session(self, database: Database) -> None:
        async with database.session() as session:
            await SubscriptionRepository(session).upsert(MEMBER_ID, sigs=["SIGHPC"])
        async with database.session() as session:
            assert (await SubscriptionRepository(session).get(MEMBER_ID)).sigs == ["SIGHPC"]  # type: ignore[union-attr]

    async def test_updating_one_dimension_leaves_the_other_intact(self, database: Database) -> None:
        """The two select menus submit separately and must not clobber each other."""
        async with database.session() as session:
            repository = SubscriptionRepository(session)
            await repository.upsert(MEMBER_ID, opportunity_types=["internship"], sigs=["SIGAI"])
            await repository.upsert(MEMBER_ID, sigs=["SIGSAC"])

        async with database.session() as session:
            stored = await SubscriptionRepository(session).get(MEMBER_ID)
        assert stored is not None
        assert stored.opportunity_types == ["internship"]
        assert stored.sigs == ["SIGSAC"]

    async def test_preferences_can_be_cleared_explicitly(self, database: Database) -> None:
        async with database.session() as session:
            repository = SubscriptionRepository(session)
            await repository.upsert(MEMBER_ID, sigs=["SIGAI"])
            await repository.upsert(MEMBER_ID, sigs=[])

        async with database.session() as session:
            assert (await SubscriptionRepository(session).get(MEMBER_ID)).sigs == []  # type: ignore[union-attr]

    async def test_unsubscribing_removes_the_record(self, database: Database) -> None:
        async with database.session() as session:
            repository = SubscriptionRepository(session)
            await repository.upsert(MEMBER_ID, sigs=["SIGAI"])
            assert await repository.delete(MEMBER_ID) is True

        async with database.session() as session:
            assert await SubscriptionRepository(session).get(MEMBER_ID) is None

    async def test_unsubscribing_an_unknown_member_is_harmless(self, database: Database) -> None:
        async with database.session() as session:
            assert await SubscriptionRepository(session).delete(123) is False

    async def test_members_are_independent(self, database: Database) -> None:
        async with database.session() as session:
            repository = SubscriptionRepository(session)
            await repository.upsert(MEMBER_ID, sigs=["SIGAI"])
            await repository.upsert(MEMBER_ID + 1, sigs=["SIGSOFT"])
            assert await repository.count() == 2

        async with database.session() as session:
            stored = await SubscriptionRepository(session).get(MEMBER_ID)
        assert stored is not None
        assert stored.sigs == ["SIGAI"]

    async def test_large_discord_snowflakes_round_trip(self, database: Database) -> None:
        """Snowflakes exceed 32 bits, so the column must be a BIGINT."""
        snowflake = 1234567890123456789
        async with database.session() as session:
            await SubscriptionRepository(session).upsert(snowflake, sigs=["SIGAI"])
        async with database.session() as session:
            stored = await SubscriptionRepository(session).get(snowflake)
        assert stored is not None
        assert stored.discord_user_id == snowflake

    async def test_every_configured_sig_can_be_stored(self, database: Database) -> None:
        async with database.session() as session:
            await SubscriptionRepository(session).upsert(MEMBER_ID, sigs=list(SIG_NAMES))
        async with database.session() as session:
            stored = await SubscriptionRepository(session).get(MEMBER_ID)
        assert stored is not None
        assert stored.sigs == list(SIG_NAMES)
