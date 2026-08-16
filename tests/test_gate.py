"""The gate, against a real Postgres.

These are the dev-battery verdicts: every allow/deny path plus ordered
supersession, driven by a fake connection-killer so the decision logic is
proven without a broker. The staging run (a real broker, a real kill) is the
verification that counts; this catches the mechanical failures cheaply first.
"""

from __future__ import annotations

import json

import pytest

from fis.db.models import (
    GNodeSql,
    LeaseSql,
    PrincipalKind,
    PrincipalSql,
    PrincipalStatus,
)
from fis.gate import (
    Decision,
    GateReason,
    UserAuthRequest,
    decide_user,
    parse_user_request,
)
from fis.sema.enums import (
    BaseGNodeClass,
    GNodeInstanceStatus,
    GNodeInstanceTransport,
    GNodeStatus,
)
from fis.sema.types import FisConnectClaims, GNodeGt, GNodeInstanceGt

pytestmark = pytest.mark.integration

UNIVERSE = "hw1"
RUN = "hw1__1"
BEECH_ID = "19ee09df-80ba-437b-b6c1-1eebe9d34801"
BEECH_ALIAS = "hw1.isone.me.versant.keene.beech.scada"
INSTANCE_A = "aaaaaaaa-1111-4aaa-8aaa-aaaaaaaaaaaa"
INSTANCE_B = "bbbbbbbb-2222-4bbb-8bbb-bbbbbbbbbbbb"


