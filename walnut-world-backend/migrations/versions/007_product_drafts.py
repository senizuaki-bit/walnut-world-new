"""Add Product Skill Draft CAS and path-scoped idempotency receipts."""

from __future__ import annotations

from alembic import op
from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

revision = "007_product_drafts"
down_revision = "006_agent_turns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_skill_drafts",
        Column("draft_row_id", Integer, primary_key=True, autoincrement=True),
        Column("tenant_id", String(96), nullable=False),
        Column("actor_id", String(128), nullable=False),
        Column("session_id", String(128), nullable=False),
        Column("draft_id", String(128), nullable=False),
        Column("skill_id", String(128), nullable=False),
        Column("revision", Integer, nullable=False),
        Column("draft_sha256", String(64), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        Column("draft_json", JSONB(), nullable=False),
        UniqueConstraint("tenant_id", "session_id", "draft_id", name="uq_product_draft_identity"),
        UniqueConstraint("tenant_id", "session_id", "skill_id", name="uq_product_draft_skill"),
    )
    op.create_index(
        "ix_product_drafts_authority",
        "product_skill_drafts",
        ["tenant_id", "actor_id", "session_id"],
    )
    op.create_table(
        "product_idempotency_receipts",
        Column("receipt_id", Integer, primary_key=True, autoincrement=True),
        Column("tenant_id", String(96), nullable=False),
        Column("actor_id", String(128), nullable=False),
        Column("operation", String(128), nullable=False),
        Column("canonical_path", String(512), nullable=False),
        Column("idempotency_key", String(128), nullable=False),
        Column("request_sha256", String(64), nullable=False),
        Column("resource_id", String(128), nullable=False),
        Column("http_status", Integer, nullable=False),
        Column("original_trace_id", String(128), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        UniqueConstraint(
            "tenant_id",
            "actor_id",
            "operation",
            "canonical_path",
            "idempotency_key",
            name="uq_product_idempotency_scope",
        ),
    )


def downgrade() -> None:
    op.drop_table("product_idempotency_receipts")
    op.drop_index("ix_product_drafts_authority", table_name="product_skill_drafts")
    op.drop_table("product_skill_drafts")
