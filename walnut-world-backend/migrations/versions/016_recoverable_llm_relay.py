"""Add the private durable YAYA_RECOVERABLE_LLM_V1 relay authority."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "016_recoverable_llm_relay"
down_revision = "015_runtime_event_streams"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recoverable_llm_dispatches",
        sa.Column("dispatch_id", sa.String(length=47), primary_key=True),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("context_sha256", sa.String(length=64), nullable=False),
        sa.Column("completion_sha256", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("request_body_sha256", sa.String(length=64), nullable=False),
        sa.Column("request_body", sa.LargeBinary(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("generation_count", sa.Integer(), nullable=False),
        sa.Column("dispatch_started_at", sa.DateTime(timezone=True)),
        sa.Column("upstream_deadline_at", sa.DateTime(timezone=True)),
        sa.Column("response_http_status", sa.Integer()),
        sa.Column("response_content_type", sa.String(length=256)),
        sa.Column("response_body_sha256", sa.String(length=64)),
        sa.Column("response_body", sa.LargeBinary()),
        sa.Column("failure_code", sa.String(length=96)),
        sa.Column("failure_retryable", sa.Boolean()),
        sa.Column("terminal_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('PENDING','SUCCEEDED','FAILED','EXPIRED')",
            name="ck_recoverable_llm_dispatch_state",
        ),
        sa.CheckConstraint(
            "generation_count IN (0, 1)",
            name="ck_recoverable_llm_generation_count",
        ),
        sa.CheckConstraint(
            "response_http_status IS NULL OR response_http_status BETWEEN 100 AND 599",
            name="ck_recoverable_llm_http_status",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name="ck_recoverable_llm_updated_order",
        ),
        sa.CheckConstraint(
            "terminal_at IS NULL OR terminal_at >= created_at",
            name="ck_recoverable_llm_terminal_order",
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR (terminal_at IS NOT NULL AND expires_at >= terminal_at)",
            name="ck_recoverable_llm_expiry_order",
        ),
        sa.CheckConstraint(
            "request_sha256 ~ '^[a-f0-9]{64}$' AND "
            "context_sha256 ~ '^[a-f0-9]{64}$' AND "
            "completion_sha256 ~ '^[a-f0-9]{64}$' AND "
            "request_body_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_recoverable_llm_request_hashes",
        ),
        sa.CheckConstraint(
            "response_body_sha256 IS NULL OR response_body_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_recoverable_llm_response_hash",
        ),
        sa.CheckConstraint(
            "(generation_count = 0 AND dispatch_started_at IS NULL "
            "AND upstream_deadline_at IS NULL) OR "
            "(generation_count = 1 AND dispatch_started_at IS NOT NULL "
            "AND upstream_deadline_at IS NOT NULL)",
            name="ck_recoverable_llm_generation_timestamps",
        ),
        sa.CheckConstraint(
            "state <> 'SUCCEEDED' OR "
            "(generation_count = 1 AND response_http_status IS NOT NULL "
            "AND response_content_type IS NOT NULL AND response_body IS NOT NULL "
            "AND response_body_sha256 IS NOT NULL AND failure_code IS NULL "
            "AND failure_retryable IS NULL AND terminal_at IS NOT NULL "
            "AND expires_at IS NOT NULL)",
            name="ck_recoverable_llm_success_shape",
        ),
        sa.CheckConstraint(
            "state <> 'FAILED' OR "
            "(generation_count = 1 AND failure_code IS NOT NULL "
            "AND failure_retryable IS NOT NULL AND response_http_status IS NULL "
            "AND response_content_type IS NULL AND response_body IS NULL "
            "AND response_body_sha256 IS NULL AND terminal_at IS NOT NULL "
            "AND expires_at IS NOT NULL)",
            name="ck_recoverable_llm_failure_shape",
        ),
        sa.CheckConstraint(
            "state <> 'EXPIRED' OR "
            "(generation_count = 1 AND request_body IS NULL AND response_http_status IS NULL "
            "AND response_content_type IS NULL AND response_body IS NULL "
            "AND response_body_sha256 IS NULL AND failure_code IS NULL "
            "AND failure_retryable IS NULL AND terminal_at IS NOT NULL "
            "AND expires_at IS NOT NULL)",
            name="ck_recoverable_llm_expired_shape",
        ),
        sa.CheckConstraint(
            "state <> 'PENDING' OR "
            "(request_body IS NOT NULL AND terminal_at IS NULL AND expires_at IS NULL "
            "AND response_http_status IS NULL AND response_content_type IS NULL "
            "AND response_body IS NULL AND response_body_sha256 IS NULL "
            "AND failure_code IS NULL AND failure_retryable IS NULL)",
            name="ck_recoverable_llm_pending_shape",
        ),
    )
    op.create_index(
        "ix_recoverable_llm_dispatch_ready",
        "recoverable_llm_dispatches",
        ["state", "generation_count", "created_at"],
    )
    op.create_index(
        "ix_recoverable_llm_dispatch_expiry",
        "recoverable_llm_dispatches",
        ["state", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_recoverable_llm_dispatch_expiry",
        table_name="recoverable_llm_dispatches",
    )
    op.drop_index(
        "ix_recoverable_llm_dispatch_ready",
        table_name="recoverable_llm_dispatches",
    )
    op.drop_table("recoverable_llm_dispatches")
