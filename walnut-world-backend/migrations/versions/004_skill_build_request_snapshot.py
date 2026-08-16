"""Persist the private source request needed by asynchronous Skill Build workers."""

from __future__ import annotations

from alembic import op
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB

revision = "004_skill_build_request_snapshot"
down_revision = "003_skill_builds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("skill_builds", Column("request_json", JSONB(), nullable=True))
    op.execute("UPDATE skill_builds SET request_json = '{}'::jsonb WHERE request_json IS NULL")
    op.alter_column("skill_builds", "request_json", nullable=False)


def downgrade() -> None:
    op.drop_column("skill_builds", "request_json")
