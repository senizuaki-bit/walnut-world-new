"""Add durable ordered client event ingestion."""

from __future__ import annotations

from alembic import op
from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

revision = "011_client_events"
down_revision = "010_patch_decision_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "client_events",
        Column("event_id", String(160), primary_key=True),
        Column("tenant_id", String(96), nullable=False),
        Column("actor_id", String(128), nullable=False),
        Column("session_id", String(128), nullable=False),
        Column("world_id", String(128), nullable=False),
        Column("sequence", Integer, nullable=False),
        Column("occurred_at", DateTime(timezone=True), nullable=False),
        Column("event_json", JSONB(), nullable=False),
        UniqueConstraint("tenant_id", "session_id", "sequence", name="uq_client_event_session_sequence"),
    )
    op.create_index(
        "ix_client_events_authority",
        "client_events",
        ["tenant_id", "actor_id", "session_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_index("ix_client_events_authority", table_name="client_events")
    op.drop_table("client_events")
