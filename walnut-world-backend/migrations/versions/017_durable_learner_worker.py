"""Make learner projection a separately claimed, fenced Backend workflow."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "017_durable_learner_worker"
down_revision = "016_recoverable_llm_relay"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A Turn worker releases its own lease after an atomic learner hand-off.
    # WAITING_PROJECTION is deliberately absent from the normal workflow claim
    # set; only the independently fenced learner worker may close this state.
    op.drop_constraint("ck_workflow_job_status", "workflow_jobs", type_="check")
    op.create_check_constraint(
        "ck_workflow_job_status",
        "workflow_jobs",
        "status IN ('ACCEPTED','READY','CLAIMED','RUNNING','RETRY_WAIT',"
        "'WAITING_PROJECTION','SUCCEEDED','FAILED','CANCELLED','DEAD_LETTER')",
    )

    for column in (
        sa.Column("command_id", sa.String(length=128)),
        sa.Column("session_id", sa.String(length=128)),
        sa.Column("turn_id", sa.String(length=128)),
        sa.Column("run_id", sa.String(length=128)),
        sa.Column("status", sa.String(length=32)),
        sa.Column("attempt", sa.Integer()),
        sa.Column("fencing_token", sa.BigInteger()),
        sa.Column("lease_owner", sa.String(length=128)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("request_sha256", sa.String(length=64)),
        sa.Column("result_sha256", sa.String(length=64)),
        sa.Column("result_json", postgresql.JSONB()),
        sa.Column("last_error_json", postgresql.JSONB()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    ):
        op.add_column("learner_projection_jobs", column)

    # Rows created by 014 were already synchronously projected. Preserve that
    # authority as terminal history rather than replaying learner side effects.
    bind = op.get_bind()
    legacy_count = bind.scalar(sa.text("SELECT count(*) FROM learner_projection_jobs"))
    rows = list(
        bind.execute(
            sa.text(
                """
            SELECT lp.job_id,
                   lp.projection_json,
                   lp.created_at,
                   lp.updated_at,
                   workflow.command_id,
                   run.session_id,
                   run.turn_id,
                   run.run_id,
                   outcome.receipt_id AS outcome_receipt_id,
                   outcome.step_name AS outcome_step_name,
                   outcome.fencing_token AS outcome_fencing_token,
                   outcome.input_sha256 AS outcome_input_sha256,
                   outcome.output_sha256 AS outcome_output_sha256,
                   outcome.receipt_json AS outcome_receipt_json,
                   outcome.completed_at AS outcome_completed_at,
                   final.receipt_id AS final_receipt_id,
                   final.step_name AS final_step_name,
                   final.fencing_token AS final_fencing_token,
                   final.input_sha256 AS final_input_sha256,
                   final.output_sha256 AS final_output_sha256,
                   final.receipt_json AS final_receipt_json,
                   final.completed_at AS final_completed_at
              FROM learner_projection_jobs AS lp
              JOIN workflow_jobs AS workflow
                ON workflow.tenant_id = lp.tenant_id
               AND workflow.job_id = lp.job_id
              JOIN game_runs AS run
                ON run.tenant_id = workflow.tenant_id
               AND run.command_id = workflow.command_id
              JOIN job_step_receipts AS outcome
                ON outcome.tenant_id = workflow.tenant_id
               AND outcome.job_id = workflow.job_id
               AND outcome.step_name = 'OUTCOME_DERIVED'
              JOIN job_step_receipts AS final
                ON final.tenant_id = workflow.tenant_id
               AND final.job_id = workflow.job_id
               AND final.step_name = 'FINAL_DECISION_DERIVED'
            """
            )
        ).mappings()
    )
    if legacy_count != len(rows) or len({row["job_id"] for row in rows}) != len(rows):
        raise RuntimeError(
            "legacy learner projection rows do not have one exact Workflow/Run closure"
        )
    for row in rows:
        projection = _mapping(row["projection_json"])
        outcome_receipt_json = _mapping(row["outcome_receipt_json"])
        final_receipt_json = _mapping(row["final_receipt_json"])
        outcome = _mapping(outcome_receipt_json.get("event"))
        final_decision = _mapping(final_receipt_json.get("decision"))
        if (
            not isinstance(projection.get("source_feedback_event_id"), str)
            or not isinstance(projection.get("source_evidence_ids"), list)
            or row["outcome_output_sha256"] != _canonical_sha256(outcome_receipt_json)
            or row["final_output_sha256"] != _canonical_sha256(final_receipt_json)
        ):
            raise RuntimeError("legacy learner projection receipt authority is corrupt")
        projection.update(
            {
                "outcome": outcome,
                "outcome_receipt": _receipt_wire(row, "outcome"),
                "final_decision": final_decision,
                "final_decision_receipt": _receipt_wire(row, "final"),
            }
        )
        projection_sha256 = _canonical_sha256(projection)
        bind.execute(
            sa.text(
                """
                UPDATE learner_projection_jobs
                   SET command_id = :command_id,
                       session_id = :session_id,
                       turn_id = :turn_id,
                       run_id = :run_id,
                       status = 'SUCCEEDED',
                       attempt = 0,
                       fencing_token = 0,
                       next_attempt_at = NULL,
                       request_sha256 = :projection_sha256,
                       result_sha256 = :projection_sha256,
                       result_json = CAST(:projection_json AS jsonb),
                       completed_at = :completed_at
                 WHERE job_id = :job_id
                """
            ),
            {
                "job_id": row["job_id"],
                "command_id": row["command_id"],
                "session_id": row["session_id"],
                "turn_id": row["turn_id"],
                "run_id": row["run_id"],
                "projection_sha256": projection_sha256,
                "projection_json": json.dumps(
                    projection,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                "completed_at": row["updated_at"],
            },
        )

    for name in (
        "command_id",
        "session_id",
        "turn_id",
        "run_id",
        "status",
        "attempt",
        "fencing_token",
        "request_sha256",
    ):
        op.alter_column("learner_projection_jobs", name, nullable=False)

    op.create_foreign_key(
        "fk_learner_projection_command",
        "learner_projection_jobs",
        "commands",
        ["command_id"],
        ["command_id"],
    )
    op.create_unique_constraint(
        "uq_learner_projection_command",
        "learner_projection_jobs",
        ["tenant_id", "command_id"],
    )
    op.create_unique_constraint(
        "uq_learner_projection_turn",
        "learner_projection_jobs",
        ["tenant_id", "session_id", "turn_id"],
    )
    op.create_unique_constraint(
        "uq_learner_projection_run",
        "learner_projection_jobs",
        ["tenant_id", "run_id"],
    )
    op.create_unique_constraint(
        "uq_learner_projection_revision",
        "learner_projection_jobs",
        ["tenant_id", "learner_id", "actor_id", "content_hash", "expected_revision"],
    )
    op.create_unique_constraint(
        "uq_learner_projection_sequence",
        "learner_projection_jobs",
        ["tenant_id", "learner_id", "actor_id", "content_hash", "through_sequence"],
    )
    op.create_index(
        "ix_learner_projection_jobs_ready",
        "learner_projection_jobs",
        ["status", "next_attempt_at", "lease_expires_at", "created_at"],
    )
    op.create_check_constraint(
        "ck_learner_projection_status",
        "learner_projection_jobs",
        "status IN ('READY','CLAIMED','RUNNING','RETRY_WAIT','SUCCEEDED','DEAD_LETTER')",
    )
    op.create_check_constraint(
        "ck_learner_projection_attempt",
        "learner_projection_jobs",
        "attempt >= 0",
    )
    op.create_check_constraint(
        "ck_learner_projection_fencing_token",
        "learner_projection_jobs",
        "fencing_token >= 0",
    )
    op.create_check_constraint(
        "ck_learner_projection_request_sha256",
        "learner_projection_jobs",
        "request_sha256 ~ '^[a-f0-9]{64}$'",
    )
    op.create_check_constraint(
        "ck_learner_projection_result_sha256",
        "learner_projection_jobs",
        "result_sha256 IS NULL OR result_sha256 ~ '^[a-f0-9]{64}$'",
    )
    op.create_check_constraint(
        "ck_learner_projection_lease_state",
        "learner_projection_jobs",
        "((status IN ('CLAIMED','RUNNING')) = "
        "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL))",
    )
    op.create_check_constraint(
        "ck_learner_projection_next_attempt",
        "learner_projection_jobs",
        "((status IN ('READY','RETRY_WAIT')) = (next_attempt_at IS NOT NULL))",
    )
    op.create_check_constraint(
        "ck_learner_projection_terminal_payload",
        "learner_projection_jobs",
        "(status = 'SUCCEEDED' AND result_sha256 IS NOT NULL AND result_json IS NOT NULL "
        "AND completed_at IS NOT NULL) OR "
        "(status = 'DEAD_LETTER' AND result_sha256 IS NULL AND result_json IS NULL "
        "AND completed_at IS NOT NULL) OR "
        "(status NOT IN ('SUCCEEDED','DEAD_LETTER') AND result_sha256 IS NULL "
        "AND result_json IS NULL AND completed_at IS NULL)",
    )


def downgrade() -> None:
    # Re-exposing a handed-off Turn as READY would repeat Provider/Sandbox side
    # effects. Refuse the downgrade until every durable hand-off is terminal.
    bind = op.get_bind()
    unsafe = bind.scalar(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1 FROM workflow_jobs WHERE status = 'WAITING_PROJECTION'
                UNION ALL
                SELECT 1
                  FROM learner_projection_jobs
                 WHERE status <> 'SUCCEEDED'
            )
            """
        )
    )
    if unsafe:
        raise RuntimeError(
            "cannot downgrade 017 unless every learner projection hand-off succeeded"
        )
    for name in (
        "ck_learner_projection_terminal_payload",
        "ck_learner_projection_next_attempt",
        "ck_learner_projection_lease_state",
        "ck_learner_projection_result_sha256",
        "ck_learner_projection_request_sha256",
        "ck_learner_projection_fencing_token",
        "ck_learner_projection_attempt",
        "ck_learner_projection_status",
    ):
        op.drop_constraint(name, "learner_projection_jobs", type_="check")
    op.drop_index("ix_learner_projection_jobs_ready", table_name="learner_projection_jobs")
    for name in (
        "uq_learner_projection_sequence",
        "uq_learner_projection_revision",
        "uq_learner_projection_run",
        "uq_learner_projection_turn",
        "uq_learner_projection_command",
    ):
        op.drop_constraint(name, "learner_projection_jobs", type_="unique")
    op.drop_constraint(
        "fk_learner_projection_command", "learner_projection_jobs", type_="foreignkey"
    )
    for name in (
        "completed_at",
        "last_error_json",
        "result_json",
        "result_sha256",
        "request_sha256",
        "next_attempt_at",
        "lease_expires_at",
        "lease_owner",
        "fencing_token",
        "attempt",
        "status",
        "run_id",
        "turn_id",
        "session_id",
        "command_id",
    ):
        op.drop_column("learner_projection_jobs", name)

    op.drop_constraint("ck_workflow_job_status", "workflow_jobs", type_="check")
    op.create_check_constraint(
        "ck_workflow_job_status",
        "workflow_jobs",
        "status IN ('ACCEPTED','READY','CLAIMED','RUNNING','RETRY_WAIT',"
        "'SUCCEEDED','FAILED','CANCELLED','DEAD_LETTER')",
    )


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeError("legacy learner projection payload is not an object")
    return dict(value)


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _receipt_wire(row: Mapping[Any, Any], prefix: str) -> dict[str, Any]:
    completed_at = row[f"{prefix}_completed_at"]
    if completed_at.tzinfo is None:
        raise RuntimeError("legacy learner projection receipt timestamp is naive")
    return {
        "receipt_id": row[f"{prefix}_receipt_id"],
        "step_name": row[f"{prefix}_step_name"],
        "fencing_token": row[f"{prefix}_fencing_token"],
        "input_sha256": row[f"{prefix}_input_sha256"],
        "output_sha256": row[f"{prefix}_output_sha256"],
        "receipt_json": _mapping(row[f"{prefix}_receipt_json"]),
        "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
    }
