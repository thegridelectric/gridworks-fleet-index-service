"""The FIS authorization gate — the `/auth/user` decision.

Build step 3 of stand-up-fis, and the heart of the service: given what the
broker forwards at connect time, return the single allow/deny verdict that
admits or refuses a broker connection, and on a never-seen instance perform
the synchronous supersession that keeps single-writer true.

The decision is a function over a DB session, a parsed request, and an
injected `ConnectionKiller`. Keeping it here — separate from the HTTP wiring
(`api.py`) and the management-API kill (`rabbit_admin.py`) — is what makes
every verdict unit-testable without a live broker (the dev battery); the real
broker is reserved for staging, the verification that counts.

Response contract: the stock `rabbitmq_auth_backend_http` backend wants a
**plain-text** `allow`/`deny` body, not JSON — so `Decision.value` is exactly
that string. (The FIS executor spec's `{"result": "allow"}` mapping predates
this source-read and needs correcting.)
"""

from __future__ import annotations

import enum
import json
import logging
import time
from collections.abc import Mapping
from typing import NamedTuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fis.db.models import (
    GNodeSql,
    LeaseSql,
    PrincipalKind,
    PrincipalSql,
    PrincipalStatus,
)
from fis.gnr_client import RegistryReader
from fis.mirror import apply_gnode
from fis.rabbit_admin import ConnectionKiller
from fis.sema.codec import default_codec
from fis.sema.enums import GNodeInstanceStatus, GNodeInstanceTransport
from fis.sema.property_format import is_uuid4_str
from fis.sema.types import FisConnectClaims, GNodeInstanceGt

logger = logging.getLogger(__name__)


class Decision(enum.StrEnum):
    """The two verdicts, whose `value` is the literal broker response body."""

    Allow = "allow"
    Deny = "deny"


class GateReason(enum.StrEnum):
    """Why the gate decided as it did — for logging now, and the auth event
    later. FIS-internal: the broker response carries no hint channel.

    → retired by the `fis.authorization.reason` enum (currently `draft`) when
    build step 6 promotes it and wires the `fis.instance.authorization.event`.
    """

    Malformed = "malformed"
    PrincipalNotFound = "principal-not-found"
    PrincipalSuspended = "principal-suspended"
    RunOutsideUniverse = "run-outside-universe"
    LeaseMatch = "lease-match"
    RevokedForever = "revoked-forever"
    NotInRegistry = "not-in-registry"
    AliasMismatch = "alias-mismatch"
    ClassMismatch = "class-mismatch"
    Superseded = "superseded"
    KillUnconfirmed = "kill-unconfirmed"
    LeaseRace = "lease-race"
    # /auth/vhost
    VhostRunMatch = "vhost-run-match"
    VhostRunMismatch = "vhost-run-mismatch"
    # /auth/resource
    ResourceAllowed = "resource-allowed"
    # /auth/topic
    TopicRead = "topic-read"
    TopicWriteAliasMatch = "topic-write-alias-match"
    TopicWriteAliasMismatch = "topic-write-alias-mismatch"
    TopicWriteServiceAllowed = "topic-write-service-allowed"
    TopicWriteNoIdentity = "topic-write-no-identity"
    TopicMalformed = "topic-malformed"


class GateResult(NamedTuple):
    """A verdict plus the reason behind it (the reason is not sent to the
    broker; it is what the log line and the future auth event carry)."""

    decision: Decision
    reason: GateReason


class UserAuthRequest(NamedTuple):
    """What the broker forwards to `/auth/user`, parsed and transport-typed.

    FIS-internal request plumbing — no sema word covers this shape (the AMQP
    claims payload does, and rides in `claims`). AMQP carries the full
    `fis.connect.claims`; MQTT carries only `client_id` (the instance id) and
    `vhost` (the run), with alias/class enforced later at first publish.
    """

    principal_id: str  # cert CN: GNodeId for a GNode, principal UUID for a service
    transport: GNodeInstanceTransport
    instance_id: str
    run: str  # `universe.run`, which is also the vhost, e.g. hw1__1
    alias: str | None  # AMQP only
    g_node_class: str | None  # AMQP only


def _allow(reason: GateReason) -> GateResult:
    return GateResult(Decision.Allow, reason)


def _deny(reason: GateReason) -> GateResult:
    return GateResult(Decision.Deny, reason)


