"""Closing a superseded instance's broker connections via the management API.

The gate's synchronous supersession (build step 3) must *confirm* that no
connection remains for a (principal, run) before it admits a successor — "an
empty kill is success". That confirmation is a management-API round trip
against the broker on the same box; this module is the only thing in FIS that
talks to the broker rather than to Postgres.

`ConnectionKiller` is a `Protocol` so the gate runs against a fake in the dev
battery and against a real broker only on staging — the verification that
counts. `RabbitMgmtKiller` is the real implementation; `default_killer()`
builds it from settings for the running service.
"""

from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Protocol
from urllib.parse import quote

import httpx

from fis.settings import RabbitMgmtSettings

logger = logging.getLogger(__name__)


class ConnectionKiller(Protocol):
    def kill(self, *, principal_id: str, vhost: str) -> bool:
        """Close every broker connection for this identity on this vhost and
        confirm none remain.

        Returns `True` on a confirmed-empty result — including the no-op case,
        since nothing to close is success (every restart is a supersession and
        a clean stop leaves nothing behind). Returns `False` if the management
        API could not be reached or the kill could not be confirmed; the gate
        reads `False` as fail-closed and denies the successor.
        """
        ...

    def kill_identity(self, *, principal_id: str) -> int:
        """Close every connection for this identity across all vhosts, and
        return how many were closed.

        This is the reconvergence flush behind a registry rename: it is not
        gated on confirmation the way `kill` is (the reconnect re-authorizes
        against the new alias), so it is best-effort — a management-API
        failure is logged, not fatal.
        """
        ...


class RabbitMgmtKiller:
    """Kills connections through `rabbitmq_management`'s HTTP API.

    Connections carry the authenticated `user` (the cert CN = principal id)
    and their `vhost`, so a (principal, run) is exactly the set with a
    matching `user` and `vhost`. There is at most one authorized instance per
    that key, so closing all of them closes the predecessor and nothing else.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 5.0,
        confirm_s: float = 2.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = (username, password)
        self.timeout = timeout
        self.confirm_s = confirm_s  # how long to wait for the listing to empty

    def _targets(
        self, client: httpx.Client, principal_id: str, vhost: str
    ) -> list[str]:
        resp = client.get(f"{self.base_url}/api/connections")
        resp.raise_for_status()
        return [
            conn["name"]
            for conn in resp.json()
            if conn.get("user") == principal_id and conn.get("vhost") == vhost
        ]

    def kill(self, *, principal_id: str, vhost: str) -> bool:
        try:
            with httpx.Client(auth=self.auth, timeout=self.timeout) as client:
                for name in self._targets(client, principal_id, vhost):
                    resp = client.delete(
                        f"{self.base_url}/api/connections/{quote(name, safe='')}",
                        headers={"X-Reason": "fis-supersession"},
                    )
                    resp.raise_for_status()
                # The DELETE closes the connection, but the management
                # database drops it only when the connection_closed event is
                # processed, so a listing taken at once still shows it. Poll
                # briefly; the budget stays well inside the broker's 10 s
                # handshake timeout that bounds the whole gate.
                deadline = time.monotonic() + self.confirm_s
                while True:
                    remaining = self._targets(client, principal_id, vhost)
                    if not remaining or time.monotonic() >= deadline:
                        break
                    time.sleep(0.05)
        except Exception as e:  # noqa: BLE001 -- any failure is "unconfirmed" → fail closed
            logger.warning(
                "supersession kill failed for %s on %s: %s", principal_id, vhost, e
            )
            return False

        if remaining:
            logger.warning(
                "supersession kill unconfirmed: %d connection(s) remain for %s on %s",
                len(remaining),
                principal_id,
                vhost,
            )
            return False
        return True

    def kill_identity(self, *, principal_id: str) -> int:
        killed = 0
        try:
            with httpx.Client(auth=self.auth, timeout=self.timeout) as client:
                resp = client.get(f"{self.base_url}/api/connections")
                resp.raise_for_status()
                names = [
                    conn["name"]
                    for conn in resp.json()
                    if conn.get("user") == principal_id
                ]
                for name in names:
                    resp = client.delete(
                        f"{self.base_url}/api/connections/{quote(name, safe='')}",
                        headers={"X-Reason": "fis-reconvergence"},
                    )
                    resp.raise_for_status()
                    killed += 1
        except Exception as e:  # noqa: BLE001 -- best-effort flush; log and move on
            logger.warning(
                "reconvergence kill failed for %s after %d closed: %s",
                principal_id,
                killed,
                e,
            )
        return killed


@lru_cache(maxsize=1)
def default_killer() -> RabbitMgmtKiller:
    """The service's killer, built once from the box's `.env`."""
    cfg = RabbitMgmtSettings()
    return RabbitMgmtKiller(
        base_url=cfg.mgmt_url,
        username=cfg.mgmt_user.get_secret_value(),
        password=cfg.mgmt_password.get_secret_value(),
    )
