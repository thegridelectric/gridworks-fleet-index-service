"""The auth-event record, against a real Postgres: every user-gate reason
maps, the projection axiom guards the mapping, and a row round-trips."""

from __future__ import annotations

import pytest

from fis.auth_event import USER_REASONS, build_auth_event, record_auth_event
from fis.db.models import AuthEventSql
from fis.gate import (
    Decision,
    GateReason,
    GateResult,
    MalformedRequest,
    UserAuthRequest,
)
from fis.sema.enums import (
    FisAuthorizationDecision,
    FisAuthorizationReason,
    GNodeInstanceTransport,
)

BEECH_ID = "19ee09df-80ba-437b-b6c1-1eebe9d34801"
INSTANCE_A = "aaaaaaaa-1111-4aaa-8aaa-aaaaaaaaaaaa"

REQ = UserAuthRequest(
    principal_id=BEECH_ID,
    transport=GNodeInstanceTransport.RabbitAmqp,
    instance_id=INSTANCE_A,
    run="hw1__1",
    alias="hw1.isone.me.versant.keene.beech.scada",
    g_node_class="Scada",
)

USER_GATE_REASONS = {
    r for r in GateReason if not r.value.startswith(("vhost", "resource", "topic"))
}


def test_every_user_gate_reason_maps_and_nothing_else() -> None:
    assert set(USER_REASONS) == USER_GATE_REASONS
    assert set(USER_REASONS.values()) == set(FisAuthorizationReason)


def test_build_event_carries_the_verdict() -> None:
    event = build_auth_event(
        REQ,
        GateResult(Decision.Allow, GateReason.Superseded),
        decided_at_unix_ms=1762634100033,
    )
    assert event.decision is FisAuthorizationDecision.Authorized
    assert event.reason is FisAuthorizationReason.Superseded
    assert event.principal_id == BEECH_ID and event.run == "hw1__1"


def test_build_event_for_a_malformed_request_carries_what_was_forwarded() -> None:
    event = build_auth_event(
        MalformedRequest(
            principal_id=BEECH_ID, transport=GNodeInstanceTransport.RabbitMqtt
        ),
        GateResult(Decision.Deny, GateReason.Malformed),
        decided_at_unix_ms=1762634100033,
    )
    assert event.reason is FisAuthorizationReason.MalformedRequest
    assert event.decision is FisAuthorizationDecision.Denied
    assert event.principal_id == BEECH_ID
    assert event.transport is GNodeInstanceTransport.RabbitMqtt
    assert event.instance_id is None and event.run is None
    assert event.alias is None and event.g_node_class is None


def test_projection_axiom_refuses_a_drifted_verdict() -> None:
    # A gate result whose decision does not follow from its reason cannot be
    # recorded: the word's axiom fires before any row is written.
    with pytest.raises(ValueError, match="(?i)axiom 1"):
        build_auth_event(
            REQ,
            GateResult(Decision.Allow, GateReason.RevokedForever),
            decided_at_unix_ms=1762634100033,
        )


@pytest.mark.integration
def test_record_round_trips(session) -> None:
    row = record_auth_event(
        session, REQ, GateResult(Decision.Deny, GateReason.LeaseRace)
    )
    back = session.get(AuthEventSql, row.event_id)
    gt = back.to_gt()
    assert gt.reason is FisAuthorizationReason.LeaseRace
    assert gt.decision is FisAuthorizationDecision.Denied
    assert AuthEventSql.from_gt(gt).to_gt() == gt
