"""Shared fixtures for the FIS test suite.

The schema tests need a real Postgres — the single-writer guarantee is a
partial unique index, which is the thing most worth testing for real and
which sqlite expresses differently. By default we spin up an ephemeral one
with `testcontainers` (hermetic, CI-friendly). Set `FIS_TEST_PG_URL` to point
the suite at an already-running Postgres instead — e.g. the dev
docker-compose on `localhost:5437` — for a faster local loop. When neither an
override nor a working Docker is available, the integration tests self-skip.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from fis.db.models import Base


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    """A Postgres URL: env override if set, else an ephemeral container."""
    override = os.environ.get("FIS_TEST_PG_URL")
    if override:
        yield override
        return

    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # pragma: no cover - depends on the installed extra
        pytest.skip("no FIS_TEST_PG_URL and testcontainers not installed")

    # The container is stopped by the context manager; ryuk, testcontainers'
    # reaper sidecar, adds nothing here and cannot mount the Docker socket
    # on Docker Desktop for Mac, which would skip every db test.
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    try:
        with PostgresContainer("postgres:16") as postgres:
            yield postgres.get_connection_url(driver="psycopg")
    except Exception as e:  # noqa: BLE001 -- Docker not available / image pull failed
        pytest.skip(f"could not start a testcontainers Postgres: {e}")


@pytest.fixture(scope="session")
def engine(pg_url):
    """An engine with the FIS schema applied."""
    eng = create_engine(pg_url, future=True)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine) -> Iterator[Session]:
    """A session whose rows are cleared after each test."""
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    with factory() as s:
        yield s
        s.rollback()
        for table in reversed(Base.metadata.sorted_tables):
            s.execute(table.delete())
        s.commit()
