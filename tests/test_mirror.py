"""The mirror apply + reconvergence kill, against a real Postgres.

An alias change on an already-mirrored identity is the one update the
`/auth/topic` verdict cache cannot see on its own, so it must flush that
identity's connections. Everything else (insert, no-op, a non-alias field
change) touches no connection.
"""

from __future__ import annotations

import pytest

from fis.db.models import GNodeSql
from fis.mirror import MirrorApply, apply_forest, apply_gnode, reconcile_once
from fis.sema.enums import BaseGNodeClass, GNodeStatus
from fis.sema.types import GNodeForest, GNodeGt

pytestmark = pytest.mark.integration

BEECH_ID = "19ee09df-80ba-437b-b6c1-1eebe9d34801"
BEECH_ALIAS = "hw1.isone.me.versant.keene.beech.scada"
BEECH_RENAMED = "hw1.isone.me.versant.keene.birch.scada"
KEENE_ID = "dcb05390-5bca-40ef-b63d-7908ccb33d9b"
KEENE_ALIAS = "hw1.isone.me.versant.keene"


class FakeKiller:
    def __init__(self, killed: int = 1) -> None:
        self.killed = killed
        self.identity_calls: list[str] = []

    def kill(self, *, principal_id: str, vhost: str) -> bool:
        return True

    def kill_identity(self, *, principal_id: str) -> int:
        self.identity_calls.append(principal_id)
        return self.killed


def _gt(alias: str = BEECH_ALIAS, **overrides) -> GNodeGt:
    fields = {
        "g_node_id": BEECH_ID,
        "alias": alias,
        "base_class": BaseGNodeClass.Logical,
        "g_node_class": "Scada",
        "status": GNodeStatus.Active,
    }
    fields.update(overrides)
    return GNodeGt(**fields)


def test_apply_inserts_new_gnode(session) -> None:
    killer = FakeKiller()
    result = apply_gnode(session, _gt(), killer)
    assert result is MirrorApply.Inserted
    assert session.get(GNodeSql, BEECH_ID).alias == BEECH_ALIAS
    assert killer.identity_calls == []


def test_apply_same_snapshot_is_unchanged(session) -> None:
    killer = FakeKiller()
    apply_gnode(session, _gt(), killer)
    result = apply_gnode(session, _gt(), killer)
    assert result is MirrorApply.Unchanged
    assert killer.identity_calls == []


def test_apply_non_alias_change_updates_without_killing(session) -> None:
    killer = FakeKiller()
    apply_gnode(session, _gt(), killer)
    result = apply_gnode(session, _gt(display_name="Beech Scada"), killer)
    assert result is MirrorApply.Updated
    assert session.get(GNodeSql, BEECH_ID).display_name == "Beech Scada"
    assert killer.identity_calls == []


def test_apply_rename_flushes_the_identity(session) -> None:
    killer = FakeKiller()
    apply_gnode(session, _gt(), killer)
    result = apply_gnode(
        session, _gt(alias=BEECH_RENAMED, prev_alias=BEECH_ALIAS), killer
    )
    assert result is MirrorApply.Renamed
    assert killer.identity_calls == [BEECH_ID]
    assert session.get(GNodeSql, BEECH_ID).alias == BEECH_RENAMED


def test_apply_rename_kill_is_best_effort(session) -> None:
    # Nothing to close (or the management API failing) is not fatal — the
    # rename still lands.
    killer = FakeKiller(killed=0)
    apply_gnode(session, _gt(), killer)
    result = apply_gnode(session, _gt(alias=BEECH_RENAMED), killer)
    assert result is MirrorApply.Renamed
    assert session.get(GNodeSql, BEECH_ID).alias == BEECH_RENAMED


def _keene_gt() -> GNodeGt:
    return GNodeGt(
        g_node_id=KEENE_ID,
        alias=KEENE_ALIAS,
        base_class=BaseGNodeClass.MarketMaker,
        g_node_class="MarketMaker",
        status=GNodeStatus.Active,
        position_point_id="c1b8f2e4-9f9b-4a2c-8f0e-2a4d9b6a1c3e",
    )


def _forest(*nodes: GNodeGt) -> GNodeForest:
    return GNodeForest(
        roots=["hw1"], nodes=list(nodes), edges=[], send_time_ms=1762634100033
    )


class FakeRegistry:
    def __init__(self, forest: GNodeForest | None) -> None:
        self.forest = forest
        self.requested: list[list[str]] = []

    def get_forest(self, roots):
        self.requested.append(list(roots))
        return self.forest

    def get_by_id(self, g_node_id):
        raise AssertionError("reconcile never reads through")


def test_apply_forest_counts_each_outcome(session) -> None:
    killer = FakeKiller()
    apply_gnode(session, _gt(), killer)
    result = apply_forest(
        session, _forest(_keene_gt(), _gt(alias=BEECH_RENAMED)), killer
    )
    assert (result.inserted, result.updated, result.renamed, result.unchanged) == (
        1,
        0,
        1,
        0,
    )
    assert result.missing == []
    assert killer.identity_calls == [BEECH_ID]


def test_apply_forest_reports_missing_without_touching_them(session) -> None:
    killer = FakeKiller()
    apply_gnode(session, _gt(), killer)
    result = apply_forest(session, _forest(_keene_gt()), killer)
    assert result.missing == [BEECH_ID]
    beech = session.get(GNodeSql, BEECH_ID)
    assert beech.alias == BEECH_ALIAS
    assert beech.status == GNodeStatus.Active


def test_reconcile_once_pulls_the_universe(session, engine) -> None:
    from sqlalchemy.orm import Session, sessionmaker

    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    registry = FakeRegistry(_forest(_keene_gt(), _gt()))
    result = reconcile_once(factory, registry, FakeKiller(), universe="hw1")
    assert registry.requested == [["hw1"]]
    assert result is not None and result.inserted == 2
    assert session.get(GNodeSql, KEENE_ID).alias == KEENE_ALIAS


def test_reconcile_once_keeps_the_mirror_when_gnr_is_down(session, engine) -> None:
    from sqlalchemy.orm import Session, sessionmaker

    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    apply_gnode(session, _gt(), FakeKiller())
    result = reconcile_once(factory, FakeRegistry(None), FakeKiller(), universe="hw1")
    assert result is None
    assert session.get(GNodeSql, BEECH_ID).alias == BEECH_ALIAS
