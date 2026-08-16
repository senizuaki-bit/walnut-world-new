"""Add durable version-pinned Agent Session resources."""

from __future__ import annotations

from alembic import op
from sqlalchemy import Column, DateTime, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

revision = "005_agent_sessions"
down_revision = "004_skill_build_request_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_sessions",
        Column("session_id", String(128), primary_key=True),
        Column("tenant_id", String(96), nullable=False),
        Column("actor_id", String(128), nullable=False),
        Column("command_id", String(128), nullable=False),
        Column("world_id", String(128), nullable=False),
        Column("status", String(32), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        Column("session_json", JSONB(), nullable=False),
        UniqueConstraint("tenant_id", "command_id", name="uq_agent_session_command"),
    )
    op.create_index(
        "ix_agent_sessions_authority",
        "agent_sessions",
        ["tenant_id", "actor_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_sessions_authority", table_name="agent_sessions")
    op.drop_table("agent_sessions")
