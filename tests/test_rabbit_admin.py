"""The real connection-killer against a mock management API: the kill is
close-by-username, the confirm is the by-username view (the broker's
tracking table), and anything short of a confirmed-zero answer fails
closed. The dev battery runs the same class against a real broker.
"""

from __future__ import annotations

import httpx
import pytest

from fis.rabbit_admin import RabbitMgmtKiller

PRINCIPAL = "5d1e0f3a-0f1b-4c8e-9b2f-3d4e5f6a7b8c"
BASE = "http://broker:15672"
BY_USER = f"/api/connections/username/{PRINCIPAL}"


class MgmtApi:
    """A mock management API recording every request. `counts` scripts the
    successive by-username GET answers (the last repeats); `delete_status`
    is what the close answers."""

    def __init__(self, *counts: int, delete_status: int = 204) -> None:
        self.counts = list(counts) or [0]
        self.delete_status = delete_status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.path == BY_USER
        if request.method == "DELETE":
            return httpx.Response(self.delete_status)
        n = self.counts.pop(0) if len(self.counts) > 1 else self.counts[0]
        return httpx.Response(200, json=[{"name": f"c{i}"} for i in range(n)])

    def deletes(self) -> list[str]:
        return [r.headers["X-Reason"] for r in self.requests if r.method == "DELETE"]

    def gets(self) -> int:
        return sum(1 for r in self.requests if r.method == "GET")


def killer(api: MgmtApi, confirm_s: float = 0.5) -> RabbitMgmtKiller:
    transport = httpx.MockTransport(api.handler)
    return RabbitMgmtKiller(BASE, "u", "p", confirm_s=confirm_s, transport=transport)


@pytest.fixture(autouse=True)
def short_grace(monkeypatch):
    monkeypatch.setattr("fis.rabbit_admin.CLOSE_OK_GRACE_S", 0.15)


def test_kill_is_close_by_username_then_confirmed_by_the_tracked_view():
    api = MgmtApi(0)
    assert killer(api).kill(principal_id=PRINCIPAL, vhost="d1__1") is True
    assert api.deletes() == ["fis-supersession"]
    assert api.gets() == 1  # an empty kill still asks the broker


def test_kill_polls_until_the_broker_reports_empty():
    api = MgmtApi(1, 0)
    assert killer(api).kill(principal_id=PRINCIPAL, vhost="d1__1") is True
    assert api.gets() == 2
    assert api.deletes() == ["fis-supersession"]  # answered its close: never forced


def test_kill_forces_a_predecessor_that_never_answers_the_close():
    api = MgmtApi()

    def handler(request: httpx.Request) -> httpx.Response:
        api.requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        # still tracked until the second close forces the reader down
        gone = len(api.deletes()) == 2
        return httpx.Response(200, json=[] if gone else [{"name": "c0"}])

    k = RabbitMgmtKiller(
        BASE, "u", "p", confirm_s=2.0, transport=httpx.MockTransport(handler)
    )
    assert k.kill(principal_id=PRINCIPAL, vhost="d1__1") is True
    assert api.deletes() == ["fis-supersession", "fis-supersession-force"]


def test_kill_fails_closed_when_the_connection_outlives_the_budget():
    api = MgmtApi(1)
    assert (
        killer(api, confirm_s=0.4).kill(principal_id=PRINCIPAL, vhost="d1__1") is False
    )
    assert api.deletes() == ["fis-supersession", "fis-supersession-force"]


def test_kill_fails_closed_when_the_management_api_refuses():
    api = MgmtApi(0, delete_status=401)
    assert killer(api).kill(principal_id=PRINCIPAL, vhost="d1__1") is False
    assert api.gets() == 0  # never confirmed what was never closed


def test_kill_fails_closed_when_the_management_api_is_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    k = RabbitMgmtKiller(BASE, "u", "p", transport=httpx.MockTransport(handler))
    assert k.kill(principal_id=PRINCIPAL, vhost="d1__1") is False


def test_kill_identity_counts_then_closes_by_username():
    api = MgmtApi(2)
    assert killer(api).kill_identity(principal_id=PRINCIPAL) == 2
    assert api.deletes() == ["fis-reconvergence"]


def test_kill_identity_is_best_effort():
    api = MgmtApi(2, delete_status=500)
    assert killer(api).kill_identity(principal_id=PRINCIPAL) == 0
