import re
import uuid
from datetime import UTC, datetime
from typing import Annotated

from pydantic import BeforeValidator, Field


# --- patterns ---
LEFT_RIGHT_DOT_PATTERN = re.compile(r"^[a-z][a-z0-9]*(\.[a-z0-9]+)*$")

UNIVERSE_RUN_PATTERN = re.compile(r"^[a-z][a-z0-9]*__[1-9][0-9]*$")

UUID4_STR_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


# --- methods ---
def is_left_right_dot(v: str) -> str:
    if not isinstance(v, str):
        raise ValueError(f"<{v}>: LeftRightDot must be a string.")

    if not LEFT_RIGHT_DOT_PATTERN.fullmatch(v):
        raise ValueError(f"<{v}>: Fails LeftRightDot format.")

    return v


def is_universe_run(v: str) -> str:
    if not isinstance(v, str):
        raise ValueError(f"<{v}>: UniverseRun must be a string.")

    if not UNIVERSE_RUN_PATTERN.fullmatch(v):
        raise ValueError(f"<{v}>: Fails UniverseRun format.")

    return v


def is_utc_milliseconds(v: int) -> int:
    if not isinstance(v, int):
        raise TypeError("Not an int!")
    start_date = datetime(2000, 1, 1, tzinfo=UTC)
    end_date = datetime(3000, 1, 1, tzinfo=UTC)

    start_timestamp_ms = int(start_date.timestamp() * 1000)
    end_timestamp_ms = int(end_date.timestamp() * 1000)

    if v < start_timestamp_ms:
        raise ValueError(f"{v} must be after Jan 1 2000")
    if v > end_timestamp_ms:
        raise ValueError(f"{v} must be before Jan 1 3000")
    return v


def is_uuid4_str(v: str) -> str:
    if not isinstance(v, str):
        raise ValueError(f"<{v}>: uuid4.str must be a string.")

    if not UUID4_STR_PATTERN.fullmatch(v):
        raise ValueError(f"<{v}>: Fails uuid4.str format.")

    try:
        u = uuid.UUID(v)
    except Exception as e:
        raise ValueError(f"Invalid UUID4: {v}  <{e}>") from e
    if u.version != 4:
        raise ValueError(
            f"{v} is valid uid, but of version {u.version}. Fails UuidCanonicalTextual"
        )
    return str(u)


# --- annotated types ---
LeftRightDot = Annotated[
    str,
    BeforeValidator(is_left_right_dot),
]

NonEmptyString = Annotated[
    str,
    Field(min_length=1),
]

UniverseRun = Annotated[
    str,
    BeforeValidator(is_universe_run),
]

UTCMilliseconds = Annotated[
    int,
    BeforeValidator(is_utc_milliseconds),
]

UUID4Str = Annotated[
    str,
    BeforeValidator(is_uuid4_str),
]
