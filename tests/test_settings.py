"""Settings default to the dev universe, and the universe is validated at boot.

A fresh clone runs against `d1` and a local Postgres with no `.env`. A
malformed universe still fails loudly rather than mirroring nothing.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fis.settings import Settings

DB_URL = "postgresql+psycopg://fis:fispass@localhost:5436/fis"


@pytest.mark.parametrize("universe", ["d1", "hw1", "w"])
def test_valid_universes_accepted(universe: str) -> None:
    assert Settings(universe=universe, db_url=DB_URL).universe == universe


@pytest.mark.parametrize(
    "universe",
    [
        "D1",  # not lowercase
        "1d",  # does not start with a letter
        "d1.isone",  # a whole alias, not the universe segment
        "x1",  # not a known universe kind
        "d1__1",  # the broker vhost, not the universe token
        "",
    ],
)
def test_malformed_universes_rejected(universe: str) -> None:
    with pytest.raises(ValidationError):
        Settings(universe=universe, db_url=DB_URL)


def test_defaults_are_the_local_dev_universe() -> None:
    settings = Settings()
    assert settings.universe == "d1"
    assert "localhost" in settings.db_url.get_secret_value()