def parse_user_request(params: Mapping[str, str]) -> UserAuthRequest | None:
    """Turn the broker's form/query params into a typed request, or `None`
    when they are malformed (which the caller maps to deny).

    The transport is discriminated structurally: a `claims` param means the
    GridWorks SASL mechanism carried a `fis.connect.claims` payload (AMQP);
    otherwise `client_id` + `vhost` is the MQTT adapter's shape.
    """
    username = params.get("username")
    if not username:
        return None

    claims_raw = params.get("claims")
    if claims_raw is not None:
        try:
            claims = default_codec.from_dict(
                json.loads(claims_raw), expect=FisConnectClaims
            )
        except Exception as e:  # noqa: BLE001 -- any decode failure is "malformed"
            logger.info("auth/user malformed claims for %s: %s", username, e)
            return None
        return UserAuthRequest(
            principal_id=username,
            transport=GNodeInstanceTransport.RabbitAmqp,
            instance_id=claims.instance_id,
            run=claims.run,
            alias=claims.alias,
            g_node_class=claims.g_node_class,
        )

    client_id = params.get("client_id")
    vhost = params.get("vhost")
    if client_id and vhost:
        try:
            is_uuid4_str(client_id)  # the MQTT client_id must be a GNodeInstanceId
        except ValueError:
            logger.info("auth/user MQTT client_id not a uuid4 for %s", username)
            return None
        return UserAuthRequest(
            principal_id=username,
            transport=GNodeInstanceTransport.RabbitMqtt,
            instance_id=client_id,
            run=vhost,
            alias=None,
            g_node_class=None,
        )

    return None


def decide_user(
    session: Session,
    req: UserAuthRequest,
    killer: ConnectionKiller,
    registry: RegistryReader,
    *,
    universe: str,
) -> GateResult:
    """The five verdicts, in order (FIS executor "`/auth/user` — the gate").

    Malformed is handled upstream in `parse_user_request`; here we assume a
    well-formed request and decide: principal status, then lease state, then
    — on a never-seen instance — synchronous supersession before allowing.

    A GNode the mirror does not know is read through from the registry
    before the alias/class check, so a freshly provisioned node connecting
    ahead of the next reconcile is admitted on its first try. A registry
    that does not know it, or cannot be reached, admits nothing new.
    """
    if not req.run.startswith(f"{universe}__"):
        return _deny(GateReason.RunOutsideUniverse)

    principal = session.get(PrincipalSql, req.principal_id)
    if principal is None:
        return _deny(GateReason.PrincipalNotFound)
    if principal.status != PrincipalStatus.Active:
        return _deny(GateReason.PrincipalSuspended)

    existing = session.get(LeaseSql, req.instance_id)
    if existing is not None:
        if (
            existing.status == GNodeInstanceStatus.Active
            and existing.principal_id == req.principal_id
            and existing.run == req.run
        ):
            return _allow(GateReason.LeaseMatch)
        if existing.status == GNodeInstanceStatus.Active:
            # A uuid4 instance id owned by another identity/run cannot happen
            # honestly; refuse rather than reason about it.
            return _deny(GateReason.Malformed)
        # Revoked (or Ended, which FIS never writes) → denied forever.
        return _deny(GateReason.RevokedForever)

    # Never-seen instance id → supersession. A GNode's AMQP claims are
    # checked against the registry first; a service principal has no
    # registry row to check (its claims carry no GNodeClass).
    if (
        req.transport == GNodeInstanceTransport.RabbitAmqp
        and principal.kind == PrincipalKind.GNode
    ):
        gnode = session.get(GNodeSql, req.principal_id)
        if gnode is None:
            fetched = registry.get_by_id(req.principal_id)
            if fetched is None:
                return _deny(GateReason.NotInRegistry)
            apply_gnode(session, fetched, killer)
            gnode = session.get(GNodeSql, req.principal_id)
            assert gnode is not None  # apply_gnode just inserted it
        if req.alias != gnode.alias:
            return _deny(GateReason.AliasMismatch)
        if req.g_node_class != gnode.g_node_class:
            return _deny(GateReason.ClassMismatch)

    prior = (
        session.query(LeaseSql)
        .filter(
            LeaseSql.principal_id == req.principal_id,
            LeaseSql.run == req.run,
            LeaseSql.status == GNodeInstanceStatus.Active,
        )
        .one_or_none()
    )
    now_ms = int(time.time() * 1000)
    if prior is not None:
        # Revoke the predecessor first so the partial-unique index (one Active
        # lease per (principal, run)) admits the successor's row below.
        prior.status = GNodeInstanceStatus.Revoked
        prior.revoked_at_unix_ms = now_ms
        session.flush()

    # Close the predecessor's connections and confirm none remain (an empty
    # kill is success). Unconfirmable → fail closed: roll the revoke back so
    # the predecessor keeps its lease, and deny.
    if not killer.kill(principal_id=req.principal_id, vhost=req.run):
        session.rollback()
        return _deny(GateReason.KillUnconfirmed)

    lease = GNodeInstanceGt(
        g_node_id=req.principal_id,
        g_node_instance_id=req.instance_id,
        run=req.run,
        status=GNodeInstanceStatus.Active,
        transport=req.transport,
        connected_at_unix_ms=now_ms,
    )
    session.add(LeaseSql.from_gt(lease))
    try:
        session.commit()
    except IntegrityError:
        # A concurrent boot won the run between our revoke and our insert; the
        # index caught it. Fail closed and let this instance retry.
        session.rollback()
        return _deny(GateReason.LeaseRace)
    return _allow(GateReason.Superseded)


