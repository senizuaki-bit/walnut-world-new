"""Add immutable Product ContentUnit projections."""

from __future__ import annotations

from alembic import op
from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

revision = "012_product_content_units"
down_revision = "011_client_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_content_units",
        Column("content_row_id", Integer, primary_key=True, autoincrement=True),
        Column("tenant_id", String(96), nullable=False),
        Column("unit_id", String(128), nullable=False),
        Column("version", String(64), nullable=False),
        Column("content_hash", String(64), nullable=False),
        Column("audiences", JSONB(), nullable=False),
        Column("published_at", DateTime(timezone=True), nullable=False),
        Column("content_json", JSONB(), nullable=False),
        UniqueConstraint("tenant_id", "unit_id", "version", name="uq_product_content_version"),
        UniqueConstraint("tenant_id", "unit_id", "content_hash", name="uq_product_content_hash"),
    )
    op.create_index("ix_product_content_lookup", "product_content_units", ["tenant_id", "unit_id", "version", "content_hash"])


def downgrade() -> None:
    op.drop_index("ix_product_content_lookup", table_name="product_content_units")
    op.drop_table("product_content_units")
