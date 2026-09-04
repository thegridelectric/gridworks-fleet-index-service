"""auth_events: one connect-gate verdict each

Revision ID: a7c3e1f9b2d4
Revises: 953747624bf9
Create Date: 2026-09-04 15:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c3e1f9b2d4"
down_revision: str | Sequence[str] | None = "953747624bf9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "auth_events",
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("principal_id", sa.String(), nullable=False),
        sa.Column("instance_id", sa.String(), nullable=False),
        sa.Column("run", sa.String(), nullable=False),
        sa.Column("alias", sa.String(), nullable=True),
        sa.Column("g_node_class", sa.String(), nullable=True),
        sa.Column(
            "transport",
            sa.Enum(
                "RabbitAmqp",
                "RabbitMqtt",
                name="g_node_instance_transport",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "decision",
            sa.Enum("Authorized", "Denied", name="fis_authorization_decision"),
            nullable=False,
        ),
        sa.Column(
            "reason",
            sa.Enum(
                "MalformedRequest",
                "PrincipalNotFound",
                "PrincipalSuspended",
                "RunOutsideUniverse",
                "NotInRegistry",
                "AliasMismatch",
                "ClassMismatch",
                "InstanceRevoked",
                "KillUnconfirmed",
                "LeaseRace",
                "IdempotentReconnect",
                "Superseded",
                name="fis_authorization_reason",
            ),
            nullable=False,
        ),
        sa.Column("decided_at_unix_ms", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        op.f("ix_auth_events_principal_id"), "auth_events", ["principal_id"]
    )
    op.create_index(op.f("ix_auth_events_instance_id"), "auth_events", ["instance_id"])
    op.create_index(
        op.f("ix_auth_events_decided_at_unix_ms"), "auth_events", ["decided_at_unix_ms"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_auth_events_decided_at_unix_ms"), table_name="auth_events")
    op.drop_index(op.f("ix_auth_events_instance_id"), table_name="auth_events")
    op.drop_index(op.f("ix_auth_events_principal_id"), table_name="auth_events")
    op.drop_table("auth_events")
    sa.Enum(name="fis_authorization_reason").drop(op.get_bind())
    sa.Enum(name="fis_authorization_decision").drop(op.get_bind())