def decide_vhost(session: Session, *, username: str, vhost: str) -> GateResult:
    """`/auth/vhost` — cross-check the claimed run against the vhost opened.

    The `/auth/vhost` call carries the actual vhost but not the claims; the
    claimed run reached FIS at `/auth/user` (which fires first) and was
    recorded as the lease's run. So an Active lease for (principal, vhost)
    exists iff the client's claimed run equals the vhost it is opening.
    gwbase derives `Run` from the vhost, so an honest actor matches by
    construction; a hand-built client claiming a different run has no lease
    here and is denied.
    """
    lease = (
        session.query(LeaseSql)
        .filter(
            LeaseSql.principal_id == username,
            LeaseSql.run == vhost,
            LeaseSql.status == GNodeInstanceStatus.Active,
        )
        .one_or_none()
    )
    if lease is None:
        return _deny(GateReason.VhostRunMismatch)
    return _allow(GateReason.VhostRunMatch)


def decide_resource() -> GateResult:
    """`/auth/resource` — v1 allow-all (executor "Scope")."""
    return _allow(GateReason.ResourceAllowed)


# tokens[0] is the category (rj/rjb/gw); tokens[1] is the from-alias in LRH
# (hyphenated) form — the "segment 2" of the routing-key grammar, identical
# across all three grammars (gwbase `transport_encoding.py`).
FROM_ALIAS_SEGMENT = 1


def decide_topic(
    session: Session, *, username: str, permission: str, routing_key: str
) -> GateResult:
    """`/auth/topic` — the alias-pinning write rule; reads are allowed.

    A read (fired on every MQTT subscribe) is about visibility, not
    authority, so it is allowed (OPS-420 "The read side is open"). A write is
    authorized iff the routing key's from-alias segment equals the wire-form
    (hyphenated) current alias of the connection's identity.
    """
    if permission != "write":
        return _allow(GateReason.TopicRead)

    # MQTT topics arrive slash-separated; the AMQP routing key is dotted.
    # Normalize so one rule covers both (aliases are hyphenated, never
    # slashed, so this cannot corrupt a segment).
    parts = routing_key.replace("/", ".").split(".")
    if len(parts) <= FROM_ALIAS_SEGMENT:
        return _deny(GateReason.TopicMalformed)
    segment = parts[FROM_ALIAS_SEGMENT]

    gnode = session.get(GNodeSql, username)
    if gnode is None:
        # Not a GNode in the mirror. A service principal has no registry alias
        # to pin in v1, so its writes are allowed (it is cert-authenticated
        # infra); anything else is denied.
        principal = session.get(PrincipalSql, username)
        if (
            principal is not None
            and principal.kind == PrincipalKind.Service
            and principal.status == PrincipalStatus.Active
        ):
            return _allow(GateReason.TopicWriteServiceAllowed)
        return _deny(GateReason.TopicWriteNoIdentity)

    if segment == gnode.alias.replace(".", "-"):
        return _allow(GateReason.TopicWriteAliasMatch)
    return _deny(GateReason.TopicWriteAliasMismatch)
