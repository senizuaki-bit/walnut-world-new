"""Core command, idempotency, audit, and reliable outbox infrastructure."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "001_core_infrastructure"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "commands",
        sa.Column("command_id", sa.String(length=128), primary_key=True),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("command_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("terminal", sa.Boolean(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("record_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )
    op.create_index("ix_commands_tenant_id", "commands", ["tenant_id"])
    op.create_index("ix_commands_updated_at", "commands", ["updated_at"])
    op.create_table(
        "idempotency_receipts",
        sa.Column("receipt_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("command_id", sa.String(length=128), nullable=False, unique=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "actor_id", "operation", "idempotency_key", name="uq_command_idempotency_scope"
        ),
    )
    op.create_table(
        "audit_records",
        sa.Column("audit_id", sa.String(length=128), primary_key=True),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("operation", sa.String(length=128), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("record_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )
    op.create_index("ix_audit_records_tenant_id", "audit_records", ["tenant_id"])
    op.create_index("ix_audit_records_occurred_at", "audit_records", ["occurred_at"])
    op.create_table(
        "outbox_messages",
        sa.Column("message_id", sa.String(length=128), primary_key=True),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("destination", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("lease_id", sa.String(length=128)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("message_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.UniqueConstraint("tenant_id", "destination", "idempotency_key", name="uq_outbox_delivery_scope"),
    )
    op.create_index(
        "ix_outbox_ready",
        "outbox_messages",
        ["tenant_id", "status", "next_attempt_at", "lease_expires_at"],
    )
    op.execute(
        """
        CREATE FUNCTION prevent_audit_mutation() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'audit_records are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_records_no_update BEFORE UPDATE OR DELETE ON audit_records
        FOR EACH ROW EXECUTE FUNCTION prevent_audit_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_records_no_update ON audit_records")
    op.execute("DROP FUNCTION IF EXISTS prevent_audit_mutation()")
    op.drop_table("outbox_messages")
    op.drop_table("audit_records")
    op.drop_table("idempotency_receipts")
    op.drop_table("commands")
