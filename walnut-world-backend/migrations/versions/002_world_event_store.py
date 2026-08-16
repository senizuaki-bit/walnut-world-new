"""Create durable World snapshots and append-only Event streams."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "002_world_event_store"
down_revision = "001_core_infrastructure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "world_streams",
        sa.Column("stream_id", sa.String(length=160), primary_key=True),
        sa.Column("tenant_id", sa.String(length=96), primary_key=True),
        sa.Column("world_id", sa.String(length=128), nullable=False),
        sa.Column("last_sequence", sa.Integer(), nullable=False),
        sa.UniqueConstraint("tenant_id", "world_id", name="uq_world_stream_tenant_world"),
    )
    op.create_table(
        "world_snapshots",
        sa.Column("world_id", sa.String(length=128), primary_key=True),
        sa.Column("tenant_id", sa.String(length=96), primary_key=True),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("last_event_sequence", sa.Integer(), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )
    op.create_index(
        "ix_world_snapshots_authority",
        "world_snapshots",
        ["tenant_id", "actor_id", "content_hash"],
    )
    op.create_table(
        "domain_events",
        sa.Column("event_id", sa.String(length=132), primary_key=True),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("stream_id", sa.String(length=160), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.UniqueConstraint("tenant_id", "stream_id", "sequence", name="uq_domain_event_stream_sequence"),
    )
    op.create_index("ix_domain_events_tenant_id", "domain_events", ["tenant_id"])
    op.create_index(
        "ix_domain_events_stream_sequence",
        "domain_events",
        ["tenant_id", "stream_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_index("ix_domain_events_stream_sequence", table_name="domain_events")
    op.drop_index("ix_domain_events_tenant_id", table_name="domain_events")
    op.drop_table("domain_events")
    op.execute("DROP INDEX IF EXISTS ix_world_snapshots_authority")
    op.drop_table("world_snapshots")
    op.drop_table("world_streams")
