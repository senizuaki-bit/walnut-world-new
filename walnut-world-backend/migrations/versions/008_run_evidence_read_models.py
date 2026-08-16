"""Add immutable Run and Evidence read projections."""

from __future__ import annotations

from alembic import op
from sqlalchemy import Column, DateTime, String
from sqlalchemy.dialects.postgresql import JSONB

revision = "008_run_evidence_read_models"
down_revision = "007_product_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "game_runs",
        Column("run_id", String(128), primary_key=True),
        Column("tenant_id", String(96), nullable=False),
        Column("actor_id", String(128), nullable=False),
        Column("content_hash", String(64), nullable=False),
        Column("session_id", String(128), nullable=False),
        Column("turn_id", String(128), nullable=False),
        Column("command_id", String(128), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("run_json", JSONB(), nullable=False),
    )
    op.create_index("ix_game_runs_authority", "game_runs", ["tenant_id", "actor_id", "created_at"])
    op.create_table(
        "game_evidence",
        Column("evidence_id", String(128), primary_key=True),
        Column("tenant_id", String(96), nullable=False),
        Column("actor_id", String(128), nullable=False),
        Column("content_hash", String(64), nullable=False),
        Column("command_id", String(128), nullable=True),
        Column("recorded_at", DateTime(timezone=True), nullable=False),
        Column("evidence_json", JSONB(), nullable=False),
    )
    op.create_index(
        "ix_game_evidence_authority", "game_evidence", ["tenant_id", "actor_id", "recorded_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_game_evidence_authority", table_name="game_evidence")
    op.drop_table("game_evidence")
    op.drop_index("ix_game_runs_authority", table_name="game_runs")
    op.drop_table("game_runs")
