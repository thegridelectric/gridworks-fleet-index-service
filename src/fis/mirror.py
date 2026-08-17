"""Applying registry updates to FIS's `g_node` mirror.

FIS holds a copy of the registry so that auth survives gnr being down.
Updates arrive as `g.node.gt` snapshots — seeded via the registry read
façade, upserted from `g.node.forest` broadcast deltas, healed by periodic
snapshot broadcasts. This module is the **apply** step: it writes a snapshot
into the mirror and, when the write is a rename, triggers the reconvergence
kill (build step 5).

The transport that *delivers* those snapshots (the gwbase forest subscriber)
is a separate concern; keeping the apply here — a function over a session, a
validated `GNodeGt`, and an injected `ConnectionKiller` — makes it
unit-testable without a broker, exactly like the gate.

Why a rename forces a kill: `/auth/topic` write verdicts cache per
(connection, exchange, routing key), so a mid-connection alias change is
invisible to a live connection's cached keys. Killing the identity's
connections flushes that cache; the reconnect re-runs every check against the
new alias. Convergence is immediate (by forced reconnect), not "eventually at
next redeploy".
"""

from __future__ import annotations

import enum
import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from fis.db.models import GNodeSql
from fis.rabbit_admin import ConnectionKiller
from fis.sema.types import GNodeGt

logger = logging.getLogger(__name__)


class MirrorApply(enum.StrEnum):
    """What applying a snapshot did to the mirror."""

    Inserted = "inserted"
    Unchanged = "unchanged"
    Updated = "updated"
    Renamed = "renamed"


def apply_gnode(session: Session, gt: GNodeGt, killer: ConnectionKiller) -> MirrorApply:
    """Upsert one GNode snapshot into the mirror; on a rename, flush the
    identity's connections so its cached topic verdicts re-check.

    A rename is an alias change on an already-mirrored identity — the one
    mirror update the topic-verdict cache cannot see on its own.
    """
    existing = session.get(GNodeSql, gt.g_node_id)
    if existing is None:
        session.add(GNodeSql.from_gt(gt))
        session.commit()
        return MirrorApply.Inserted

    if existing.to_gt() == gt:
        return MirrorApply.Unchanged

    old_alias = existing.alias
    renamed = old_alias != gt.alias

    fresh = GNodeSql.from_gt(gt)
    existing.alias = fresh.alias
    existing.prev_alias = fresh.prev_alias
    existing.base_class = fresh.base_class
    existing.g_node_class = fresh.g_node_class
    existing.status = fresh.status
    existing.position_point_id = fresh.position_point_id
    existing.display_name = fresh.display_name
    existing.mirrored_at = datetime.now(UTC)
    session.commit()

    if not renamed:
        return MirrorApply.Updated

    killed = killer.kill_identity(principal_id=gt.g_node_id)
    logger.info(
        "mirror rename %s: %s -> %s, flushed %d connection(s)",
        gt.g_node_id,
        old_alias,
        gt.alias,
        killed,
    )
    return MirrorApply.Renamed
