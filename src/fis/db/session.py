"""SQLAlchemy engine + session factory for the Fleet Index Service.

The engine is built from `fis.settings.Settings` (`FIS_DB_URL` ← `.env`)
**lazily, on first use**: importing this module (e.g. for `fis --help`) needs
no environment; only actually opening a session does.
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from fis.settings import Settings


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    settings = Settings()
    engine = create_engine(
        settings.db_url.get_secret_value(),
        echo=settings.db_echo,
        future=True,
    )
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def SessionLocal() -> Session:
    """Open a session on the env-configured engine (built on first call)."""
    return _session_factory()()
