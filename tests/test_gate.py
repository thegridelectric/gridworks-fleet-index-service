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
    GateResult,
    MalformedRequest,
    UserAuthRequest,
    decide_resource,
    decide_topic,
    decide_user,
    decide_vhost,
    parse_user_request,
    user_response,
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
BEECH_LRH = "hw1-isone-me-versant-keene-beech-scada"
SERVICE_ID = "5e971ce0-0000-4000-8000-000000000001"


class FakeKiller:
    """Records kill calls and returns a fixed confirmation outcome."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[str, str]] = []

    def kill(self, *, principal_id: str, vhost: str) -> bool:
        self.calls.append((principal_id, vhost))
        return self.ok

    def kill_identity(self, *, principal_id: str) -> int:
        return 0


class FakeRegistry:
    """Answers `get_by_id` from a fixed map and records the lookups."""

    def __init__(self, known: dict[str, GNodeGt] | None = None) -> None:
        self.known = known or {}
        self.lookups: list[str] = []

    def get_forest(self, roots):
        raise AssertionError("the gate never pulls a forest")

    def get_by_id(self, g_node_id: str) -> GNodeGt | None:
        self.lookups.append(g_node_id)
        return self.known.get(g_node_id)


NO_REGISTRY = FakeRegistry()


def _beech_gt() -> GNodeGt:
    return GNodeGt(
        g_node_id=BEECH_ID,
        alias=BEECH_ALIAS,
        base_class=BaseGNodeClass.Logical,
        g_node_class="Scada",
        status=GNodeStatus.Active,
    )


def _principal(status: PrincipalStatus = PrincipalStatus.Active) -> PrincipalSql:
    return PrincipalSql(id=BEECH_ID, kind=PrincipalKind.GNode, status=status)


def _mirror_row() -> GNodeSql:
    return GNodeSql.from_gt(_beech_gt())


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
    result = decide_user(session, _amqp_req(), killer, NO_REGISTRY, universe=UNIVERSE)
    assert result == (Decision.Deny, GateReason.PrincipalNotFound)
    assert killer.calls == []


def test_suspended_principal_denied(session) -> None:
    _seed(session, principal=_principal(PrincipalStatus.Suspended))
    killer = FakeKiller()
    result = decide_user(session, _amqp_req(), killer, NO_REGISTRY, universe=UNIVERSE)
    assert result == (Decision.Deny, GateReason.PrincipalSuspended)
    assert killer.calls == []


def test_run_outside_this_universe_denied(session) -> None:
    _seed(session)
    killer = FakeKiller()
    result = decide_user(
        session, _amqp_req(run="hw1__1"), killer, NO_REGISTRY, universe="d1"
    )
    assert result == (Decision.Deny, GateReason.RunOutsideUniverse)


def test_first_connect_allowed_and_leases(session) -> None:
    _seed(session)
    killer = FakeKiller(ok=True)
    result = decide_user(session, _amqp_req(), killer, NO_REGISTRY, universe=UNIVERSE)
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
    result = decide_user(
        session, _amqp_req(INSTANCE_A), killer, NO_REGISTRY, universe=UNIVERSE
    )
    assert result == (Decision.Allow, GateReason.LeaseMatch)
    assert killer.calls == []  # a matching lease never kills


def test_revoked_instance_denied_forever(session) -> None:
    _seed(session)
    session.add(_lease(INSTANCE_A, GNodeInstanceStatus.Revoked))
    session.commit()

    killer = FakeKiller()
    result = decide_user(
        session, _amqp_req(INSTANCE_A), killer, NO_REGISTRY, universe=UNIVERSE
    )
    assert result == (Decision.Deny, GateReason.RevokedForever)
    assert killer.calls == []


def test_alias_claim_mismatch_denied(session) -> None:
    _seed(session)
    killer = FakeKiller()
    result = decide_user(
        session,
        _amqp_req(alias="hw1.isone.me.versant.keene.elm.scada"),
        killer,
        NO_REGISTRY,
        universe=UNIVERSE,
    )
    assert result == (Decision.Deny, GateReason.AliasMismatch)
    assert killer.calls == []


def test_class_claim_mismatch_denied(session) -> None:
    _seed(session)
    killer = FakeKiller()
    result = decide_user(
        session,
        _amqp_req(g_node_class="AtomicTNode"),
        killer,
        NO_REGISTRY,
        universe=UNIVERSE,
    )
    assert result == (Decision.Deny, GateReason.ClassMismatch)


def test_amqp_first_connect_requires_registry_mirror(session) -> None:
    _seed(session, mirror=False)  # principal exists, registry mirror does not
    killer = FakeKiller()
    result = decide_user(session, _amqp_req(), killer, NO_REGISTRY, universe=UNIVERSE)
    assert result == (Decision.Deny, GateReason.NotInRegistry)


def test_amqp_first_connect_reads_through_on_mirror_miss(session) -> None:
    # Freshly provisioned: known to the registry, not yet in the mirror.
    _seed(session, mirror=False)
    killer = FakeKiller(ok=True)
    registry = FakeRegistry({BEECH_ID: _beech_gt()})
    result = decide_user(session, _amqp_req(), killer, registry, universe=UNIVERSE)
    assert result.decision is Decision.Allow
    assert result.reason is GateReason.Superseded
    assert registry.lookups == [BEECH_ID]
    assert session.get(GNodeSql, BEECH_ID).alias == BEECH_ALIAS


def test_read_through_still_checks_the_claim(session) -> None:
    _seed(session, mirror=False)
    killer = FakeKiller(ok=True)
    registry = FakeRegistry({BEECH_ID: _beech_gt()})
    result = decide_user(
        session,
        _amqp_req(alias="hw1.isone.me.versant.keene.birch.scada"),
        killer,
        registry,
        universe=UNIVERSE,
    )
    assert result.decision is Decision.Deny
    assert result.reason is GateReason.AliasMismatch
    assert killer.calls == []


def test_mirror_hit_never_asks_the_registry(session) -> None:
    _seed(session)
    killer = FakeKiller(ok=True)
    registry = FakeRegistry()
    result = decide_user(session, _amqp_req(), killer, registry, universe=UNIVERSE)
    assert result.decision is Decision.Allow
    assert registry.lookups == []


def test_supersession_kills_before_admitting(session) -> None:
    _seed(session)
    session.add(_lease(INSTANCE_A, GNodeInstanceStatus.Active))
    session.commit()

    killer = FakeKiller(ok=True)
    result = decide_user(
        session, _amqp_req(INSTANCE_B), killer, NO_REGISTRY, universe=UNIVERSE
    )
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
    result = decide_user(
        session, _amqp_req(INSTANCE_B), killer, NO_REGISTRY, universe=UNIVERSE
    )
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
    result = decide_user(session, req, killer, NO_REGISTRY, universe=UNIVERSE)
    assert result.decision is Decision.Allow
    lease = session.get(LeaseSql, INSTANCE_A)
    assert lease.transport is GNodeInstanceTransport.RabbitMqtt


# --- /auth/vhost -----------------------------------------------------------


def test_user_allow_carries_run_as_tag() -> None:
    req = UserAuthRequest(
        principal_id=BEECH_ID,
        transport=GNodeInstanceTransport.RabbitAmqp,
        instance_id=INSTANCE_A,
        run=RUN,
        alias=BEECH_ALIAS,
        g_node_class="Scada",
    )
    allow = GateResult(Decision.Allow, GateReason.LeaseMatch)
    deny = GateResult(Decision.Deny, GateReason.RevokedForever)
    assert user_response(allow, req) == f"allow {RUN}"
    assert user_response(deny, req) == "deny"


def test_vhost_tag_matches_vhost_allowed() -> None:
    result = decide_vhost(tags=RUN, vhost=RUN)
    assert result == (Decision.Allow, GateReason.VhostRunMatch)


def test_vhost_claimed_run_ne_vhost_denied() -> None:
    # The connection claimed hw1__1 at /auth/user (its tag) and opens hw1__2.
    # No lease is consulted, so an identity already live on hw1__2 changes
    # nothing (dev-battery Finding B).
    result = decide_vhost(tags=RUN, vhost="hw1__2")
    assert result == (Decision.Deny, GateReason.VhostRunMismatch)


def test_vhost_no_run_tag_denied() -> None:
    # A connection without exactly one tag, the run, did not pass this gate.
    assert decide_vhost(tags="", vhost=RUN).decision is Decision.Deny
    assert (
        decide_vhost(tags=f"{RUN} administrator", vhost=RUN).decision is Decision.Deny
    )


# --- /auth/resource --------------------------------------------------------


def test_resource_allow_all() -> None:
    assert decide_resource() == (Decision.Allow, GateReason.ResourceAllowed)


# --- /auth/topic -----------------------------------------------------------


def _rj_key(from_alias_lrh: str) -> str:
    return f"rj.{from_alias_lrh}.scada.gt.sh.status.a.hw1-mm"


def test_topic_read_always_allowed(session) -> None:
    # No mirror, no principal — a read is about visibility, not authority.
    result = decide_topic(
        session, username=BEECH_ID, permission="read", routing_key=_rj_key("anything")
    )
    assert result == (Decision.Allow, GateReason.TopicRead)


def test_topic_write_alias_match_allowed(session) -> None:
    _seed(session)
    result = decide_topic(
        session, username=BEECH_ID, permission="write", routing_key=_rj_key(BEECH_LRH)
    )
    assert result == (Decision.Allow, GateReason.TopicWriteAliasMatch)


def test_topic_write_alias_mismatch_denied(session) -> None:
    _seed(session)
    result = decide_topic(
        session,
        username=BEECH_ID,
        permission="write",
        routing_key=_rj_key("hw1-isone-me-versant-keene-elm-scada"),
    )
    assert result == (Decision.Deny, GateReason.TopicWriteAliasMismatch)


def test_topic_write_gw_grammar_matches_segment_two(session) -> None:
    # gw grammar: category.from_alias.to.to_class.type — from-alias still at
    # token 1.
    _seed(session)
    rk = f"gw.{BEECH_LRH}.to.scada.gt.sh.status"
    result = decide_topic(
        session, username=BEECH_ID, permission="write", routing_key=rk
    )
    assert result == (Decision.Allow, GateReason.TopicWriteAliasMatch)


def test_topic_write_mqtt_slashes_normalized(session) -> None:
    _seed(session)
    rk = f"rj/{BEECH_LRH}/scada/gt/sh/status/a/hw1-mm"
    result = decide_topic(
        session, username=BEECH_ID, permission="write", routing_key=rk
    )
    assert result == (Decision.Allow, GateReason.TopicWriteAliasMatch)


def test_topic_write_malformed_key_denied(session) -> None:
    _seed(session)
    result = decide_topic(
        session, username=BEECH_ID, permission="write", routing_key="rj"
    )
    assert result == (Decision.Deny, GateReason.TopicMalformed)


def test_topic_write_service_principal_allowed(session) -> None:
    # A service principal (no registry alias) is allowed to write in v1.
    session.add(
        PrincipalSql(
            id=SERVICE_ID, kind=PrincipalKind.Service, status=PrincipalStatus.Active
        )
    )
    session.commit()
    result = decide_topic(
        session,
        username=SERVICE_ID,
        permission="write",
        routing_key=_rj_key("hw1-weather"),
    )
    assert result == (Decision.Allow, GateReason.TopicWriteServiceAllowed)


def test_topic_write_unknown_identity_denied(session) -> None:
    # Neither a GNode mirror row nor a principal — deny.
    result = decide_topic(
        session,
        username=SERVICE_ID,
        permission="write",
        routing_key=_rj_key("whatever"),
    )
    assert result == (Decision.Deny, GateReason.TopicWriteNoIdentity)


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
    # The principal is named and the shape says AMQP: both are kept for the
    # record; the claim that did not decode is not.
    assert parse_user_request(
        {"username": BEECH_ID, "claims": "not json"}
    ) == MalformedRequest(
        principal_id=BEECH_ID, transport=GNodeInstanceTransport.RabbitAmqp
    )


def test_parse_mqtt_bad_client_id_is_malformed() -> None:
    params = {"username": BEECH_ID, "client_id": "not-a-uuid", "vhost": RUN}
    assert parse_user_request(params) == MalformedRequest(
        principal_id=BEECH_ID, transport=GNodeInstanceTransport.RabbitMqtt
    )


def test_service_principal_skips_registry_check(session) -> None:
    """A service is not a GNode: its AMQP claims carry no class and there is
    no registry row to match, so the alias/class check does not run and the
    connect is admitted on the lease path alone."""
    service_id = "5f1c2d3e-4a5b-4c6d-8e7f-9a0b1c2d3e4f"
    session.add(
        PrincipalSql(
            id=service_id, kind=PrincipalKind.Service, status=PrincipalStatus.Active
        )
    )
    session.commit()
    killer = FakeKiller(ok=True)
    req = UserAuthRequest(
        principal_id=service_id,
        transport=GNodeInstanceTransport.RabbitAmqp,
        instance_id=INSTANCE_A,
        run=RUN,
        alias="d1.some.service",
        g_node_class=None,
    )
    result = decide_user(session, req, killer, NO_REGISTRY, universe=UNIVERSE)
    assert result.decision is Decision.Allow
    assert result.reason is GateReason.Superseded
    assert killer.calls == [(service_id, RUN)]
