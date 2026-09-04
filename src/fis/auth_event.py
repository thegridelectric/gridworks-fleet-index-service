"""Recording each connect-gate verdict as a `fis.instance.authorization.event`.

The gate answers the broker first; the record is written afterwards so the
verdict's latency carries no database write. Every `/auth/user` outcome is
recorded, allow and deny alike — the audit trail is the point, and a denied
zombie is exactly what an operator will want to see.

`GateReason` is the gate's own vocabulary and includes the per-publish
verdicts (vhost, resource, topic), which are not instance authorizations
and have no event. The user-path reasons map onto the sema enum here,
one-to-one, in the one place the two meet.
"""

from __future__ import annotations

import logging
import time
import uuid

from sqlalchemy.orm import Session

from fis.db.models import AuthEventSql
from fis.gate import Decision, GateReason, GateResult, UserAuthRequest
from fis.sema.enums import FisAuthorizationDecision, FisAuthorizationReason
from fis.sema.types import FisInstanceAuthorizationEvent

logger = logging.getLogger(__name__)

# The connect-gate reasons, each to its sema value. A GateReason missing
# here is a per-publish verdict, never recorded as an instance event.
USER_REASONS: dict[GateReason, FisAuthorizationReason] = {
    GateReason.Malformed: FisAuthorizationReason.MalformedRequest,
    GateReason.PrincipalNotFound: FisAuthorizationReason.PrincipalNotFound,
    GateReason.PrincipalSuspended: FisAuthorizationReason.PrincipalSuspended,
    GateReason.RunOutsideUniverse: FisAuthorizationReason.RunOutsideUniverse,
    GateReason.NotInRegistry: FisAuthorizationReason.NotInRegistry,
    GateReason.AliasMismatch: FisAuthorizationReason.AliasMismatch,
    GateReason.ClassMismatch: FisAuthorizationReason.ClassMismatch,
    GateReason.RevokedForever: FisAuthorizationReason.InstanceRevoked,
    GateReason.KillUnconfirmed: FisAuthorizationReason.KillUnconfirmed,
    GateReason.LeaseRace: FisAuthorizationReason.LeaseRace,
    GateReason.LeaseMatch: FisAuthorizationReason.IdempotentReconnect,
    GateReason.Superseded: FisAuthorizationReason.Superseded,
}

DECISIONS: dict[Decision, FisAuthorizationDecision] = {
    Decision.Allow: FisAuthorizationDecision.Authorized,
    Decision.Deny: FisAuthorizationDecision.Denied,
}


def build_auth_event(
    req: UserAuthRequest, result: GateResult, *, decided_at_unix_ms: int
) -> FisInstanceAuthorizationEvent:
    """The event for one `/auth/user` verdict. Validated on construction:
    the word's projection axiom refuses a decision its reason does not
    imply, so a drift between the gate and this mapping fails here, loudly."""
    return FisInstanceAuthorizationEvent(
        event_id=str(uuid.uuid4()),
        principal_id=req.principal_id,
        instance_id=req.instance_id,
        run=req.run,
        alias=req.alias,
        g_node_class=req.g_node_class,
        transport=req.transport,
        decision=DECISIONS[result.decision],
        reason=USER_REASONS[result.reason],
        decided_at_unix_ms=decided_at_unix_ms,
    )


def record_auth_event(
    session: Session, req: UserAuthRequest, result: GateResult
) -> AuthEventSql:
    row = AuthEventSql.from_gt(
        build_auth_event(req, result, decided_at_unix_ms=int(time.time() * 1000))
    )
    session.add(row)
    session.commit()
    return row
