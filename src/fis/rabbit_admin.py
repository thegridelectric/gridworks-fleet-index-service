"""Closing a superseded instance's broker connections, and confirming it.

The gate's synchronous supersession must *confirm* that no
connection remains for an identity before it admits a successor — "an empty
kill is success". Both halves are management-API calls against the broker
on the same box, and this module is the only thing in FIS that talks to the
broker rather than to Postgres:

- the **kill** is `DELETE /api/connections/username/<principal-id>`. The
  broker applies it to live connection state, so it closes a connection the
  management *listing* has not yet seen (the listing is served from the
  stats database and lags by up to its collection interval). It is
  broker-wide for the identity, which is exact while a broker hosts one run.
- the **confirm** polls `GET /api/connections/username/<principal-id>`
  until the identity holds no connection. The broker serves that view from
  its connection-tracking table — its own record of established
  connections, the same table the close acts on — not from the stats
  database, and it answers without asking any connection process, so a
  connection still mid-handshake (the successor itself) is neither counted
  nor consulted. The listing gates no safety decision, neither to find nor
  to confirm.
- the close is **two-phase** because it returns before the sockets are
  gone and a predecessor may never let them go. The broker sends
  `Connection.Close` and waits for the client's `Close-Ok` before it tears
  the reader down; a responsive client answers within milliseconds, but a
  wedged one never does and the reader sits in its closing state (routing
  nothing, so the successor is safe, but still tracked) until the broker's
  30 s close timeout. A second close on a reader already closing forces it
  down at once (the reader's `terminate` is forced whenever the connection
  is not running), so the kill waits a short grace for the close-ok, closes
  again to force any straggler, and only then confirms. Even forced, a
  reader whose peer is not reading lingers about 5 s in the TLS socket
  close (OTP waits that long for a close_notify the peer never sends),
  which the confirm budget must cover.

`ConnectionKiller` is a `Protocol` so the gate runs against a fake in the
handler tests; `RabbitMgmtKiller` is the real implementation, exercised by
the dev battery and, as the verification that counts, on staging.
`default_killer()` builds it from settings for the running service.
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

# How long a predecessor gets to answer the broker's Connection.Close before
# the kill forces it. A live client answers in milliseconds; the grace only
# spares a healthy one the forced teardown and bounds how long a wedged one
# delays its successor.
CLOSE_OK_GRACE_S = 1.0


class ConnectionKiller(Protocol):
    def kill(self, *, principal_id: str, vhost: str) -> bool:
        """Close every broker connection for this identity and confirm none
        remain.

        Returns `True` on a confirmed-empty result — including the no-op case,
        since nothing to close is success (every restart is a supersession and
        a clean stop leaves nothing behind). Returns `False` if the kill could
        not be issued or could not be confirmed within the budget; the gate
        reads `False` as fail-closed and denies the successor. `vhost` is the
        run the successor claimed; the close is broker-wide for the identity.
        """
        ...

    def kill_identity(self, *, principal_id: str) -> int:
        """Close every connection for this identity across all vhosts, and
        return how many were live when the close was issued.

        This is the reconvergence flush behind a registry rename: it is not
        gated on confirmation the way `kill` is (the reconnect re-authorizes
        against the new alias), so it is best-effort — a broker failure is
        logged, not fatal.
        """
        ...


class RabbitMgmtKiller:
    """Kills and confirms through the management API's by-username
    connection resource (see the module docstring for why that one)."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 5.0,
        confirm_s: float = 8.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = (username, password)
        self.timeout = timeout
        self.confirm_s = confirm_s  # budget for the broker to drop the sockets
        self.transport = transport  # tests inject a mock; None → real HTTP

    def by_username(self, principal_id: str) -> str:
        return (
            f"{self.base_url}/api/connections/username/{quote(principal_id, safe='')}"
        )

    def close(self, client: httpx.Client, principal_id: str, reason: str) -> None:
        resp = client.delete(
            self.by_username(principal_id), headers={"X-Reason": reason}
        )
        resp.raise_for_status()

    def live_count(self, client: httpx.Client, principal_id: str) -> int:
        """How many established connections the broker tracks for this
        identity. Raises on anything but a clean answer: the caller decides
        what an unknown answer means (the gate fails closed)."""
        resp = client.get(self.by_username(principal_id))
        resp.raise_for_status()
        return len(resp.json())

    def drain(self, client: httpx.Client, principal_id: str, deadline: float) -> int:
        """Poll until the identity holds no connection or the deadline
        passes; the count still held."""
        while True:
            remaining = self.live_count(client, principal_id)
            if remaining == 0 or time.monotonic() >= deadline:
                return remaining
            time.sleep(0.1)

    def kill(self, *, principal_id: str, vhost: str) -> bool:
        try:
            with httpx.Client(
                auth=self.auth, timeout=self.timeout, transport=self.transport
            ) as client:
                self.close(client, principal_id, "fis-supersession")
                # The 204 means the broker accepted the close, not that the
                # sockets are gone. Give a responsive predecessor its grace
                # to answer the close, then force whatever is still closing,
                # and confirm inside a budget that keeps the whole gate
                # under the broker's 10 s handshake timeout.
                deadline = time.monotonic() + self.confirm_s
                grace = min(deadline, time.monotonic() + CLOSE_OK_GRACE_S)
                remaining = self.drain(client, principal_id, grace)
                if remaining:
                    self.close(client, principal_id, "fis-supersession-force")
                    remaining = self.drain(client, principal_id, deadline)
        except Exception as e:  # noqa: BLE001 -- any failure is "unconfirmed" → fail closed
            logger.warning(
                "supersession kill failed for %s (run %s): %s", principal_id, vhost, e
            )
            return False

        if remaining:
            logger.warning(
                "supersession kill unconfirmed: %d connection(s) remain for %s "
                "after %.1fs (run %s)",
                remaining,
                principal_id,
                self.confirm_s,
                vhost,
            )
            return False
        return True

    def kill_identity(self, *, principal_id: str) -> int:
        try:
            with httpx.Client(
                auth=self.auth, timeout=self.timeout, transport=self.transport
            ) as client:
                killed = self.live_count(client, principal_id)
                self.close(client, principal_id, "fis-reconvergence")
        except Exception as e:  # noqa: BLE001 -- best-effort flush; log and move on
            logger.warning("reconvergence kill failed for %s: %s", principal_id, e)
            return 0
        return killed


@lru_cache(maxsize=1)
def default_killer() -> RabbitMgmtKiller:
    """The service's killer, built once from the box's `.env`."""
    cfg = RabbitMgmtSettings()
    return RabbitMgmtKiller(
        base_url=cfg.mgmt_url,
        username=cfg.mgmt_user.get_secret_value(),
        password=cfg.mgmt_password.get_secret_value(),
        confirm_s=cfg.confirm_s,
    )
