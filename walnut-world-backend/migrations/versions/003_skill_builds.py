"""Persist canonical Skill Build resources beside their accepted commands."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "003_skill_builds"
down_revision = "002_world_event_store"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_builds",
        sa.Column("build_id", sa.String(length=128), primary_key=True),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("command_id", sa.String(length=128), nullable=False),
        sa.Column("skill_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("terminal", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("build_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.UniqueConstraint("tenant_id", "command_id", name="uq_skill_build_command"),
    )
    op.create_index(
        "ix_skill_builds_authority", "skill_builds", ["tenant_id", "actor_id", "updated_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_skill_builds_authority", table_name="skill_builds")
    op.drop_table("skill_builds")
