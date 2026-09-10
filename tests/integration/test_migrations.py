"""Migration parity.

The initial migration is hand-written, so it can drift from the ORM models. This
compares the schema Alembic produces against the schema SQLAlchemy expects and
fails on any difference, which is the only way to know the migration is actually
correct without a running PostgreSQL instance.

The comparison runs on SQLite, so it verifies table, column, nullability, index
and constraint structure rather than PostgreSQL-specific types.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine, inspect

from acm_hub.db.models import Base

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Differences SQLite cannot express and that therefore are not real drift.
IGNORED_DIFF_KINDS = frozenset({"modify_default", "modify_type"})


def _alembic_config() -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return config


@contextmanager
def _database_url_env(url: str) -> Iterator[None]:
    """Point ``alembic/env.py`` at a throwaway database for the duration."""
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


@pytest.fixture
def migrated_engine(tmp_path: Path) -> Iterator[Engine]:
    """A database built purely by running the real Alembic migrations.

    Driving ``alembic upgrade head`` through the project's own ``env.py`` means
    this also covers the migration environment, not just the revision scripts.
    """
    path = tmp_path / "migrated.db"
    with _database_url_env(f"sqlite+aiosqlite:///{path}"):
        command.upgrade(_alembic_config(), "head")

    engine = create_engine(f"sqlite:///{path}")
    yield engine
    engine.dispose()


def test_migrations_create_every_table(migrated_engine: Engine) -> None:
    tables = set(inspect(migrated_engine).get_table_names()) - {"alembic_version"}
    assert tables == set(Base.metadata.tables)


def test_migrated_schema_matches_the_orm_models(migrated_engine: Engine) -> None:
    """Fails if a model changes without a matching migration."""
    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(connection)
        diffs = compare_metadata(context, Base.metadata)

    meaningful = [
        diff
        for diff in diffs
        if not (isinstance(diff, tuple) and str(diff[0]) in IGNORED_DIFF_KINDS)
    ]
    assert meaningful == [], f"schema drift between models and migrations: {meaningful}"


def test_the_publication_uniqueness_constraint_exists(migrated_engine: Engine) -> None:
    """The database-level guarantee against double-publishing."""
    constraints = inspect(migrated_engine).get_unique_constraints("publication_records")
    assert any(
        set(constraint["column_names"]) == {"opportunity_id", "channel_id"}
        for constraint in constraints
    )


def test_the_canonical_key_is_unique(migrated_engine: Engine) -> None:
    """The database-level guarantee against storing one posting twice."""
    inspector = inspect(migrated_engine)
    unique_columns = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("opportunities")
    } | {
        tuple(index["column_names"])
        for index in inspector.get_indexes("opportunities")
        if index["unique"]
    }
    assert ("canonical_key",) in unique_columns


def test_the_sig_projection_is_unique_per_opportunity(migrated_engine: Engine) -> None:
    constraints = inspect(migrated_engine).get_unique_constraints("opportunity_sigs")
    assert any(
        set(constraint["column_names"]) == {"opportunity_id", "sig"} for constraint in constraints
    )


def test_sources_carry_a_publication_policy_column(migrated_engine: Engine) -> None:
    columns = {column["name"] for column in inspect(migrated_engine).get_columns("sources")}
    assert "publication_policy" in columns
    assert "initialized_at" in columns
    assert "enabled" not in columns


def test_downgrade_removes_everything(tmp_path: Path) -> None:
    """A migration that cannot be rolled back is a migration you cannot deploy."""
    path = tmp_path / "roundtrip.db"
    with _database_url_env(f"sqlite+aiosqlite:///{path}"):
        config = _alembic_config()
        command.upgrade(config, "head")
        command.downgrade(config, "base")

    engine = create_engine(f"sqlite:///{path}")
    try:
        remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()
    assert remaining == set()
