"""Shared test fixtures.

Tests never reach the network and never touch Discord. HTTP is mocked with
``respx`` and the database is an in-memory SQLite instance, which is why the
models avoid PostgreSQL-only column types.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from acm_hub.db.models import Base
from acm_hub.db.repositories.sources import SourceRepository
from acm_hub.db.session import Database
from acm_hub.domain.enums import OpportunityType, PublicationPolicy, VerificationStatus
from acm_hub.domain.source import RawOpportunity

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool:
    """Keep PostgreSQL tests out of the default SQLite run.

    They live under ``tests/postgres/`` and need a live instance. Collecting
    them only when ``ACMHUB_POSTGRES_URL`` is set is what lets ``pytest`` stay
    secret-free and network-free in ordinary development and in the lint job.
    """
    del config
    if collection_path.is_dir() and collection_path.name == "postgres":
        return not os.environ.get("ACMHUB_POSTGRES_URL")
    return False


#: Fixed clock so canonical keys and hashes are reproducible across runs.
FROZEN_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURE_DIR


@pytest.fixture
def asu_mentors_html() -> str:
    """Trimmed capture of the official Fulton Forge mentors page.

    Includes the Ready-to-mentor section and the expo-only section so tests
    can prove expo mentors are never treated as recruiting.
    """
    return (FIXTURE_DIR / "asu_mentors.html").read_text()


@pytest.fixture
def acm_conferences_html() -> str:
    """Official-format ACM conferences listing used by the collector tests."""
    return (FIXTURE_DIR / "acm_conferences.html").read_text()


@pytest.fixture
def acm_cloudflare_html() -> str:
    return (FIXTURE_DIR / "acm_cloudflare_block.html").read_text()


@pytest.fixture
def simplify_listings() -> list[dict[str, Any]]:
    """A trimmed but unmodified slice of the real upstream feed.

    Captured from ``SimplifyJobs/Summer2027-Internships`` and kept small on
    purpose: it covers every observed category and sponsorship value, a remote
    listing, a multi-location listing and an inactive listing.
    """
    payload = json.loads((FIXTURE_DIR / "simplify_listings.json").read_text())
    assert isinstance(payload, list)
    return payload


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    """A fresh SQLite database with the schema created from the ORM metadata.

    Backed by a file rather than ``:memory:`` so every pooled connection sees
    the same database, and torn down with the tmp directory.
    """
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'acm_hub_test.db'}")

    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    yield db
    await db.dispose()


async def initialize_source(
    database: Database,
    name: str,
    *,
    display_name: str = "Test Source",
    source_url: str = "https://example.test/source",
    policy: PublicationPolicy = PublicationPolicy.MANUAL_REVIEW,
) -> None:
    """Register ``name`` as a source that has already completed a first run.

    Most tests are about what happens to *news*, so they need the source past
    its initial import - otherwise everything they collect is stored as
    baseline and held back from the review queue. Tests that are specifically
    about the initial import simply do not call this.
    """
    async with database.session() as session:
        sources = SourceRepository(session)
        await sources.register(name, display_name, source_url)
        await sources.set_policy(name, policy)
        await sources.mark_initialized(name, run_id=uuid4())


def make_raw(
    *,
    title: str = "Software Engineering Intern",
    organization: str = "Cloudflare",
    application_url: str = "https://boards.example.com/jobs/123",
    source_name: str = "test_source",
    source_external_id: str | None = "ext-1",
    locations: tuple[str, ...] = ("Austin, TX",),
    **overrides: Any,
) -> RawOpportunity:
    """Build a ``RawOpportunity`` with sensible defaults for tests."""
    payload: dict[str, Any] = {
        "source_name": source_name,
        "source_external_id": source_external_id,
        "source_url": "https://example.com/source",
        "application_url": application_url,
        "title": title,
        "organization": organization,
        "opportunity_type": OpportunityType.INTERNSHIP,
        "locations": locations,
        "verification_status": VerificationStatus.SOURCE_CLAIMED,
    }
    payload.update(overrides)
    return RawOpportunity(**payload)