class FakeKiller:
    """Records kill calls and returns a fixed confirmation outcome."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[str, str]] = []

    def kill(self, *, principal_id: str, vhost: str) -> bool:
        self.calls.append((principal_id, vhost))
        return self.ok


def _principal(status: PrincipalStatus = PrincipalStatus.Active) -> PrincipalSql:
    return PrincipalSql(id=BEECH_ID, kind=PrincipalKind.GNode, status=status)


def _mirror_row() -> GNodeSql:
    return GNodeSql.from_gt(
        GNodeGt(
            g_node_id=BEECH_ID,
            alias=BEECH_ALIAS,
            base_class=BaseGNodeClass.Logical,
            g_node_class="Scada",
            status=GNodeStatus.Active,
        )
    )


def _lease(instance_id: str, status: GNodeInstanceStatus) -> LeaseSql:
    return LeaseSql.from_gt(
        GNodeInstanceGt(
            g_node_id=BEECH_ID,
            g_node_instance_id=instance_id,
            run=RUN,
            status=status,
            transport=GNodeInstanceTransport.RabbitAmqp,
            connected_at_unix_ms=1762634100033,
            revoked_at_unix_ms=(
                None if status == GNodeInstanceStatus.Active else 1762634900000
            ),
        )
    )


def _amqp_req(
    instance_id: str = INSTANCE_A,
    alias: str = BEECH_ALIAS,
    g_node_class: str = "Scada",
    run: str = RUN,
) -> UserAuthRequest:
    return UserAuthRequest(
        principal_id=BEECH_ID,
        transport=GNodeInstanceTransport.RabbitAmqp,
        instance_id=instance_id,
        run=run,
        alias=alias,
        g_node_class=g_node_class,
    )


def _seed(
    session, *, mirror: bool = True, principal: PrincipalSql | None = None
) -> None:
    session.add(principal if principal is not None else _principal())
    if mirror:
        session.add(_mirror_row())
    session.commit()


# --- the verdicts ----------------------------------------------------------


def test_unknown_principal_denied(session) -> None:
    killer = FakeKiller()
    result = decide_user(session, _amqp_req(), killer, universe=UNIVERSE)
    assert result == (Decision.Deny, GateReason.PrincipalNotFound)
    assert killer.calls == []


def test_suspended_principal_denied(session) -> None:
    _seed(session, principal=_principal(PrincipalStatus.Suspended))
    killer = FakeKiller()
    result = decide_user(session, _amqp_req(), killer, universe=UNIVERSE)
    assert result == (Decision.Deny, GateReason.PrincipalSuspended)
    assert killer.calls == []


def test_run_outside_this_universe_denied(session) -> None:
    _seed(session)
    killer = FakeKiller()
    result = decide_user(session, _amqp_req(run="hw1__1"), killer, universe="d1")
    assert result == (Decision.Deny, GateReason.RunOutsideUniverse)


def test_first_connect_allowed_and_leases(session) -> None:
    _seed(session)
    killer = FakeKiller(ok=True)
    result = decide_user(session, _amqp_req(), killer, universe=UNIVERSE)
    assert result.decision is Decision.Allow
    assert result.reason is GateReason.Superseded
    # An empty kill still runs (confirming nothing remains) before admitting.
    assert killer.calls == [(BEECH_ID, RUN)]
    lease = session.get(LeaseSql, INSTANCE_A)
    assert lease is not None
    assert lease.status is GNodeInstanceStatus.Active


def test_reconnect_same_instance_is_idempotent(session) -> None:
    _seed(session)
    session.add(_lease(INSTANCE_A, GNodeInstanceStatus.Active))
    session.commit()

    killer = FakeKiller()
    result = decide_user(session, _amqp_req(INSTANCE_A), killer, universe=UNIVERSE)
    assert result == (Decision.Allow, GateReason.LeaseMatch)
    assert killer.calls == []  # a matching lease never kills


def test_revoked_instance_denied_forever(session) -> None:
    _seed(session)
    session.add(_lease(INSTANCE_A, GNodeInstanceStatus.Revoked))
    session.commit()

    killer = FakeKiller()
    result = decide_user(session, _amqp_req(INSTANCE_A), killer, universe=UNIVERSE)
    assert result == (Decision.Deny, GateReason.RevokedForever)
    assert killer.calls == []


def test_alias_claim_mismatch_denied(session) -> None:
    _seed(session)
    killer = FakeKiller()
    result = decide_user(
        session,
        _amqp_req(alias="hw1.isone.me.versant.keene.elm.scada"),
        killer,
        universe=UNIVERSE,
    )
    assert result == (Decision.Deny, GateReason.AliasMismatch)
    assert killer.calls == []


def test_class_claim_mismatch_denied(session) -> None:
    _seed(session)
    killer = FakeKiller()
    result = decide_user(
        session, _amqp_req(g_node_class="AtomicTNode"), killer, universe=UNIVERSE
    )
    assert result == (Decision.Deny, GateReason.ClassMismatch)


def test_amqp_first_connect_requires_registry_mirror(session) -> None:
    _seed(session, mirror=False)  # principal exists, registry mirror does not
    killer = FakeKiller()
    result = decide_user(session, _amqp_req(), killer, universe=UNIVERSE)
    assert result == (Decision.Deny, GateReason.NotInRegistry)


def test_supersession_kills_before_admitting(session) -> None:
    _seed(session)
    session.add(_lease(INSTANCE_A, GNodeInstanceStatus.Active))
    session.commit()

    killer = FakeKiller(ok=True)
    result = decide_user(session, _amqp_req(INSTANCE_B), killer, universe=UNIVERSE)
    assert result == (Decision.Allow, GateReason.Superseded)
    assert killer.calls == [(BEECH_ID, RUN)]

    predecessor = session.get(LeaseSql, INSTANCE_A)
    successor = session.get(LeaseSql, INSTANCE_B)
    assert predecessor.status is GNodeInstanceStatus.Revoked
    assert predecessor.revoked_at_unix_ms is not None
    assert successor.status is GNodeInstanceStatus.Active


def test_unconfirmed_kill_fails_closed(session) -> None:
    _seed(session)
    session.add(_lease(INSTANCE_A, GNodeInstanceStatus.Active))
    session.commit()

    killer = FakeKiller(ok=False)
    result = decide_user(session, _amqp_req(INSTANCE_B), killer, universe=UNIVERSE)
    assert result == (Decision.Deny, GateReason.KillUnconfirmed)

    # The predecessor keeps its Active lease; the successor was never created.
    session.expire_all()
    predecessor = session.get(LeaseSql, INSTANCE_A)
    assert predecessor.status is GNodeInstanceStatus.Active
    assert session.get(LeaseSql, INSTANCE_B) is None


def test_mqtt_first_connect_allowed_without_alias_check(session) -> None:
    _seed(session, mirror=False)  # MQTT does not check the mirror at connect
    req = UserAuthRequest(
        principal_id=BEECH_ID,
        transport=GNodeInstanceTransport.RabbitMqtt,
        instance_id=INSTANCE_A,
        run=RUN,
        alias=None,
        g_node_class=None,
    )
    killer = FakeKiller(ok=True)
    result = decide_user(session, req, killer, universe=UNIVERSE)
    assert result.decision is Decision.Allow
    lease = session.get(LeaseSql, INSTANCE_A)
    assert lease.transport is GNodeInstanceTransport.RabbitMqtt


# --- request parsing -------------------------------------------------------


def test_parse_amqp_claims() -> None:
    claims = FisConnectClaims(
        alias=BEECH_ALIAS, instance_id=INSTANCE_A, run=RUN, g_node_class="Scada"
    )
    params = {"username": BEECH_ID, "claims": json.dumps(claims.to_dict())}
    req = parse_user_request(params)
    assert req is not None
    assert req.transport is GNodeInstanceTransport.RabbitAmqp
    assert req.instance_id == INSTANCE_A
    assert req.alias == BEECH_ALIAS


def test_parse_mqtt() -> None:
    params = {"username": BEECH_ID, "client_id": INSTANCE_A, "vhost": RUN}
    req = parse_user_request(params)
    assert req is not None
    assert req.transport is GNodeInstanceTransport.RabbitMqtt
    assert req.instance_id == INSTANCE_A
    assert req.run == RUN


def test_parse_missing_username_is_malformed() -> None:
    assert parse_user_request({"client_id": INSTANCE_A, "vhost": RUN}) is None


def test_parse_bad_claims_is_malformed() -> None:
    assert parse_user_request({"username": BEECH_ID, "claims": "not json"}) is None


def test_parse_mqtt_bad_client_id_is_malformed() -> None:
    params = {"username": BEECH_ID, "client_id": "not-a-uuid", "vhost": RUN}
    assert parse_user_request(params) is None
