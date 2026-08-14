"""The universe declaration is validated at boot.

A FIS instance mirrors exactly one registry, so a malformed or missing
universe must fail loudly at startup rather than mirroring the wrong tree.
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
        "",
    ],
)
def test_malformed_universes_rejected(universe: str) -> None:
    with pytest.raises(ValidationError):
        Settings(universe=universe, db_url=DB_URL)


def test_universe_is_required() -> None:
    with pytest.raises(ValidationError):
        Settings(db_url=DB_URL)
