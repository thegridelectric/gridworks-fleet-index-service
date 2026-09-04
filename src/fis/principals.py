"""Minting and administering principals — the identities that may hold
leases.

A principal is a durable identity belonging to a core piece of the
GridWorks platform, allowed to connect to the broker. A GNode principal
is a node in the grid topology (a house's scada, its leaf transactive
node, a market maker) and its id is its GNodeId. A Service principal is
platform infrastructure outside the topology (the grid-node-registry, the
ear, the journalkeeper, the weather forecast service) and its id is a UUID
FIS mints.

A principal row is created **before** its certificate is cut: `create`
mints the row and returns its id, and that id becomes the cert CN. For a
service the id is a fresh uuid4 minted here; for a GNode the id is its
GNodeId, given, because the cert CN for a GNode is the GNodeId itself and
there is no second identifier. Row first, cert second, so the CN is never a
hand-picked value a row is later back-filled to match.

Suspension is the emergency-eviction lever the gate already honors: a
suspended principal is denied on every connect regardless of lease state.
The functions here are pure over a session so they are tested against
Postgres without the CLI; `cli.py` is the thin front.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from fis.db.models import PrincipalKind, PrincipalSql, PrincipalStatus
from fis.sema.property_format import UUID4Str, is_uuid4_str


class PrincipalExists(ValueError):
    """The id is already a principal."""


class PrincipalUnknown(ValueError):
    """No principal has this id."""


def create_principal(
    session: Session,
    *,
    kind: PrincipalKind,
    g_node_id: UUID4Str | None,
    display_name: str | None,
) -> PrincipalSql:
    """Mint a principal row. A Service mints its own id; a GNode's id is
    its GNodeId and must be given (and must not be given for a Service)."""
    if kind is PrincipalKind.GNode:
        if g_node_id is None:
            raise ValueError("a GNode principal's id is its GNodeId; pass --g-node-id")
        principal_id = is_uuid4_str(g_node_id)
    else:
        if g_node_id is not None:
            raise ValueError("a Service principal mints its own id; drop --g-node-id")
        principal_id = str(uuid.uuid4())

    if session.get(PrincipalSql, principal_id) is not None:
        raise PrincipalExists(f"{principal_id} is already a principal")

    row = PrincipalSql(
        id=principal_id,
        kind=kind,
        status=PrincipalStatus.Active,
        display_name=display_name,
        created_at=datetime.now(UTC),
    )
    session.add(row)
    session.commit()
    return row


def list_principals(session: Session) -> list[PrincipalSql]:
    return session.query(PrincipalSql).order_by(PrincipalSql.created_at).all()


def set_principal_status(
    session: Session, principal_id: str, status: PrincipalStatus
) -> PrincipalSql:
    row = session.get(PrincipalSql, principal_id)
    if row is None:
        raise PrincipalUnknown(f"no principal {principal_id}")
    row.status = status
    session.commit()
    return row
