"""Add the independent authoritative World presentation stream.

Revision ID: 018_world_presentation_events
Revises: 017_durable_learner_worker
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "018_world_presentation_events"
down_revision = "017_durable_learner_worker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "world_presentation_streams",
        sa.Column("stream_id", sa.String(length=160), nullable=False),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("world_id", sa.String(length=128), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("initial_world_revision", sa.Integer(), nullable=False),
        sa.Column("initial_world_event_sequence", sa.Integer(), nullable=False),
        sa.Column("initial_snapshot_state_hash", sa.String(length=64), nullable=False),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False),
        sa.Column("last_world_revision", sa.Integer(), nullable=False),
        sa.Column("last_world_event_sequence", sa.Integer(), nullable=False),
        sa.Column("last_snapshot_state_hash", sa.String(length=64), nullable=False),
        sa.Column("gap_world_revision", sa.Integer()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "initial_snapshot_state_hash ~ '^[a-f0-9]{64}$' "
            "AND last_snapshot_state_hash ~ '^[a-f0-9]{64}$'",
            name="ck_world_presentation_stream_hashes",
        ),
        sa.CheckConstraint("last_sequence >= 0", name="ck_world_presentation_last_sequence"),
        sa.CheckConstraint(
            "initial_world_revision >= 0 AND initial_world_event_sequence >= 0 "
            "AND last_world_revision >= initial_world_revision "
            "AND last_world_event_sequence >= initial_world_event_sequence",
            name="ck_world_presentation_world_head",
        ),
        sa.CheckConstraint(
            "gap_world_revision IS NULL OR gap_world_revision >= 1",
            name="ck_world_presentation_gap_revision",
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "tenant_id"],
            ["world_snapshots.world_id", "world_snapshots.tenant_id"],
            name="fk_world_presentation_snapshot",
        ),
        sa.PrimaryKeyConstraint("stream_id", "tenant_id"),
        sa.UniqueConstraint(
            "tenant_id", "world_id", name="uq_world_presentation_tenant_world"
        ),
    )
    op.create_table(
        "world_presentation_events",
        sa.Column("event_id", sa.String(length=45), nullable=False),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("stream_id", sa.String(length=160), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("producer", sa.String(length=64), nullable=False),
        sa.Column("world_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("turn_id", sa.String(length=128), nullable=False),
        sa.Column("command_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("commit_id", sa.String(length=128), nullable=False),
        sa.Column("world_revision", sa.Integer(), nullable=False),
        sa.Column("action_index", sa.Integer(), nullable=False),
        sa.Column("action_count", sa.Integer(), nullable=False),
        sa.Column("intent_id", sa.String(length=128), nullable=False),
        sa.Column("state_hash_before", sa.String(length=64), nullable=False),
        sa.Column("state_hash_after", sa.String(length=64), nullable=False),
        sa.Column("final_snapshot_revision", sa.Integer(), nullable=False),
        sa.Column("final_world_event_sequence", sa.Integer(), nullable=False),
        sa.Column("final_snapshot_state_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("integrity_sha256", sa.String(length=64), nullable=False),
        sa.Column("event_json", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("sequence >= 1", name="ck_world_presentation_event_sequence"),
        sa.CheckConstraint(
            "action_count >= 1 AND action_index >= 0 AND action_index < action_count",
            name="ck_world_presentation_action_index",
        ),
        sa.CheckConstraint(
            "event_type = 'world.action.harvested' AND event_version = 1 "
            "AND schema_version = '1.0.0' AND producer = 'walnut_world_engine'",
            name="ck_world_presentation_event_version",
        ),
        sa.CheckConstraint(
            "state_hash_before ~ '^[a-f0-9]{64}$' "
            "AND state_hash_after ~ '^[a-f0-9]{64}$' "
            "AND final_snapshot_state_hash ~ '^[a-f0-9]{64}$' "
            "AND payload_sha256 ~ '^[a-f0-9]{64}$' "
            "AND integrity_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_world_presentation_event_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["stream_id", "tenant_id"],
            ["world_presentation_streams.stream_id", "world_presentation_streams.tenant_id"],
            name="fk_world_presentation_event_stream",
        ),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint(
            "tenant_id", "stream_id", "sequence", name="uq_world_presentation_sequence"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "commit_id",
            "action_index",
            name="uq_world_presentation_commit_action",
        ),
        sa.UniqueConstraint(
            "tenant_id", "commit_id", "intent_id", name="uq_world_presentation_commit_intent"
        ),
    )
    op.create_index(
        "ix_world_presentation_events_stream_sequence",
        "world_presentation_events",
        ["tenant_id", "stream_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_world_presentation_events_stream_sequence",
        table_name="world_presentation_events",
    )
    op.drop_table("world_presentation_events")
    op.drop_table("world_presentation_streams")
