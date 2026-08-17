"""The mirror apply + reconvergence kill, against a real Postgres.

An alias change on an already-mirrored identity is the one update the
`/auth/topic` verdict cache cannot see on its own, so it must flush that
identity's connections. Everything else (insert, no-op, a non-alias field
change) touches no connection.
"""

from __future__ import annotations

import pytest

from fis.db.models import GNodeSql
from fis.mirror import MirrorApply, apply_gnode
from fis.sema.enums import BaseGNodeClass, GNodeStatus
from fis.sema.types import GNodeGt

pytestmark = pytest.mark.integration

BEECH_ID = "19ee09df-80ba-437b-b6c1-1eebe9d34801"
BEECH_ALIAS = "hw1.isone.me.versant.keene.beech.scada"
BEECH_RENAMED = "hw1.isone.me.versant.keene.birch.scada"


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
