"""The schema, against a real Postgres.

Two things are worth proving here: the GT ↔ SQL bijection holds in both
directions, and the single-writer invariant is enforced by the database
rather than only by the gate's care.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from fis.db.models import (
    GNodeSql,
    LeaseSql,
    PrincipalKind,
    PrincipalSql,
    PrincipalStatus,
)
from fis.sema.enums import (
    BaseGNodeClass,
    GNodeInstanceStatus,
    GNodeInstanceTransport,
    GNodeStatus,
)
from fis.sema.types import GNodeGt, GNodeInstanceGt

pytestmark = pytest.mark.integration

BEECH_ID = "19ee09df-80ba-437b-b6c1-1eebe9d34801"
NODE_ID = "9cff2689-eadc-4577-94ea-6d86d0d23e9e"


def _lease(instance_id: str, run: str, status: GNodeInstanceStatus) -> GNodeInstanceGt:
    return GNodeInstanceGt(
        g_node_id=BEECH_ID,
        g_node_instance_id=instance_id,
        run=run,
        status=status,
        transport=GNodeInstanceTransport.RabbitAmqp,
        connected_at_unix_ms=1762634100033,
        revoked_at_unix_ms=(
            None if status == GNodeInstanceStatus.Active else 1762634900000
        ),
    )


def _principal() -> PrincipalSql:
    return PrincipalSql(
        id=BEECH_ID,
        kind=PrincipalKind.GNode,
        status=PrincipalStatus.Active,
    )


def test_g_node_mirror_round_trips(session) -> None:
    gt = GNodeGt(
        g_node_id=NODE_ID,
        alias="hw1.isone.me.versant.keene.beech.scada",
        base_class=BaseGNodeClass.Logical,
        g_node_class="Scada",
        status=GNodeStatus.Active,
        position_point_id="a3f1c8de-2b47-4e9a-9d61-7c0e5b842f10",
    )
    session.add(GNodeSql.from_gt(gt))
    session.commit()

    row = session.get(GNodeSql, NODE_ID)
    assert row.to_gt() == gt


def test_lease_round_trips(session) -> None:
    gt = _lease(BEECH_ID, "hw1__1", GNodeInstanceStatus.Active)
    session.add(_principal())
    session.add(LeaseSql.from_gt(gt))
    session.commit()

    row = session.get(LeaseSql, BEECH_ID)
    assert row.to_gt() == gt


def test_second_active_lease_on_same_run_is_rejected(session) -> None:
    """Invariant 1 — single writer per (principal, run)."""
    session.add(_principal())
    session.add(
        LeaseSql.from_gt(_lease(BEECH_ID, "hw1__1", GNodeInstanceStatus.Active))
    )
    session.commit()

    session.add(
        LeaseSql.from_gt(
            _lease(
                "c0ffee00-1111-4222-8333-444455556666",
                "hw1__1",
                GNodeInstanceStatus.Active,
            )
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_same_identity_holds_active_leases_on_different_runs(session) -> None:
    """Leases are run-scoped: prod and staging coexist for one identity."""
    session.add(_principal())
    session.add(
        LeaseSql.from_gt(_lease(BEECH_ID, "hw1__1", GNodeInstanceStatus.Active))
    )
    session.add(
        LeaseSql.from_gt(
            _lease(
                "c0ffee00-1111-4222-8333-444455556666",
                "hw1__2",
                GNodeInstanceStatus.Active,
            )
        )
    )
    session.commit()

    assert session.query(LeaseSql).count() == 2


def test_revoked_lease_does_not_block_its_successor(session) -> None:
    """Supersession: the predecessor's row stays forever and the successor
    still takes the run."""
    session.add(_principal())
    session.add(
        LeaseSql.from_gt(_lease(BEECH_ID, "hw1__1", GNodeInstanceStatus.Revoked))
    )
    session.add(
        LeaseSql.from_gt(
            _lease(
                "c0ffee00-1111-4222-8333-444455556666",
                "hw1__1",
                GNodeInstanceStatus.Active,
            )
        )
    )
    session.commit()

    assert session.query(LeaseSql).count() == 2
