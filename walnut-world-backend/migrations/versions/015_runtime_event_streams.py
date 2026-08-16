"""Allow non-World runtime streams to share the authoritative event store."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "015_runtime_event_streams"
down_revision = "014_int1_authority_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "world_streams",
        "world_id",
        existing_type=sa.String(length=128),
        nullable=True,
    )


def downgrade() -> None:
    # PostgreSQL deliberately refuses this downgrade while non-World stream
    # heads exist.  Operators must archive those durable events explicitly;
    # the migration never deletes runtime authority to make a downgrade pass.
    op.alter_column(
        "world_streams",
        "world_id",
        existing_type=sa.String(length=128),
        nullable=False,
    )
