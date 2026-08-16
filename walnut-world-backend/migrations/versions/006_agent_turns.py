"""Add durable accepted Agent Turns before worker execution begins."""

from __future__ import annotations

from alembic import op
from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

revision = "006_agent_turns"
down_revision = "005_agent_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_turns",
        Column("turn_row_id", Integer, primary_key=True, autoincrement=True),
        Column("tenant_id", String(96), nullable=False),
        Column("actor_id", String(128), nullable=False),
        Column("session_id", String(128), nullable=False),
        Column("turn_id", String(128), nullable=False),
        Column("command_id", String(128), nullable=False),
        Column("turn_sequence", Integer, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("request_json", JSONB(), nullable=False),
        UniqueConstraint("tenant_id", "session_id", "turn_id", name="uq_agent_turn_identity"),
        UniqueConstraint("tenant_id", "command_id", name="uq_agent_turn_command"),
    )
    op.create_index(
        "ix_agent_turns_session_sequence",
        "agent_turns",
        ["tenant_id", "session_id", "turn_sequence"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_turns_session_sequence", table_name="agent_turns")
    op.drop_table("agent_turns")
