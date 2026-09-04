"""Applying registry updates to FIS's `g_node` mirror.

FIS holds a copy of the registry so that auth survives gnr being down.
Updates arrive as `g.node.gt` snapshots over gnr's HTTP read façade: a
whole-universe `g.node.forest` pulled at boot and on an interval
(`reconcile_once`), and a single node read through by the gate on a mirror
miss. This module is the **apply** step: it writes a snapshot into the
mirror and, when the write is a rename, triggers the reconvergence kill
(build step 5); `apply_forest` loops it over a pulled forest.

The client that *fetches* those snapshots is `gnr_client`; keeping the
apply here — a function over a session, a validated `GNodeGt`, and an
injected `ConnectionKiller` — makes it unit-testable without a registry,
exactly like the gate.

Nothing is retired by absence. A registry node never vanishes (its id is
immutable, its status terminal, and there is no delete command), so a
mirrored id missing from a whole-universe pull is a registry anomaly to log,
not a status FIS may invent in a mirror bijective with `g.node.gt`. Absence
grants no authority either way: the gate needs a live alias/class match.

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
from collections.abc import Callable
from datetime import UTC, datetime
from typing import NamedTuple

from sqlalchemy.orm import Session

from fis.db.models import GNodeSql
from fis.gnr_client import RegistryReader
from fis.rabbit_admin import ConnectionKiller
from fis.sema.property_format import LeftRightDot, UUID4Str
from fis.sema.types import GNodeForest, GNodeGt

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


class ForestApply(NamedTuple):
    """What applying a pulled forest did to the mirror.

    FIS-internal bookkeeping for the reconcile log line; no sema word covers
    it and none is wanted — it never crosses a boundary.
    """

    inserted: int
    updated: int
    renamed: int
    unchanged: int
    missing: list[UUID4Str]  # mirrored ids the forest did not carry


def apply_forest(
    session: Session, forest: GNodeForest, killer: ConnectionKiller
) -> ForestApply:
    """Apply every node of a pulled forest, then report which mirrored ids
    the forest did not carry. The forest is the served universe, so a
    missing id is a registry anomaly — logged, never acted on."""
    counts = dict.fromkeys(MirrorApply, 0)
    for gt in forest.nodes:
        counts[apply_gnode(session, gt, killer)] += 1

    carried = {gt.g_node_id for gt in forest.nodes}
    missing = [
        row.id
        for row in session.query(GNodeSql).order_by(GNodeSql.alias).all()
        if row.id not in carried
    ]
    if missing:
        logger.warning(
            "mirror holds %d GNode(s) the registry forest for %s did not carry: %s",
            len(missing),
            forest.roots,
            missing,
        )
    return ForestApply(
        inserted=counts[MirrorApply.Inserted],
        updated=counts[MirrorApply.Updated],
        renamed=counts[MirrorApply.Renamed],
        unchanged=counts[MirrorApply.Unchanged],
        missing=missing,
    )


def reconcile_once(
    session_factory: Callable[[], Session],
    registry: RegistryReader,
    killer: ConnectionKiller,
    *,
    universe: LeftRightDot,
) -> ForestApply | None:
    """One pull-and-apply of the served universe's forest. Returns `None`
    when the registry could not be reached: the last-known mirror serves."""
    forest = registry.get_forest([universe])
    if forest is None:
        logger.warning("reconcile skipped: registry unreachable for %s", universe)
        return None
    with session_factory() as session:
        result = apply_forest(session, forest, killer)
    logger.info(
        "reconcile %s: %d node(s) — inserted=%d updated=%d renamed=%d unchanged=%d missing=%d",
        universe,
        len(forest.nodes),
        result.inserted,
        result.updated,
        result.renamed,
        result.unchanged,
        len(result.missing),
    )
    return result
