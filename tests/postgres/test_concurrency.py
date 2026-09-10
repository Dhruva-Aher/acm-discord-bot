"""PostgreSQL-backed behaviour that SQLite cannot honestly exercise.

The uniqueness guarantees that stop double-publishing and double-deciding are
database constraints. SQLite serializes writers, so two concurrent sessions
there never actually race. PostgreSQL does, which is why these tests exist and
why they are skipped unless a live instance is pointed at by
``ACMHUB_POSTGRES_URL``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from acm_hub.db import models
from acm_hub.db.models import Base
from acm_hub.db.repositories.opportunities import OpportunityRepository
from acm_hub.db.session import Database
from acm_hub.domain.enums import ReviewDecision
from tests.integration.test_review_workflow import OPPORTUNITIES_CHANNEL_ID, seed

pytestmark = pytest.mark.postgres

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _postgres_url() -> str | None:
    url = os.environ.get("ACMHUB_POSTGRES_URL")
    if url and url.startswith("postgresql+asyncpg://"):
        return url
    return None


@pytest.fixture(scope="session")
def postgres_url() -> str:
    url = _postgres_url()
    if url is None:
        pytest.skip("set ACMHUB_POSTGRES_URL to a postgresql+asyncpg:// URL")
    return url


@pytest.fixture
async def pg_database(postgres_url: str) -> AsyncIterator[Database]:
    """A throwaway schema, created from the real Alembic migrations.

    Dropping every table first keeps a leftover row from a crashed previous
    run from poisoning uniqueness tests.
    """
    admin = create_async_engine(postgres_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
    await admin.dispose()

    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = postgres_url
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    # alembic/env.py uses asyncio.run. That cannot nest inside this fixture's
    # running loop, so the upgrade happens on a thread with its own loop.
    await asyncio.to_thread(command.upgrade, config, "head")

    db = Database(postgres_url)
    try:
        yield db
    finally:
        await db.dispose()
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


class TestPostgresMigrations:
    async def test_alembic_creates_the_orm_tables(self, pg_database: Database) -> None:
        async with pg_database.engine.connect() as connection:
            tables = await connection.run_sync(
                lambda sync: set(inspect(sync).get_table_names()) - {"alembic_version"}
            )
        assert tables == set(Base.metadata.tables)

    async def test_the_publication_constraint_is_present(self, pg_database: Database) -> None:
        async with pg_database.engine.connect() as connection:
            constraints = await connection.run_sync(
                lambda sync: inspect(sync).get_unique_constraints("publication_records")
            )
        assert any(
            set(constraint["column_names"]) == {"opportunity_id", "channel_id"}
            for constraint in constraints
        )

    async def test_the_sig_projection_constraint_is_present(self, pg_database: Database) -> None:
        async with pg_database.engine.connect() as connection:
            constraints = await connection.run_sync(
                lambda sync: inspect(sync).get_unique_constraints("opportunity_sigs")
            )
        assert any(
            set(constraint["column_names"]) == {"opportunity_id", "sig"}
            for constraint in constraints
        )


class TestPostgresConcurrency:
    async def test_two_simultaneous_publication_claims_only_one_wins(
        self, pg_database: Database
    ) -> None:
        row = await seed(pg_database)

        async def claim() -> bool:
            async with pg_database.session() as session:
                return await OpportunityRepository(session).record_publication(
                    row.id, channel_id=OPPORTUNITIES_CHANNEL_ID, message_id=None
                )

        results = await asyncio.gather(claim(), claim())
        assert sorted(results) == [False, True]

    async def test_two_simultaneous_decisions_only_one_wins(self, pg_database: Database) -> None:
        row = await seed(pg_database)

        async def decide(reviewer: int) -> bool:
            async with pg_database.session() as session:
                return await OpportunityRepository(session).record_decision(
                    row.id, decision=ReviewDecision.APPROVED, reviewer_discord_id=reviewer
                )

        results = await asyncio.gather(decide(1), decide(2))
        assert sorted(results) == [False, True]

        async with pg_database.session() as session:
            refreshed = await OpportunityRepository(session).get(row.id)
        assert refreshed is not None
        assert len(refreshed.reviews) == 1

    async def test_a_duplicate_canonical_key_is_rejected_by_the_database(
        self, pg_database: Database
    ) -> None:
        row = await seed(pg_database)
        with pytest.raises(IntegrityError):
            async with pg_database.session() as session:
                session.add(
                    models.Opportunity(
                        canonical_key=row.canonical_key,
                        content_hash="other",
                        source_name="stub",
                        source_url="https://example.test/source",
                        application_url="https://example.test/other",
                        title="Other",
                        organization="Other",
                    )
                )
