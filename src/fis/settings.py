"""Deploy configuration for the Fleet Index Service.

One `.env`, one `FIS_` prefix. The defaults are the **dev universe on this
machine**: a fresh clone runs against `d1` and a local Postgres with no
`.env` at all, because dev is where the service is exercised first and
most often. A deployed box overrides both, and provisioning writes that
`.env`.
"""

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # The universe this FIS instance serves. A FIS holds one registry
    # mirror, and a registry is scoped to one universe; runs partition
    # inside that scope (the lease key is (principal, run)), so one
    # instance serves every run its broker hosts. Note this is the bare
    # universe token (`d1`), not the broker vhost (`d1__1`, which is
    # `<universe>__<run>`) — the validator below rejects the vhost form.
    universe: str = "d1"
    db_url: SecretStr = SecretStr("postgresql+psycopg://fis:fispass@localhost:5436/fis")
    db_echo: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="fis_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @field_validator("universe")
    @classmethod
    def _check_universe(cls, v: str) -> str:
        if not (v.isalnum() and v.islower() and v[0].isalpha()):
            raise ValueError(
                f"universe {v!r} must be a single lowercase alphanumeric word "
                "(the first dotted segment of every alias this FIS authorizes)"
            )
        if v[0] not in "dhw":
            raise ValueError(
                f"universe {v!r} must start with its kind letter: d (dev), "
                "h (hybrid), or w (production)"
            )
        return v


class ApiRunSettings(BaseSettings):
    """Bind config for `fis api`. Loopback by default: the broker calls FIS
    over localhost from the same box, so a non-local bind would widen the
    auth surface and is a deliberate declaration."""

    api_host: str = "127.0.0.1"
    api_port: int = 8080

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="fis_",
        env_nested_delimiter="__",
        extra="ignore",
    )
