"""Persist Product PatchDecision idempotency receipts."""

from __future__ import annotations

from alembic import op
from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

revision = "010_patch_decision_receipts"
down_revision = "009_product_interactions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_patch_decision_receipts",
        Column("receipt_id", Integer, primary_key=True, autoincrement=True),
        Column("tenant_id", String(96), nullable=False),
        Column("actor_id", String(128), nullable=False),
        Column("canonical_path", String(512), nullable=False),
        Column("idempotency_key", String(128), nullable=False),
        Column("request_sha256", String(64), nullable=False),
        Column("interaction_id", String(128), nullable=False),
        Column("interaction_revision", Integer, nullable=False),
        Column("receipt_json", JSONB(), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        UniqueConstraint(
            "tenant_id", "actor_id", "canonical_path", "idempotency_key",
            name="uq_product_patch_decision_idempotency",
        ),
    )


def downgrade() -> None:
    op.drop_table("product_patch_decision_receipts")
