"""The HTTP surface, driven the way the broker drives it.

`rabbitmq-auth-backend-http` POSTs a form and reads a plain-text verdict.
These tests cover what the `decide_*` tests cannot: the form parsing, the
`allow <run>` body the broker turns into the connection's user tag, and the
audit row written after the response. The app's session factory is pointed
at the test Postgres; the gate logic itself is proven in `test_gate.py`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import fis.api
from fis.api import create_app
from fis.db.models import AuthEventSql, PrincipalKind, PrincipalSql, PrincipalStatus
from fis.sema.enums import FisAuthorizationDecision
from fis.sema.types import FisConnectClaims

UNIVERSE = "hw1"
RUN = "hw1__1"
SERVICE_ID = "5e971ce0-0000-4000-8000-000000000001"
SERVICE_ALIAS = "hw1.gnr"
INSTANCE_A = "aaaaaaaa-1111-4aaa-8aaa-aaaaaaaaaaaa"


class FakeKiller:
    def kill(self, *, principal_id: str, vhost: str) -> bool:
        return True

    def kill_identity(self, *, principal_id: str) -> int:
        return 0


class FakeRegistry:
    def get_forest(self, roots):
        return None

    def get_by_id(self, g_node_id: str):
        return None


def test_ping() -> None:
    with TestClient(create_app(reconcile=False)) as client:
        response = client.get("/ping")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- the broker's calls, over HTTP ------------------------------------------

pytestmark = pytest.mark.integration


@pytest.fixture
def client(engine, session: Session, monkeypatch) -> Iterator[TestClient]:
    """The app on the test database: `SessionLocal` swapped for the test
    engine's factory, so the gate and the audit write land where the test
    can read them back (and the `session` fixture clears them after)."""
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    monkeypatch.setattr(fis.api, "SessionLocal", factory)
    app = create_app(
        killer=FakeKiller(),
        universe=UNIVERSE,
        registry=FakeRegistry(),
        reconcile=False,
    )
    with TestClient(app) as c:
        yield c


def _claims(instance_id: str = INSTANCE_A, run: str = RUN) -> str:
    return json.dumps(
        FisConnectClaims(
            alias=SERVICE_ALIAS, instance_id=instance_id, run=run
        ).to_dict()
    )


def _service_row() -> PrincipalSql:
    return PrincipalSql(
        id=SERVICE_ID, kind=PrincipalKind.Service, status=PrincipalStatus.Active
    )


def test_auth_user_malformed_claims_is_deny(client: TestClient) -> None:
    response = client.post(
        "/auth/user", data={"username": SERVICE_ID, "claims": "not json"}
    )
    assert response.status_code == 200
    assert response.text == "deny"


def test_auth_user_without_username_is_deny(client: TestClient) -> None:
    response = client.post("/auth/user", data={"claims": _claims()})
    assert response.text == "deny"


def test_auth_user_allows_a_service_and_records_the_event(
    client: TestClient, session: Session
) -> None:
    session.add(_service_row())
    session.commit()

    response = client.post(
        "/auth/user", data={"username": SERVICE_ID, "claims": _claims()}
    )

    # The verdict carries the run: the broker makes it the connection's user
    # tag and forwards it on the vhost call.
    assert response.text == f"allow {RUN}"
    events = session.scalars(select(AuthEventSql)).all()
    assert len(events) == 1
    assert events[0].principal_id == SERVICE_ID
    assert events[0].instance_id == INSTANCE_A
    assert events[0].decision == FisAuthorizationDecision.Authorized


def test_auth_user_unknown_principal_is_denied_and_recorded(
    client: TestClient, session: Session
) -> None:
    response = client.post(
        "/auth/user", data={"username": SERVICE_ID, "claims": _claims()}
    )
    assert response.text == "deny"
    events = session.scalars(select(AuthEventSql)).all()
    assert [e.decision for e in events] == [FisAuthorizationDecision.Denied]


def test_auth_vhost_compares_the_tag_to_the_vhost(client: TestClient) -> None:
    allowed = client.post(
        "/auth/vhost", data={"username": SERVICE_ID, "vhost": RUN, "tags": RUN}
    )
    denied = client.post(
        "/auth/vhost", data={"username": SERVICE_ID, "vhost": "hw1__2", "tags": RUN}
    )
    assert allowed.text == "allow"
    assert denied.text == "deny"


def test_auth_resource_allows(client: TestClient) -> None:
    response = client.post(
        "/auth/resource",
        data={
            "username": SERVICE_ID,
            "vhost": RUN,
            "resource": "queue",
            "name": "q",
            "permission": "configure",
        },
    )
    assert response.text == "allow"


def test_auth_topic_reads_allow_and_an_unparseable_write_key_denies(
    client: TestClient,
) -> None:
    read = client.post(
        "/auth/topic",
        data={"username": SERVICE_ID, "permission": "read", "routing_key": "anything"},
    )
    write = client.post(
        "/auth/topic",
        data={"username": SERVICE_ID, "permission": "write", "routing_key": "bad"},
    )
    assert read.text == "allow"
    assert write.text == "deny"
