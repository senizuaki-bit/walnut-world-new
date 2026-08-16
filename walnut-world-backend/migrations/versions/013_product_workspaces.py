"""Add recoverable Product SessionWorkspace projections."""

from __future__ import annotations

from alembic import op
from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

revision = "013_product_workspaces"
down_revision = "012_product_content_units"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("product_workspaces", Column("workspace_id", String(128), primary_key=True), Column("tenant_id", String(96), nullable=False), Column("actor_id", String(128), nullable=False), Column("session_id", String(128), nullable=False), Column("workspace_revision", Integer, nullable=False), Column("updated_at", DateTime(timezone=True), nullable=False), Column("workspace_json", JSONB(), nullable=False), UniqueConstraint("tenant_id", "session_id", name="uq_product_workspace_session"))


def downgrade() -> None:
    op.drop_table("product_workspaces")
