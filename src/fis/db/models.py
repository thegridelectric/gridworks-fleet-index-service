"""SQLAlchemy models for the Fleet Index Service.

Three tables, two of which are bijective with a Sema word — every row is a
serialized GT snapshot, validated through the codec before insert or update:

- `g_nodes`  — the registry mirror, bijective with `g.node.gt`
- `leases`   — instance authority, bijective with `g.node.instance.gt/001`
- `principals` — no Sema word exists yet (see the note on the class)

The mirror is a *mirror*: gnr owns this data, FIS holds a copy so that auth
survives gnr being down. Nothing here writes back to the registry.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Index,
    String,
)
from sqlalchemy.orm import (
    Mapped,
    declarative_base,
    mapped_column,
)

from fis.sema.enums import (
    BaseGNodeClass,
    GNodeInstanceStatus,
    GNodeInstanceTransport,
    GNodeStatus,
)
from fis.sema.types import GNodeGt, GNodeInstanceGt

Base = declarative_base()


# ============================================================
#  REGISTRY MIRROR
# ============================================================


class GNodeSql(Base):
    """A GNode as the registry currently describes it — strict bijection with
    `g.node.gt`.

    Fed only through the registry's HTTP read façade: a whole-universe
    forest pulled at boot and on an interval, and a single node read through
    on a gate miss. FIS reads two things from it: whether a claimed
    alias matches the identity connecting, and whether that identity is a
    GNode the registry still knows.

    `position_point_id` is carried to keep the bijection honest but is inert
    here — FIS never resolves it, and unlike the registry there is no
    `position_points` table and therefore no foreign key.
    """

    __tablename__ = "g_nodes"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    alias: Mapped[str] = mapped_column(String, index=True, unique=True)
    prev_alias: Mapped[str | None] = mapped_column(String, nullable=True)

    base_class: Mapped[BaseGNodeClass] = mapped_column(
        Enum(BaseGNodeClass, name="base_g_node_class")
    )
    g_node_class: Mapped[str] = mapped_column(String)
    status: Mapped[GNodeStatus] = mapped_column(Enum(GNodeStatus, name="g_node_status"))

    position_point_id: Mapped[str | None] = mapped_column(String, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)

    mirrored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    def to_gt(self) -> GNodeGt:
        """Serialize SQL row → Sema GT."""
        return GNodeGt(
            g_node_id=self.id,
            alias=self.alias,
            base_class=self.base_class,
            g_node_class=self.g_node_class,
            status=self.status,
            prev_alias=self.prev_alias,
            position_point_id=self.position_point_id,
            display_name=self.display_name,
        )

    @staticmethod
    def from_gt(gt: GNodeGt) -> GNodeSql:
        """Create SQL model from a Sema GT instance (already validated)."""
        return GNodeSql(
            id=gt.g_node_id,
            alias=gt.alias,
            prev_alias=gt.prev_alias,
            base_class=gt.base_class,
            g_node_class=gt.g_node_class,
            status=gt.status,
            position_point_id=gt.position_point_id,
            display_name=gt.display_name,
        )


# ============================================================
#  PRINCIPALS
# ============================================================


class PrincipalKind(enum.StrEnum):
    """Whether a principal is a GNode or a plain service.

    Hand-coded: no Sema word covers the principal model yet. A
    `fis.principal.gt` word retires this enum, the status enum below, and
    `PrincipalSql`'s hand-built rows together.
    """

    GNode = "GNode"
    Service = "Service"


class PrincipalStatus(enum.StrEnum):
    """Whether a principal may connect at all.

    Hand-coded alongside `PrincipalKind` — retired by the same
    `fis.principal.gt` word.
    """

    Active = "Active"
    Suspended = "Suspended"


class PrincipalSql(Base):
    """A durable identity that may hold leases: a core piece of the
    GridWorks platform allowed to connect to the broker — a GNode in the
    grid topology (a scada, a leaf transactive node, a market maker) or a
    Service outside it (the grid-node-registry, the ear, the journalkeeper).

    Keyed on the **cert subject**, which is the identity itself: for a GNode
    the CN is its GNodeId, so the GNodeId *is* the principal id and there is
    no second identifier; for a service the CN is a principal UUID minted
    with this row. One identity rule for both kinds — service principals are
    additive rows, not a code fork.

    Suspension is the emergency-eviction lever, and it is independent of
    lease state: the gate denies a suspended principal regardless of what
    leases exist.

    No Sema word covers this yet. It is hand-built here, and a
    `fis.principal.gt` word retires the hand-building.
    """

    __tablename__ = "principals"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[PrincipalKind] = mapped_column(
        Enum(PrincipalKind, name="principal_kind")
    )
    status: Mapped[PrincipalStatus] = mapped_column(
        Enum(PrincipalStatus, name="principal_status")
    )
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


# ============================================================
#  LEASES
# ============================================================


class LeaseSql(Base):
    """One runtime instance's authority on one run — bijective with
    `g.node.instance.gt/001`.

    Keyed on (principal, run): single-writer authority is per-run, so one
    identity legitimately holds simultaneous Active leases on `hw1__1` and
    `hw1__2`. `principal_id` is the cert subject, matching `PrincipalSql.id`
    — for a GNode that is its GNodeId, which is why the word's `GNodeId`
    field maps onto it directly.

    **Revoked rows are permanent.** Revoked-forever is a claim about
    history, and a TTL cleanup would quietly re-admit an old zombie. Rows
    are tiny; they stay.

    The word's `Status` enum also carries `Ended`, which FIS never writes: a
    lease ends only by supersession, so clean stop and crash are
    indistinguishable and there is no third outcome to record.
    """

    __tablename__ = "leases"

    g_node_instance_id: Mapped[str] = mapped_column(String, primary_key=True)
    principal_id: Mapped[str] = mapped_column(String, index=True)
    run: Mapped[str] = mapped_column(String, index=True)

    status: Mapped[GNodeInstanceStatus] = mapped_column(
        Enum(GNodeInstanceStatus, name="g_node_instance_status")
    )
    transport: Mapped[GNodeInstanceTransport] = mapped_column(
        Enum(GNodeInstanceTransport, name="g_node_instance_transport")
    )

    connected_at_unix_ms: Mapped[int] = mapped_column(BigInteger)
    revoked_at_unix_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    connection_handle: Mapped[str | None] = mapped_column(String, nullable=True)
    observed_peer_address: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        # Invariant 1, single writer, enforced by the database rather than by
        # the gate's care: at most one Active lease per (principal, run). The
        # gate's sync-kill supersession is what keeps this satisfiable; this
        # index is what makes a bug in that path fail loudly instead of
        # admitting two writers.
        Index(
            "uq_active_lease_per_principal_run",
            "principal_id",
            "run",
            unique=True,
            postgresql_where=(status == GNodeInstanceStatus.Active),
        ),
    )

    def to_gt(self) -> GNodeInstanceGt:
        """Serialize SQL row → Sema GT."""
        return GNodeInstanceGt(
            g_node_id=self.principal_id,
            g_node_instance_id=self.g_node_instance_id,
            run=self.run,
            status=self.status,
            transport=self.transport,
            connected_at_unix_ms=self.connected_at_unix_ms,
            revoked_at_unix_ms=self.revoked_at_unix_ms,
            connection_handle=self.connection_handle,
            observed_peer_address=self.observed_peer_address,
        )

    @staticmethod
    def from_gt(gt: GNodeInstanceGt) -> LeaseSql:
        """Create SQL model from a Sema GT instance (already validated)."""
        return LeaseSql(
            g_node_instance_id=gt.g_node_instance_id,
            principal_id=gt.g_node_id,
            run=gt.run,
            status=gt.status,
            transport=gt.transport,
            connected_at_unix_ms=gt.connected_at_unix_ms,
            revoked_at_unix_ms=gt.revoked_at_unix_ms,
            connection_handle=gt.connection_handle,
            observed_peer_address=gt.observed_peer_address,
        )
