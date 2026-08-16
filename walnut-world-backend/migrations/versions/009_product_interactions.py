"""Add durable Product Agent interaction read projections."""

from __future__ import annotations

from alembic import op
from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

revision = "009_product_interactions"
down_revision = "008_run_evidence_read_models"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_agent_interactions",
        Column("interaction_row_id", Integer, primary_key=True, autoincrement=True),
        Column("tenant_id", String(96), nullable=False),
        Column("actor_id", String(128), nullable=False),
        Column("session_id", String(128), nullable=False),
        Column("interaction_id", String(128), nullable=False),
        Column("turn_id", String(128), nullable=False),
        Column("sequence", Integer, nullable=False),
        Column("interaction_revision", Integer, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        Column("interaction_json", JSONB(), nullable=False),
        UniqueConstraint(
            "tenant_id", "session_id", "interaction_id", name="uq_product_interaction_identity"
        ),
        UniqueConstraint(
            "tenant_id", "session_id", "sequence", name="uq_product_interaction_sequence"
        ),
    )
    op.create_index(
        "ix_product_interactions_authority",
        "product_agent_interactions",
        ["tenant_id", "actor_id", "session_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_index("ix_product_interactions_authority", table_name="product_agent_interactions")
    op.drop_table("product_agent_interactions")
