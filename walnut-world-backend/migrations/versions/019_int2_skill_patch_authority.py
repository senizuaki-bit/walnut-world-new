"""Add immutable Draft and Skill Patch provenance authority.

Revision ID: 019_int2_skill_patch_authority
Revises: 018_world_presentation_events
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from jsonschema.validators import validator_for
from sqlalchemy.dialects import postgresql

from walnut_backend.adapters.postgres.models import (
    command_record_data,
    command_record_from_data,
)

revision = "019_int2_skill_patch_authority"
down_revision = "018_world_presentation_events"
branch_labels = None
depends_on = None


def _iso_datetime(value: object) -> str:
    if not isinstance(value, datetime):
        raise RuntimeError("legacy timestamp is not a datetime")
    if value.tzinfo is None:
        raise RuntimeError("legacy timestamp is not timezone-aware")
    # Frozen v0.4 resources use datetime.isoformat() without truncating the
    # PostgreSQL clock value.  Preserve all stored fractional digits so the
    # migration validates the bytes that production readers validate.
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def cast_datetime(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RuntimeError("legacy timestamp is not timezone-aware")
    return value


def _iso_or_none(value: object) -> str | None:
    return None if value is None else _iso_datetime(value)


def _wire_timestamp_matches(value: object, expected: object) -> bool:
    """Mirror runtime readers, which compare parsed aware timestamps."""

    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed == cast_datetime(expected)


def _scoped_identifier(prefix: str, *parts: str) -> str:
    framed = "\x00".join((prefix, *parts)).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(framed).hexdigest()[:24]}"


def upgrade() -> None:
    op.create_table(
        "product_skill_draft_revisions",
        sa.Column("draft_revision_row_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("parent_revision_row_id", sa.BigInteger()),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("draft_id", sa.String(length=128), nullable=False),
        sa.Column("skill_id", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("draft_sha256", sa.String(length=64), nullable=False),
        sa.Column("entrypoint", sa.String(length=240), nullable=False),
        sa.Column("source_bundle_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("patch_id", sa.String(length=128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("draft_json", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "draft_sha256 ~ '^[a-f0-9]{64}$' AND source_bundle_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_product_draft_revision_hashes",
        ),
        sa.CheckConstraint(
            "(source_kind = 'STUDENT' AND patch_id IS NULL) OR "
            "(source_kind = 'SKILL_PATCH' AND patch_id IS NOT NULL)",
            name="ck_product_draft_revision_source",
        ),
        sa.PrimaryKeyConstraint("draft_revision_row_id"),
        sa.ForeignKeyConstraint(
            ["parent_revision_row_id"],
            ["product_skill_draft_revisions.draft_revision_row_id"],
            name="fk_product_draft_revision_parent",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "session_id",
            "draft_id",
            "revision",
            name="uq_product_draft_revision_identity",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "session_id",
            "draft_id",
            "revision",
            "draft_sha256",
            name="uq_product_draft_revision_authority",
        ),
        sa.UniqueConstraint(
            "draft_revision_row_id",
            "patch_id",
            name="uq_product_draft_revision_patch_pair",
        ),
    )
    op.create_index(
        "ix_product_draft_revision_authority",
        "product_skill_draft_revisions",
        ["tenant_id", "actor_id", "session_id", "skill_id", "revision"],
    )
    _backfill_current_draft_revisions()

    op.create_table(
        "product_skill_patch_requests",
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("turn_id", sa.String(length=128), nullable=False),
        sa.Column("command_id", sa.String(length=128), nullable=False),
        sa.Column("requested_interaction_id", sa.String(length=128), nullable=False),
        sa.Column("authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("proposal_id", sa.String(length=128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "authority_sha256 ~ '^[a-f0-9]{64}$' AND status IN ('PENDING','PROPOSED')",
            name="ck_product_skill_patch_request_authority",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "session_id", "requested_interaction_id"],
            [
                "product_agent_interactions.tenant_id",
                "product_agent_interactions.session_id",
                "product_agent_interactions.interaction_id",
            ],
            name="fk_product_skill_patch_request_interaction",
        ),
        sa.PrimaryKeyConstraint("request_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "requested_interaction_id",
            name="uq_product_skill_patch_request_selected_failure",
        ),
        sa.UniqueConstraint(
            "tenant_id", "command_id", name="uq_product_skill_patch_request_command"
        ),
    )
    op.create_table(
        "product_skill_patch_proposals",
        sa.Column("patch_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("interaction_id", sa.String(length=128), nullable=False),
        sa.Column("requested_interaction_id", sa.String(length=128), nullable=False),
        sa.Column("turn_id", sa.String(length=128), nullable=False),
        sa.Column("request_command_id", sa.String(length=128), nullable=False),
        sa.Column("requested_interaction_revision", sa.Integer(), nullable=False),
        sa.Column("requested_interaction_sequence", sa.Integer(), nullable=False),
        sa.Column("requested_failure_suffix_end_sequence", sa.Integer(), nullable=False),
        sa.Column("failed_turn_id", sa.String(length=128), nullable=False),
        sa.Column("failed_command_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("world_id", sa.String(length=128), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("failure_key", sa.String(length=128), nullable=False),
        sa.Column("feedback_event_id", sa.String(length=128), nullable=False),
        sa.Column("projection_receipt_id", sa.String(length=128), nullable=False),
        sa.Column("draft_id", sa.String(length=128), nullable=False),
        sa.Column("skill_id", sa.String(length=128), nullable=False),
        sa.Column("base_draft_revision_row_id", sa.BigInteger(), nullable=False),
        sa.Column("base_draft_revision", sa.Integer(), nullable=False),
        sa.Column("base_draft_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_bundle_sha256", sa.String(length=64), nullable=False),
        sa.Column("entrypoint", sa.String(length=240), nullable=False),
        sa.Column("entrypoint_sha256", sa.String(length=64), nullable=False),
        sa.Column("previous_content_sha256", sa.String(length=64), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("result_draft_sha256", sa.String(length=64), nullable=False),
        sa.Column("patch_sha256", sa.String(length=64), nullable=False),
        sa.Column("agent_proposal_id", sa.String(length=128), nullable=False),
        sa.Column("agent_proposal_sha256", sa.String(length=64), nullable=False),
        sa.Column("failed_build_id", sa.String(length=128), nullable=False),
        sa.Column("failed_run_id", sa.String(length=128), nullable=False),
        sa.Column("proposal_json", postgresql.JSONB(), nullable=False),
        sa.Column("agent_proposal_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "base_draft_revision >= 1 AND requested_interaction_revision >= 1 "
            "AND requested_interaction_sequence >= 1 "
            "AND requested_failure_suffix_end_sequence = requested_interaction_sequence "
            "AND failure_count >= 4 "
            "AND base_draft_sha256 ~ '^[a-f0-9]{64}$' "
            "AND source_bundle_sha256 ~ '^[a-f0-9]{64}$' "
            "AND entrypoint_sha256 ~ '^[a-f0-9]{64}$' "
            "AND previous_content_sha256 ~ '^[a-f0-9]{64}$' "
            "AND content_sha256 ~ '^[a-f0-9]{64}$' "
            "AND result_draft_sha256 ~ '^[a-f0-9]{64}$' "
            "AND patch_sha256 ~ '^[a-f0-9]{64}$' "
            "AND agent_proposal_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_product_skill_patch_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["base_draft_revision_row_id"],
            ["product_skill_draft_revisions.draft_revision_row_id"],
            name="fk_product_skill_patch_base_draft",
        ),
        sa.ForeignKeyConstraint(
            ["failed_build_id"],
            ["skill_builds.build_id"],
            name="fk_product_skill_patch_failed_build",
        ),
        sa.ForeignKeyConstraint(
            ["failed_run_id"],
            ["game_runs.run_id"],
            name="fk_product_skill_patch_failed_run",
        ),
        sa.PrimaryKeyConstraint("patch_id"),
        sa.UniqueConstraint(
            "tenant_id", "interaction_id", name="uq_product_skill_patch_interaction"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "requested_interaction_id",
            name="uq_product_skill_patch_selected_failure",
        ),
        sa.UniqueConstraint(
            "tenant_id", "patch_id", "patch_sha256", name="uq_product_skill_patch_authority"
        ),
        sa.UniqueConstraint(
            "patch_id",
            "base_draft_revision_row_id",
            name="uq_product_skill_patch_base_pair",
        ),
    )
    op.create_table(
        "product_skill_patch_evidence",
        sa.Column("patch_id", sa.String(length=128), nullable=False),
        sa.Column("evidence_id", sa.String(length=128), nullable=False),
        sa.Column("evidence_type", sa.String(length=64), nullable=False),
        sa.Column("evidence_sha256", sa.String(length=64), nullable=False),
        sa.Column("evidence_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_ref_json", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "evidence_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_product_skill_patch_evidence_hash",
        ),
        sa.ForeignKeyConstraint(
            ["patch_id"],
            ["product_skill_patch_proposals.patch_id"],
            name="fk_product_skill_patch_evidence_proposal",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_id"],
            ["game_evidence.evidence_id"],
            name="fk_product_skill_patch_evidence_row",
        ),
        sa.PrimaryKeyConstraint("patch_id", "evidence_id"),
    )
    op.create_table(
        "product_skill_patch_decisions",
        sa.Column("decision_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("patch_id", sa.String(length=128), nullable=False),
        sa.Column("interaction_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("draft_id", sa.String(length=128), nullable=False),
        sa.Column("base_draft_revision_row_id", sa.BigInteger(), nullable=False),
        sa.Column("accepted_draft_revision_row_id", sa.BigInteger()),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=96)),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("receipt_json", postgresql.JSONB(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(decision = 'ACCEPT' AND reason_code IS NULL "
            "AND accepted_draft_revision_row_id IS NOT NULL) OR "
            "(decision = 'REJECT' AND reason_code IS NOT NULL "
            "AND accepted_draft_revision_row_id IS NULL)",
            name="ck_product_skill_patch_decision_terminal",
        ),
        sa.CheckConstraint(
            "request_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_product_skill_patch_decision_request_hash",
        ),
        sa.ForeignKeyConstraint(
            ["patch_id"],
            ["product_skill_patch_proposals.patch_id"],
            name="fk_product_skill_patch_decision_proposal",
        ),
        sa.ForeignKeyConstraint(
            ["patch_id", "base_draft_revision_row_id"],
            [
                "product_skill_patch_proposals.patch_id",
                "product_skill_patch_proposals.base_draft_revision_row_id",
            ],
            name="fk_product_skill_patch_decision_proposal_base",
        ),
        sa.ForeignKeyConstraint(
            ["base_draft_revision_row_id"],
            ["product_skill_draft_revisions.draft_revision_row_id"],
            name="fk_product_skill_patch_decision_base_draft",
        ),
        sa.ForeignKeyConstraint(
            ["accepted_draft_revision_row_id", "patch_id"],
            [
                "product_skill_draft_revisions.draft_revision_row_id",
                "product_skill_draft_revisions.patch_id",
            ],
            name="fk_product_skill_patch_decision_accepted_draft",
        ),
        sa.PrimaryKeyConstraint("decision_id"),
        sa.UniqueConstraint("patch_id", name="uq_product_skill_patch_terminal_decision"),
        sa.UniqueConstraint(
            "accepted_draft_revision_row_id",
            name="uq_product_skill_patch_accepted_draft",
        ),
        sa.UniqueConstraint("decision_id", "patch_id", name="uq_product_skill_patch_decision_pair"),
        sa.UniqueConstraint(
            "decision_id",
            "patch_id",
            "accepted_draft_revision_row_id",
            name="uq_product_skill_patch_decision_accepted_triple",
        ),
    )
    with op.batch_alter_table("product_patch_decision_receipts") as batch:
        batch.add_column(sa.Column("decision_id", sa.String(length=128)))
        batch.add_column(sa.Column("patch_id", sa.String(length=128)))
        batch.add_column(sa.Column("draft_revision_row_id", sa.BigInteger()))
        batch.create_foreign_key(
            "fk_product_patch_receipt_decision",
            "product_skill_patch_decisions",
            ["decision_id"],
            ["decision_id"],
        )
        batch.create_foreign_key(
            "fk_product_patch_receipt_proposal",
            "product_skill_patch_proposals",
            ["patch_id"],
            ["patch_id"],
        )
        batch.create_foreign_key(
            "fk_product_patch_receipt_draft_revision",
            "product_skill_draft_revisions",
            ["draft_revision_row_id"],
            ["draft_revision_row_id"],
        )

    op.create_table(
        "product_draft_revision_assistance",
        sa.Column("draft_revision_row_id", sa.BigInteger(), nullable=False),
        sa.Column("origin_accepted_revision_row_id", sa.BigInteger(), nullable=False),
        sa.Column("patch_id", sa.String(length=128), nullable=False),
        sa.Column("patch_decision_id", sa.String(length=128), nullable=False),
        sa.Column("inherited", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["draft_revision_row_id"],
            ["product_skill_draft_revisions.draft_revision_row_id"],
            name="fk_product_draft_assistance_revision",
        ),
        sa.ForeignKeyConstraint(
            ["origin_accepted_revision_row_id"],
            ["product_skill_draft_revisions.draft_revision_row_id"],
            name="fk_product_draft_assistance_origin",
        ),
        sa.ForeignKeyConstraint(
            ["patch_id"],
            ["product_skill_patch_proposals.patch_id"],
            name="fk_product_draft_assistance_patch",
        ),
        sa.ForeignKeyConstraint(
            ["patch_decision_id", "patch_id", "origin_accepted_revision_row_id"],
            [
                "product_skill_patch_decisions.decision_id",
                "product_skill_patch_decisions.patch_id",
                "product_skill_patch_decisions.accepted_draft_revision_row_id",
            ],
            name="fk_product_draft_assistance_accepted_decision",
        ),
        sa.PrimaryKeyConstraint("draft_revision_row_id"),
        sa.UniqueConstraint(
            "draft_revision_row_id",
            "origin_accepted_revision_row_id",
            "patch_id",
            "patch_decision_id",
            name="uq_product_draft_assistance_authority",
        ),
    )
    op.create_table(
        "int2_legacy_build_markers",
        sa.Column("marker_id", sa.String(length=128), nullable=False),
        sa.Column("build_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("build_authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("marker_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "build_authority_sha256 ~ '^[a-f0-9]{64}$' AND marker_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_int2_legacy_build_marker_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["build_id"],
            ["skill_builds.build_id"],
            name="fk_int2_legacy_build_marker_build",
        ),
        sa.PrimaryKeyConstraint("marker_id"),
        sa.UniqueConstraint("build_id", name="uq_int2_legacy_build_marker_build"),
        sa.UniqueConstraint(
            "marker_id",
            "build_id",
            "tenant_id",
            "actor_id",
            "build_authority_sha256",
            name="uq_int2_legacy_build_marker_authority",
        ),
    )
    op.create_table(
        "skill_build_provenance",
        sa.Column("build_id", sa.String(length=128), nullable=False),
        sa.Column("provenance_kind", sa.String(length=32), nullable=False),
        sa.Column("legacy_marker_id", sa.String(length=128)),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("build_request_sha256", sa.String(length=64), nullable=False),
        sa.Column("command_receipt_id", sa.Integer(), nullable=False),
        sa.Column("command_receipt_authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("workflow_job_id", sa.String(length=128), nullable=False),
        sa.Column("workflow_request_sha256", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=128)),
        sa.Column("draft_id", sa.String(length=128)),
        sa.Column("skill_id", sa.String(length=128), nullable=False),
        sa.Column("draft_revision_row_id", sa.BigInteger()),
        sa.Column("draft_revision", sa.Integer()),
        sa.Column("draft_sha256", sa.String(length=64)),
        sa.Column("source_bundle_sha256", sa.String(length=64), nullable=False),
        sa.Column("origin_accepted_revision_row_id", sa.BigInteger()),
        sa.Column("patch_id", sa.String(length=128)),
        sa.Column("patch_decision_id", sa.String(length=128)),
        sa.Column("assistance_authority", sa.String(length=32), nullable=False),
        sa.Column("authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "build_request_sha256 ~ '^[a-f0-9]{64}$' "
            "AND command_receipt_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND workflow_request_sha256 ~ '^[a-f0-9]{64}$' "
            "AND source_bundle_sha256 ~ '^[a-f0-9]{64}$' "
            "AND authority_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_skill_build_provenance_hashes",
        ),
        sa.CheckConstraint(
            "(provenance_kind = 'LEGACY_V04' AND assistance_authority = 'NONE' "
            "AND legacy_marker_id IS NOT NULL "
            "AND session_id IS NULL AND draft_id IS NULL "
            "AND draft_revision_row_id IS NULL AND draft_revision IS NULL "
            "AND draft_sha256 IS NULL AND origin_accepted_revision_row_id IS NULL "
            "AND patch_id IS NULL AND patch_decision_id IS NULL) OR "
            "(provenance_kind = 'IMMUTABLE_DRAFT' AND session_id IS NOT NULL "
            "AND legacy_marker_id IS NULL "
            "AND draft_id IS NOT NULL AND draft_revision_row_id IS NOT NULL "
            "AND draft_revision >= 1 AND draft_sha256 ~ '^[a-f0-9]{64}$' AND "
            "((assistance_authority = 'NONE' "
            "AND origin_accepted_revision_row_id IS NULL AND patch_id IS NULL "
            "AND patch_decision_id IS NULL) OR "
            "(assistance_authority = 'SKILL_PATCH' "
            "AND origin_accepted_revision_row_id IS NOT NULL "
            "AND patch_id IS NOT NULL AND patch_decision_id IS NOT NULL)))",
            name="ck_skill_build_provenance_assistance",
        ),
        sa.ForeignKeyConstraint(
            ["build_id"],
            ["skill_builds.build_id"],
            name="fk_skill_build_provenance_build",
        ),
        sa.ForeignKeyConstraint(
            ["command_receipt_id"],
            ["idempotency_receipts.receipt_id"],
            name="fk_skill_build_provenance_command_receipt",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_job_id"],
            ["workflow_jobs.tenant_id", "workflow_jobs.job_id"],
            name="fk_skill_build_provenance_workflow_job",
        ),
        sa.ForeignKeyConstraint(
            [
                "legacy_marker_id",
                "build_id",
                "tenant_id",
                "actor_id",
                "authority_sha256",
            ],
            [
                "int2_legacy_build_markers.marker_id",
                "int2_legacy_build_markers.build_id",
                "int2_legacy_build_markers.tenant_id",
                "int2_legacy_build_markers.actor_id",
                "int2_legacy_build_markers.build_authority_sha256",
            ],
            name="fk_skill_build_provenance_legacy_marker",
        ),
        sa.ForeignKeyConstraint(
            ["draft_revision_row_id"],
            ["product_skill_draft_revisions.draft_revision_row_id"],
            name="fk_skill_build_provenance_draft",
        ),
        sa.ForeignKeyConstraint(
            ["patch_id"],
            ["product_skill_patch_proposals.patch_id"],
            name="fk_skill_build_provenance_patch",
        ),
        sa.ForeignKeyConstraint(
            ["patch_decision_id", "patch_id", "origin_accepted_revision_row_id"],
            [
                "product_skill_patch_decisions.decision_id",
                "product_skill_patch_decisions.patch_id",
                "product_skill_patch_decisions.accepted_draft_revision_row_id",
            ],
            name="fk_skill_build_provenance_accepted_decision",
        ),
        sa.ForeignKeyConstraint(
            [
                "draft_revision_row_id",
                "origin_accepted_revision_row_id",
                "patch_id",
                "patch_decision_id",
            ],
            [
                "product_draft_revision_assistance.draft_revision_row_id",
                "product_draft_revision_assistance.origin_accepted_revision_row_id",
                "product_draft_revision_assistance.patch_id",
                "product_draft_revision_assistance.patch_decision_id",
            ],
            name="fk_skill_build_provenance_draft_assistance",
        ),
        sa.PrimaryKeyConstraint("build_id"),
        sa.UniqueConstraint(
            "build_id",
            "authority_sha256",
            name="uq_skill_build_provenance_authority",
        ),
    )
    op.create_table(
        "skill_certification_provenance",
        sa.Column("certification_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("build_id", sa.String(length=128), nullable=False),
        sa.Column("build_authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("build_request_sha256", sa.String(length=64), nullable=False),
        sa.Column("workflow_job_id", sa.String(length=128), nullable=False),
        sa.Column("workflow_request_sha256", sa.String(length=64), nullable=False),
        sa.Column("workflow_job_sha256", sa.String(length=64), nullable=False),
        sa.Column("command_authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("build_receipt_id", sa.String(length=128), nullable=False),
        sa.Column("build_receipt_sha256", sa.String(length=64), nullable=False),
        sa.Column("build_receipt_authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("policy_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("certification_sha256", sa.String(length=64), nullable=False),
        sa.Column("authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "build_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND build_request_sha256 ~ '^[a-f0-9]{64}$' "
            "AND workflow_request_sha256 ~ '^[a-f0-9]{64}$' "
            "AND workflow_job_sha256 ~ '^[a-f0-9]{64}$' "
            "AND command_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND build_receipt_sha256 ~ '^[a-f0-9]{64}$' "
            "AND build_receipt_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND policy_sha256 ~ '^[a-f0-9]{64}$' "
            "AND artifact_sha256 ~ '^[a-f0-9]{64}$' "
            "AND artifact_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND certification_sha256 ~ '^[a-f0-9]{64}$' "
            "AND authority_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_skill_certification_provenance_authority",
        ),
        sa.ForeignKeyConstraint(
            ["certification_id"],
            ["skill_certifications.certification_id"],
            name="fk_skill_certification_provenance_certification",
        ),
        sa.ForeignKeyConstraint(
            ["build_id", "build_authority_sha256"],
            ["skill_build_provenance.build_id", "skill_build_provenance.authority_sha256"],
            name="fk_skill_certification_provenance_build",
        ),
        sa.ForeignKeyConstraint(
            ["build_receipt_id"],
            ["job_step_receipts.receipt_id"],
            name="fk_skill_certification_provenance_receipt",
        ),
        sa.PrimaryKeyConstraint("certification_id"),
        sa.UniqueConstraint(
            "certification_id",
            "authority_sha256",
            name="uq_skill_certification_provenance_authority",
        ),
    )
    op.create_table(
        "skill_build_terminal_authority",
        sa.Column("build_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("build_authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("terminal_status", sa.String(length=32), nullable=False),
        sa.Column("command_id", sa.String(length=128), nullable=False),
        sa.Column("command_authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("workflow_job_id", sa.String(length=128), nullable=False),
        sa.Column("workflow_job_sha256", sa.String(length=64), nullable=False),
        sa.Column("terminal_receipt_id", sa.String(length=128), nullable=False),
        sa.Column("terminal_receipt_authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("certification_id", sa.String(length=128)),
        sa.Column("certification_authority_sha256", sa.String(length=64)),
        sa.Column("authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "build_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND command_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND workflow_job_sha256 ~ '^[a-f0-9]{64}$' "
            "AND terminal_receipt_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND authority_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_skill_build_terminal_hashes",
        ),
        sa.CheckConstraint(
            "(terminal_status = 'REJECTED' AND certification_id IS NULL "
            "AND certification_authority_sha256 IS NULL) OR "
            "(terminal_status = 'CERTIFIED' AND certification_id IS NOT NULL "
            "AND certification_authority_sha256 ~ '^[a-f0-9]{64}$')",
            name="ck_skill_build_terminal_status",
        ),
        sa.ForeignKeyConstraint(
            ["build_id", "build_authority_sha256"],
            ["skill_build_provenance.build_id", "skill_build_provenance.authority_sha256"],
            name="fk_skill_build_terminal_build",
        ),
        sa.ForeignKeyConstraint(
            ["command_id"],
            ["commands.command_id"],
            name="fk_skill_build_terminal_command",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_job_id"],
            ["workflow_jobs.tenant_id", "workflow_jobs.job_id"],
            name="fk_skill_build_terminal_workflow",
        ),
        sa.ForeignKeyConstraint(
            ["terminal_receipt_id"],
            ["job_step_receipts.receipt_id"],
            name="fk_skill_build_terminal_receipt",
        ),
        sa.ForeignKeyConstraint(
            ["certification_id", "certification_authority_sha256"],
            [
                "skill_certification_provenance.certification_id",
                "skill_certification_provenance.authority_sha256",
            ],
            name="fk_skill_build_terminal_certification",
        ),
        sa.PrimaryKeyConstraint("build_id"),
        sa.UniqueConstraint(
            "build_id", "authority_sha256", name="uq_skill_build_terminal_authority"
        ),
    )
    op.create_table(
        "skill_activation_provenance",
        sa.Column("activation_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("build_id", sa.String(length=128), nullable=False),
        sa.Column("build_authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("certification_id", sa.String(length=128), nullable=False),
        sa.Column("certification_sha256", sa.String(length=64), nullable=False),
        sa.Column("certification_authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("registry_revision", sa.BigInteger(), nullable=False),
        sa.Column("activation_sha256", sa.String(length=64), nullable=False),
        sa.Column("launch_authority_id", sa.String(length=128), nullable=False),
        sa.Column("entry_sha256", sa.String(length=64), nullable=False),
        sa.Column("workflow_job_id", sa.String(length=128), nullable=False),
        sa.Column("workflow_request_sha256", sa.String(length=64), nullable=False),
        sa.Column("workflow_job_sha256", sa.String(length=64), nullable=False),
        sa.Column("activation_receipt_id", sa.String(length=128), nullable=False),
        sa.Column("activation_receipt_sha256", sa.String(length=64), nullable=False),
        sa.Column("authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "activation_sha256 ~ '^[a-f0-9]{64}$' "
            "AND certification_sha256 ~ '^[a-f0-9]{64}$' "
            "AND certification_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND artifact_sha256 ~ '^[a-f0-9]{64}$' "
            "AND artifact_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND build_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND entry_sha256 ~ '^[a-f0-9]{64}$' "
            "AND workflow_request_sha256 ~ '^[a-f0-9]{64}$' "
            "AND workflow_job_sha256 ~ '^[a-f0-9]{64}$' "
            "AND activation_receipt_sha256 ~ '^[a-f0-9]{64}$' "
            "AND authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND registry_revision >= 1",
            name="ck_skill_activation_provenance_authority",
        ),
        sa.ForeignKeyConstraint(
            ["certification_id", "certification_authority_sha256"],
            [
                "skill_certification_provenance.certification_id",
                "skill_certification_provenance.authority_sha256",
            ],
            name="fk_skill_activation_provenance_certification",
        ),
        sa.ForeignKeyConstraint(
            ["activation_id"],
            ["skill_activations.activation_id"],
            name="fk_skill_activation_provenance_activation",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "launch_authority_id"],
            ["launch_authorities.tenant_id", "launch_authorities.authority_id"],
            name="fk_skill_activation_provenance_launch_authority",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_job_id"],
            ["workflow_jobs.job_id"],
            name="fk_skill_activation_provenance_workflow_job",
        ),
        sa.ForeignKeyConstraint(
            ["activation_receipt_id"],
            ["job_step_receipts.receipt_id"],
            name="fk_skill_activation_provenance_receipt",
        ),
        sa.ForeignKeyConstraint(
            ["build_id", "build_authority_sha256"],
            ["skill_build_provenance.build_id", "skill_build_provenance.authority_sha256"],
            name="fk_skill_activation_provenance_build",
        ),
        sa.PrimaryKeyConstraint("activation_id"),
        sa.UniqueConstraint(
            "activation_id",
            "authority_sha256",
            name="uq_skill_activation_provenance_authority",
        ),
    )
    op.create_table(
        "skill_run_provenance",
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("build_id", sa.String(length=128), nullable=False),
        sa.Column("provenance_kind", sa.String(length=32), nullable=False),
        sa.Column("build_authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("activation_id", sa.String(length=128)),
        sa.Column("activation_sha256", sa.String(length=64)),
        sa.Column("activation_authority_sha256", sa.String(length=64)),
        sa.Column("registry_revision", sa.BigInteger()),
        sa.Column("certification_id", sa.String(length=128), nullable=False),
        sa.Column("certification_sha256", sa.String(length=64), nullable=False),
        sa.Column("certification_authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("draft_revision_row_id", sa.BigInteger()),
        sa.Column("draft_sha256", sa.String(length=64)),
        sa.Column("assistance_authority", sa.String(length=32), nullable=False),
        sa.Column("authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "build_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND certification_sha256 ~ '^[a-f0-9]{64}$' "
            "AND certification_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND artifact_sha256 ~ '^[a-f0-9]{64}$' "
            "AND artifact_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND authority_sha256 ~ '^[a-f0-9]{64}$' AND "
            "((provenance_kind = 'LEGACY_V04' AND draft_revision_row_id IS NULL "
            "AND draft_sha256 IS NULL AND assistance_authority = 'NONE' AND "
            "activation_id IS NULL AND activation_sha256 IS NULL "
            "AND activation_authority_sha256 IS NULL "
            "AND registry_revision IS NULL AND certification_id IS NOT NULL) OR "
            "(provenance_kind = 'LEGACY_V04_ACTIVE' "
            "AND draft_revision_row_id IS NULL AND draft_sha256 IS NULL "
            "AND assistance_authority = 'NONE' AND activation_id IS NOT NULL "
            "AND activation_sha256 ~ '^[a-f0-9]{64}$' "
            "AND activation_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND registry_revision >= 1 AND certification_id IS NOT NULL) OR "
            "(provenance_kind = 'IMMUTABLE_DRAFT' "
            "AND draft_revision_row_id IS NOT NULL "
            "AND draft_sha256 ~ '^[a-f0-9]{64}$' "
            "AND activation_id IS NOT NULL "
            "AND activation_sha256 ~ '^[a-f0-9]{64}$' "
            "AND activation_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND registry_revision >= 1 AND certification_id IS NOT NULL "
            "AND assistance_authority IN ('NONE','SKILL_PATCH')))",
            name="ck_skill_run_provenance_authority",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["game_runs.run_id"],
            name="fk_skill_run_provenance_run",
        ),
        sa.ForeignKeyConstraint(
            ["build_id", "build_authority_sha256"],
            [
                "skill_build_provenance.build_id",
                "skill_build_provenance.authority_sha256",
            ],
            name="fk_skill_run_provenance_build",
        ),
        sa.ForeignKeyConstraint(
            ["draft_revision_row_id"],
            ["product_skill_draft_revisions.draft_revision_row_id"],
            name="fk_skill_run_provenance_draft",
        ),
        sa.ForeignKeyConstraint(
            ["activation_id", "activation_authority_sha256"],
            [
                "skill_activation_provenance.activation_id",
                "skill_activation_provenance.authority_sha256",
            ],
            name="fk_skill_run_provenance_activation",
        ),
        sa.PrimaryKeyConstraint("run_id"),
    )
    _backfill_legacy_build_and_run_provenance()
    _assert_int2_provenance_completeness()
    _backfill_legacy_learner_objectives()
    _install_int2_append_only_guards()


def downgrade() -> None:
    _validate_build_seals_for_downgrade()
    _drop_int2_append_only_guards()
    _downgrade_legacy_learner_objectives()
    _downgrade_legacy_activation_receipts()
    op.execute(
        "UPDATE workflow_jobs SET job_json = "
        "job_json - 'build_provenance_sha256' - 'certification_sha256' "
        "- 'artifact_authority_sha256' "
        "WHERE operation IN ('CREATE_SKILL_BUILD','ACTIVATE_SKILL_VERSION')"
    )
    op.drop_table("skill_run_provenance")
    op.drop_table("skill_activation_provenance")
    op.drop_table("skill_build_terminal_authority")
    op.drop_table("skill_certification_provenance")
    op.drop_table("skill_build_provenance")
    op.drop_table("int2_legacy_build_markers")
    op.drop_table("product_draft_revision_assistance")
    with op.batch_alter_table("product_patch_decision_receipts") as batch:
        batch.drop_constraint("fk_product_patch_receipt_draft_revision", type_="foreignkey")
        batch.drop_constraint("fk_product_patch_receipt_proposal", type_="foreignkey")
        batch.drop_constraint("fk_product_patch_receipt_decision", type_="foreignkey")
        batch.drop_column("draft_revision_row_id")
        batch.drop_column("patch_id")
        batch.drop_column("decision_id")
    op.drop_table("product_skill_patch_decisions")
    op.drop_table("product_skill_patch_evidence")
    op.drop_table("product_skill_patch_proposals")
    op.drop_table("product_skill_patch_requests")
    op.drop_index(
        "ix_product_draft_revision_authority",
        table_name="product_skill_draft_revisions",
    )
    op.drop_table("product_skill_draft_revisions")


def _validate_build_seals_for_downgrade() -> None:
    """Never erase an INT2 Build seal after its mutable authority drifted."""

    connection = op.get_bind()
    provenances = list(
        connection.execute(
            sa.text("SELECT * FROM skill_build_provenance ORDER BY build_id")
        ).mappings()
    )
    for raw_provenance in provenances:
        provenance = dict(raw_provenance)
        if provenance["provenance_kind"] != "LEGACY_V04":
            raise RuntimeError("INT2 Build provenance is not downgrade-compatible")
        build = (
            connection.execute(
                sa.text(
                    "SELECT build_id,tenant_id,actor_id,command_id,skill_id,status,terminal,"
                    "created_at,updated_at,build_json,request_json FROM skill_builds "
                    "WHERE build_id=:build_id AND tenant_id=:tenant_id AND actor_id=:actor_id"
                ),
                provenance,
            )
            .mappings()
            .one_or_none()
        )
        receipt = (
            connection.execute(
                sa.text(
                    "SELECT receipt_id,tenant_id,actor_id,operation,idempotency_key,"
                    "request_sha256,command_id,accepted_at FROM idempotency_receipts "
                    "WHERE receipt_id=:command_receipt_id AND tenant_id=:tenant_id "
                    "AND actor_id=:actor_id AND operation='CREATE_SKILL_BUILD'"
                ),
                provenance,
            )
            .mappings()
            .one_or_none()
        )
        workflow = (
            connection.execute(
                sa.text(
                    "SELECT job_id,tenant_id,command_id,operation,subject_type,subject_id,"
                    "phase,status,attempt,fencing_token,lease_owner,lease_expires_at,"
                    "next_attempt_at,request_sha256,job_json,last_error_json,created_at,updated_at "
                    "FROM workflow_jobs WHERE tenant_id=:tenant_id AND job_id=:workflow_job_id"
                ),
                provenance,
            )
            .mappings()
            .one_or_none()
        )
        build_command = (
            connection.execute(
                sa.text(
                    "SELECT command_id,tenant_id,actor_id,command_type,status,revision,terminal,"
                    "accepted_at,updated_at,record_json FROM commands "
                    "WHERE command_id=:command_id AND tenant_id=:tenant_id "
                    "AND actor_id=:actor_id"
                ),
                dict(build) if build is not None else provenance,
            )
            .mappings()
            .one_or_none()
        )
        terminal_receipts = list(
            connection.execute(
                sa.text(
                    "SELECT receipt_id,tenant_id,job_id,step_name,fencing_token,"
                    "input_sha256,output_sha256,receipt_json,completed_at "
                    "FROM job_step_receipts WHERE tenant_id=:tenant_id "
                    "AND job_id=:workflow_job_id "
                    "AND step_name IN ('BUILD_CERTIFIED','BUILD_REJECTED')"
                ),
                provenance,
            ).mappings()
        )
        marker = (
            connection.execute(
                sa.text(
                    "SELECT marker_id,build_id,tenant_id,actor_id,build_authority_sha256,"
                    "marker_sha256 FROM int2_legacy_build_markers "
                    "WHERE marker_id=:legacy_marker_id"
                ),
                provenance,
            )
            .mappings()
            .one_or_none()
        )
        request = build["request_json"] if build is not None else None
        source = request.get("source_bundle") if isinstance(request, Mapping) else None
        if (
            build is None
            or receipt is None
            or workflow is None
            or build_command is None
            or marker is None
            or not isinstance(source, Mapping)
            or _canonical_sha256(request) != provenance["build_request_sha256"]
            or _source_bundle_sha256(source) != provenance["source_bundle_sha256"]
            or receipt["command_id"] != build["command_id"]
            or receipt["request_sha256"] != provenance["workflow_request_sha256"]
            or _canonical_sha256(_legacy_build_command_receipt_authority(dict(receipt)))
            != provenance["command_receipt_authority_sha256"]
            or workflow["job_id"]
            != _scoped_identifier("job", str(provenance["tenant_id"]), str(build["command_id"]))
            or workflow["command_id"] != build["command_id"]
            or workflow["request_sha256"] != provenance["workflow_request_sha256"]
        ):
            raise RuntimeError("INT2 Build acceptance authority drifted before downgrade")
        _validate_legacy_build_terminal_authority(
            row=dict(build),
            build=build["build_json"],
            command=dict(build_command),
            job=dict(workflow),
            receipts=[dict(item) for item in terminal_receipts],
            source_sha256=provenance["source_bundle_sha256"],
        )
        expected_provenance_sha256 = _canonical_sha256(
            _legacy_build_authority(provenance, provenance["source_bundle_sha256"])
        )
        marker_authority = {
            "authority_type": "INT2_LEGACY_BUILD_MARKER",
            "authority_version": "1.0.0",
            "marker_id": marker["marker_id"],
            "build_id": marker["build_id"],
            "tenant_id": marker["tenant_id"],
            "actor_id": marker["actor_id"],
            "build_authority_sha256": marker["build_authority_sha256"],
        }
        if (
            provenance["authority_sha256"] != expected_provenance_sha256
            or marker["build_id"] != provenance["build_id"]
            or marker["tenant_id"] != provenance["tenant_id"]
            or marker["actor_id"] != provenance["actor_id"]
            or marker["build_authority_sha256"] != expected_provenance_sha256
            or marker["marker_sha256"] != _canonical_sha256(marker_authority)
        ):
            raise RuntimeError("INT2 Build acceptance seal drifted before downgrade")
        terminal = (
            connection.execute(
                sa.text("SELECT * FROM skill_build_terminal_authority WHERE build_id=:build_id"),
                provenance,
            )
            .mappings()
            .one_or_none()
        )
        if not bool(build["terminal"]):
            if build["status"] != "ACCEPTED" or terminal is not None:
                raise RuntimeError("INT2 Build terminal state drifted before downgrade")
            continue
        if terminal is None:
            raise RuntimeError("INT2 Build terminal seal disappeared before downgrade")
        terminal_command = (
            connection.execute(
                sa.text(
                    "SELECT command_id,tenant_id,actor_id,command_type,status,revision,terminal,"
                    "accepted_at,updated_at,record_json FROM commands "
                    "WHERE command_id=:command_id AND tenant_id=:tenant_id"
                ),
                dict(terminal),
            )
            .mappings()
            .one_or_none()
        )
        terminal_receipt = (
            connection.execute(
                sa.text(
                    "SELECT receipt_id,tenant_id,job_id,step_name,fencing_token,input_sha256,"
                    "output_sha256,receipt_json,completed_at FROM job_step_receipts "
                    "WHERE receipt_id=:terminal_receipt_id AND tenant_id=:tenant_id"
                ),
                dict(terminal),
            )
            .mappings()
            .one_or_none()
        )
        if terminal_command is None or terminal_receipt is None:
            raise RuntimeError("INT2 Build terminal authority disappeared before downgrade")
        expected_terminal = _legacy_build_terminal_authority(
            build=dict(build),
            build_authority_sha256=expected_provenance_sha256,
            command=dict(terminal_command),
            workflow=dict(workflow),
            receipt=dict(terminal_receipt),
            certification_id=terminal["certification_id"],
            certification_authority_sha256=terminal["certification_authority_sha256"],
        )
        if any(
            terminal[key] != value
            for key, value in expected_terminal.items()
            if key not in {"authority_type", "authority_version"}
        ) or terminal["authority_sha256"] != _canonical_sha256(expected_terminal):
            raise RuntimeError("INT2 Build terminal authority drifted before downgrade")
        if build["status"] == "CERTIFIED":
            launch, policy, evidence_rows = _validate_legacy_build_launch_policy_and_evidence(
                connection,
                row=dict(build),
                build=build["build_json"],
                request=build["request_json"],
            )
            artifacts = list(
                connection.execute(
                    sa.text(
                        "SELECT tenant_id,artifact_sha256,build_id,actor_id,content_hash,"
                        "skill_id,source_sha256,artifact_uri,metadata_json,created_at "
                        "FROM skill_artifacts WHERE tenant_id=:tenant_id "
                        "AND build_id=:build_id AND actor_id=:actor_id"
                    ),
                    dict(build),
                ).mappings()
            )
            certifications = list(
                connection.execute(
                    sa.text(
                        "SELECT certification_id,tenant_id,build_id,skill_id,"
                        "skill_version_id,artifact_sha256,actor_id,content_hash,"
                        "certification_sha256,certification_json,certified_at "
                        "FROM skill_certifications WHERE tenant_id=:tenant_id "
                        "AND build_id=:build_id AND actor_id=:actor_id"
                    ),
                    dict(build),
                ).mappings()
            )
            if len(artifacts) != 1 or len(certifications) != 1:
                raise RuntimeError(
                    "INT2 certified Build source authority drifted before downgrade"
                )
            _validate_legacy_certified_build_authority(
                row=dict(build),
                build=build["build_json"],
                request=build["request_json"],
                command=dict(build_command),
                job=dict(workflow),
                receipt=dict(terminal_receipt),
                launch=launch,
                policy=policy,
                artifact=dict(artifacts[0]),
                certification=dict(certifications[0]),
                evidence_rows=[dict(item) for item in evidence_rows],
                source_sha256=provenance["source_bundle_sha256"],
            )
            _validate_certification_seal_for_downgrade(
                connection,
                build=dict(build),
                build_request=build["request_json"],
                build_authority_sha256=expected_provenance_sha256,
                workflow=dict(workflow),
                command=dict(build_command),
                receipt=dict(terminal_receipt),
                artifact=dict(artifacts[0]),
                certification=dict(certifications[0]),
                expected_certification_id=terminal["certification_id"],
                expected_authority_sha256=terminal["certification_authority_sha256"],
            )


def _validate_certification_seal_for_downgrade(
    connection: sa.Connection,
    *,
    build: Mapping[str, object],
    build_request: Mapping[str, object],
    build_authority_sha256: str,
    workflow: Mapping[str, object],
    command: Mapping[str, object],
    receipt: Mapping[str, object],
    artifact: Mapping[str, object],
    certification: Mapping[str, object],
    expected_certification_id: object,
    expected_authority_sha256: object,
) -> None:
    sealed = (
        connection.execute(
            sa.text(
                "SELECT * FROM skill_certification_provenance "
                "WHERE certification_id=:certification_id"
            ),
            {"certification_id": expected_certification_id},
        )
        .mappings()
        .one_or_none()
    )
    certification_json = certification["certification_json"]
    if sealed is None or not isinstance(certification_json, Mapping):
        raise RuntimeError("INT2 Certification seal disappeared before downgrade")
    artifact_authority = {
        "tenant_id": artifact["tenant_id"],
        "actor_id": artifact["actor_id"],
        "content_hash": artifact["content_hash"],
        "build_id": artifact["build_id"],
        "skill_id": artifact["skill_id"],
        "artifact_sha256": artifact["artifact_sha256"],
        "source_sha256": artifact["source_sha256"],
        "artifact_uri": artifact["artifact_uri"],
        "metadata": artifact["metadata_json"],
    }
    expected = {
        "authority_type": "SKILL_CERTIFICATION_PROVENANCE",
        "authority_version": "1.0.0",
        "certification_id": certification["certification_id"],
        "tenant_id": build["tenant_id"],
        "actor_id": build["actor_id"],
        "build_id": build["build_id"],
        "build_authority_sha256": build_authority_sha256,
        "build_request_sha256": _canonical_sha256(build_request),
        "workflow_job_id": workflow["job_id"],
        "workflow_request_sha256": workflow["request_sha256"],
        "workflow_job_sha256": _canonical_sha256(
            _certification_workflow_job_authority(workflow)
        ),
        "command_authority_sha256": _canonical_sha256(
            _certification_command_authority(command)
        ),
        "build_receipt_id": receipt["receipt_id"],
        "build_receipt_sha256": receipt["output_sha256"],
        "build_receipt_authority_sha256": _canonical_sha256(
            _certification_receipt_authority(receipt)
        ),
        "policy_sha256": certification_json.get("policy_sha256"),
        "artifact_sha256": artifact["artifact_sha256"],
        "artifact_authority_sha256": _canonical_sha256(artifact_authority),
        "certification_sha256": certification["certification_sha256"],
    }
    if (
        expected_certification_id != certification["certification_id"]
        or expected_authority_sha256 != _canonical_sha256(expected)
        or sealed["authority_sha256"] != _canonical_sha256(expected)
        or any(
            sealed[key] != value
            for key, value in expected.items()
            if key not in {"authority_type", "authority_version"}
        )
    ):
        raise RuntimeError("INT2 Certification authority drifted before downgrade")


def _downgrade_legacy_activation_receipts() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT receipt.tenant_id,receipt.receipt_id,receipt.receipt_json "
            "FROM job_step_receipts AS receipt JOIN workflow_jobs AS job ON "
            "job.tenant_id=receipt.tenant_id AND job.job_id=receipt.job_id "
            "WHERE job.operation='ACTIVATE_SKILL_VERSION' "
            "AND receipt.step_name='REGISTRY_ACTIVATED'"
        )
    ).mappings()
    for row in rows:
        value = row["receipt_json"]
        if not isinstance(value, Mapping) or not {
            "certification_sha256",
            "artifact_authority_sha256",
            "build_provenance_sha256",
        }.issubset(value):
            raise RuntimeError("INT2 Activation receipt is not downgrade-compatible")
        restored = {
            key: item
            for key, item in value.items()
            if key
            not in {
                "certification_sha256",
                "artifact_authority_sha256",
                "build_provenance_sha256",
            }
        }
        connection.execute(
            sa.text(
                "UPDATE job_step_receipts SET receipt_json=CAST(:receipt AS jsonb),"
                "output_sha256=:output_sha256 WHERE tenant_id=:tenant_id "
                "AND receipt_id=:receipt_id"
            ),
            {
                **dict(row),
                "receipt": json.dumps(restored, ensure_ascii=False),
                "output_sha256": _canonical_sha256(restored),
            },
        )


def _backfill_legacy_build_and_run_provenance() -> None:
    """Label pre-cutover v0.4 authority without inventing historical Draft rows."""

    connection = op.get_bind()
    patch_receipts = connection.scalar(
        sa.text("SELECT count(*) FROM product_patch_decision_receipts")
    )
    dormant_interactions = connection.scalar(
        sa.text(
            "SELECT count(*) FROM product_agent_interactions "
            "WHERE interaction_json->>'skill_patch' IS NOT NULL "
            "OR interaction_json->>'patch_decision' IS NOT NULL"
        )
    )
    if int(patch_receipts or 0) != 0 or int(dormant_interactions or 0) != 0:
        raise RuntimeError(
            "pre-019 PatchDecision state cannot be proven because the v0.4 route was dormant"
        )
    builds = list(
        connection.execute(
            sa.text(
                "SELECT build_id, tenant_id, actor_id, command_id, skill_id, status, "
                "terminal, created_at, updated_at, build_json, request_json "
                "FROM skill_builds ORDER BY build_id"
            )
        ).mappings()
    )
    insert_build = sa.text(
        "INSERT INTO skill_build_provenance "
        "(build_id, provenance_kind, legacy_marker_id, tenant_id, actor_id, "
        "build_request_sha256,command_receipt_id,command_receipt_authority_sha256,"
        "workflow_job_id,workflow_request_sha256,"
        "session_id, draft_id, "
        "skill_id, draft_revision_row_id, draft_revision, draft_sha256, "
        "source_bundle_sha256, origin_accepted_revision_row_id, patch_id, "
        "patch_decision_id, assistance_authority, authority_sha256, created_at) "
        "VALUES (:build_id, 'LEGACY_V04', :legacy_marker_id, :tenant_id, :actor_id, "
        ":build_request_sha256,:command_receipt_id,:command_receipt_authority_sha256,"
        ":workflow_job_id,:workflow_request_sha256,"
        "NULL, NULL, "
        ":skill_id, NULL, NULL, NULL, :source_bundle_sha256, NULL, NULL, NULL, "
        "'NONE', :authority_sha256, :created_at)"
    )
    build_authorities: dict[str, tuple[Mapping[str, object], str]] = {}
    for row in builds:
        build = row["build_json"]
        request = row["request_json"]
        if not isinstance(build, Mapping) or not isinstance(request, Mapping):
            raise RuntimeError("legacy Build JSON is not an object")
        source = request.get("source_bundle")
        if not isinstance(source, Mapping):
            raise RuntimeError("legacy Build has no source bundle")
        source_sha256 = _source_bundle_sha256(source)
        command = (
            connection.execute(
                sa.text(
                    "SELECT tenant_id, actor_id, command_id, command_type, status, revision, "
                    "terminal, accepted_at, updated_at, record_json FROM commands "
                    "WHERE command_id=:id"
                ),
                {"id": row["command_id"]},
            )
            .mappings()
            .one_or_none()
        )
        job = (
            connection.execute(
                sa.text(
                    "SELECT job_id,tenant_id,command_id,operation,subject_type,subject_id,"
                    "phase,status,attempt,fencing_token,lease_owner,lease_expires_at,"
                    "next_attempt_at,request_sha256,job_json,last_error_json,created_at,"
                    "updated_at FROM workflow_jobs "
                    "WHERE tenant_id=:tenant AND command_id=:command"
                ),
                {"tenant": row["tenant_id"], "command": row["command_id"]},
            )
            .mappings()
            .one_or_none()
        )
        command_receipt = (
            connection.execute(
                sa.text(
                    "SELECT receipt_id,tenant_id,actor_id,operation,idempotency_key,request_sha256,"
                    "command_id,accepted_at FROM idempotency_receipts WHERE tenant_id=:tenant "
                    "AND actor_id=:actor AND operation='CREATE_SKILL_BUILD' "
                    "AND command_id=:command"
                ),
                {
                    "tenant": row["tenant_id"],
                    "actor": row["actor_id"],
                    "command": row["command_id"],
                },
            )
            .mappings()
            .one_or_none()
        )
        terminal_receipts = list(
            connection.execute(
                sa.text(
                    "SELECT receipt_id,tenant_id,job_id,step_name,fencing_token,"
                    "input_sha256,output_sha256,receipt_json,completed_at "
                    "FROM job_step_receipts WHERE tenant_id=:tenant AND job_id=:job "
                    "AND step_name IN ('BUILD_CERTIFIED','BUILD_REJECTED') "
                    "ORDER BY step_name"
                ),
                {"tenant": row["tenant_id"], "job": job["job_id"] if job else ""},
            ).mappings()
        )
        expected_id = (
            "build_" + hashlib.sha256(str(row["command_id"]).encode("utf-8")).hexdigest()[:24]
        )
        origin = build.get("request_context")
        origin_actor = origin.get("actor") if isinstance(origin, Mapping) else None
        if (
            command is None
            or job is None
            or command_receipt is None
            or row["build_id"] != expected_id
            or build.get("build_id") != row["build_id"]
            or build.get("skill_id") != row["skill_id"]
            or request.get("skill_id") != row["skill_id"]
            or build.get("status") != row["status"]
            or build.get("terminal") is not row["terminal"]
            or not _wire_timestamp_matches(build.get("created_at"), row["created_at"])
            or not _wire_timestamp_matches(build.get("updated_at"), row["updated_at"])
            or not isinstance(origin_actor, Mapping)
            or origin_actor.get("tenant_id") != row["tenant_id"]
            or origin_actor.get("actor_id") != row["actor_id"]
            or command["tenant_id"] != row["tenant_id"]
            or command["actor_id"] != row["actor_id"]
            or command["command_type"] != "CREATE_SKILL_BUILD"
            or not isinstance(command["record_json"], Mapping)
            or set(command["record_json"])
            != {
                "request_context",
                "command_id",
                "command_type",
                "status",
                "stage",
                "terminal",
                "accepted_at",
                "updated_at",
                "result",
                "error",
                "evidence_refs",
                "versions",
                "links",
                "revision",
            }
            or command["record_json"].get("command_id") != row["command_id"]
            or not _legacy_command_context_and_versions_are_closed(
                command["record_json"]
            )
            or command["record_json"].get("command_type") != command["command_type"]
            or command["record_json"].get("status") != command["status"]
            or command["record_json"].get("terminal") is not command["terminal"]
            or command["record_json"].get("revision") != command["revision"]
            or not _wire_timestamp_matches(
                command["record_json"].get("accepted_at"), command["accepted_at"]
            )
            or not _wire_timestamp_matches(
                command["record_json"].get("updated_at"), command["updated_at"]
            )
            or command["record_json"].get("request_context") != origin
            or command["accepted_at"] != row["created_at"]
            or command["updated_at"] != row["updated_at"]
            or job["operation"] != "CREATE_SKILL_BUILD"
            or job["tenant_id"] != row["tenant_id"]
            or job["command_id"] != row["command_id"]
            or job["job_id"] != _scoped_identifier("job", row["tenant_id"], row["command_id"])
            or job["subject_type"] != "SKILL_BUILD"
            or job["subject_id"] != row["build_id"]
            or not isinstance(job["job_json"], Mapping)
            or set(job["job_json"]) != {"schema_version", "request_context", "build_id", "request"}
            or job["job_json"].get("schema_version") != "1.0.0"
            or job["job_json"].get("request_context") != origin
            or job["job_json"].get("build_id") != row["build_id"]
            or job["job_json"].get("request") != request
            or command_receipt["request_sha256"] != job["request_sha256"]
            or command_receipt["accepted_at"] != command["accepted_at"]
            or job["created_at"] != command["accepted_at"]
            or job["updated_at"] < row["updated_at"]
        ):
            raise RuntimeError("legacy Build command/request authority is corrupt")
        launch, policy, evidence_rows = _validate_legacy_build_launch_policy_and_evidence(
            connection,
            row=row,
            build=build,
            request=request,
        )
        _validate_legacy_build_terminal_authority(
            row=row,
            build=build,
            command=command,
            job=job,
            receipts=terminal_receipts,
            source_sha256=source_sha256,
        )
        artifact_count = int(
            connection.scalar(
                sa.text(
                    "SELECT count(*) FROM skill_artifacts WHERE tenant_id=:tenant "
                    "AND build_id=:build AND actor_id=:actor"
                ),
                {
                    "tenant": row["tenant_id"],
                    "build": row["build_id"],
                    "actor": row["actor_id"],
                },
            )
            or 0
        )
        certification_count = int(
            connection.scalar(
                sa.text(
                    "SELECT count(*) FROM skill_certifications WHERE tenant_id=:tenant "
                    "AND build_id=:build AND actor_id=:actor"
                ),
                {
                    "tenant": row["tenant_id"],
                    "build": row["build_id"],
                    "actor": row["actor_id"],
                },
            )
            or 0
        )
        artifact_row = (
            connection.execute(
                sa.text(
                    "SELECT tenant_id,artifact_sha256,build_id,actor_id,content_hash,"
                    "skill_id,source_sha256,artifact_uri,metadata_json,created_at "
                    "FROM skill_artifacts WHERE tenant_id=:tenant AND build_id=:build "
                    "AND actor_id=:actor"
                ),
                {
                    "tenant": row["tenant_id"],
                    "build": row["build_id"],
                    "actor": row["actor_id"],
                },
            )
            .mappings()
            .one_or_none()
        )
        certification_row = (
            connection.execute(
                sa.text(
                    "SELECT certification_id,tenant_id,build_id,skill_id,skill_version_id,"
                    "artifact_sha256,actor_id,content_hash,certification_sha256,"
                    "certification_json,certified_at FROM skill_certifications "
                    "WHERE tenant_id=:tenant AND build_id=:build AND actor_id=:actor"
                ),
                {
                    "tenant": row["tenant_id"],
                    "build": row["build_id"],
                    "actor": row["actor_id"],
                },
            )
            .mappings()
            .one_or_none()
        )
        if (row["status"] == "CERTIFIED" and (artifact_count != 1 or certification_count != 1)) or (
            row["status"] != "CERTIFIED" and (artifact_count or certification_count)
        ):
            raise RuntimeError("legacy Build Artifact/Certification authority is corrupt")
        if row["status"] == "CERTIFIED":
            if artifact_row is None or certification_row is None:
                raise RuntimeError("legacy certified Build authority disappeared")
            certification_wire = certification_row["certification_json"]
            if (
                not isinstance(certification_wire, Mapping)
                or _canonical_sha256(certification_wire)
                != certification_row["certification_sha256"]
                or certification_wire.get("schema_version") != "1.0.0"
                or certification_wire.get("certification_id")
                != certification_row["certification_id"]
                or certification_wire.get("build_id") != row["build_id"]
                or certification_wire.get("skill_id") != row["skill_id"]
                or certification_wire.get("skill_version_id")
                != certification_row["skill_version_id"]
                or certification_wire.get("artifact_sha256") != artifact_row["artifact_sha256"]
                or certification_wire.get("source_sha256") != source_sha256
                or artifact_row["source_sha256"] != source_sha256
                or artifact_row["skill_id"] != row["skill_id"]
                or artifact_row["content_hash"] != certification_row["content_hash"]
                or certification_wire.get("actor_id") != row["actor_id"]
                or certification_wire.get("content_hash") != certification_row["content_hash"]
            ):
                raise RuntimeError("legacy Certification or Artifact bytes are corrupt")
            _validate_legacy_certified_build_authority(
                row=row,
                build=build,
                request=request,
                command=command,
                job=job,
                receipt=terminal_receipts[0],
                launch=launch,
                policy=policy,
                artifact=artifact_row,
                certification=certification_row,
                evidence_rows=evidence_rows,
                source_sha256=source_sha256,
            )
        marker_id = (
            "legacy_build_"
            + hashlib.sha256(
                f"{row['tenant_id']}\0{row['actor_id']}\0{row['build_id']}".encode()
            ).hexdigest()[:32]
        )
        command_receipt_authority_sha256 = _canonical_sha256(
            _legacy_build_command_receipt_authority(command_receipt)
        )
        row_value = {
            **dict(row),
            "legacy_marker_id": marker_id,
            "build_request_sha256": _canonical_sha256(request),
            "command_receipt_id": command_receipt["receipt_id"],
            "command_receipt_authority_sha256": (command_receipt_authority_sha256),
            "workflow_job_id": job["job_id"],
            "workflow_request_sha256": job["request_sha256"],
        }
        authority = _legacy_build_authority(row_value, source_sha256)
        digest = _canonical_sha256(authority)
        marker_authority = {
            "authority_type": "INT2_LEGACY_BUILD_MARKER",
            "authority_version": "1.0.0",
            "marker_id": marker_id,
            "build_id": row["build_id"],
            "tenant_id": row["tenant_id"],
            "actor_id": row["actor_id"],
            "build_authority_sha256": digest,
        }
        connection.execute(
            sa.text(
                "INSERT INTO int2_legacy_build_markers "
                "(marker_id,build_id,tenant_id,actor_id,build_authority_sha256,"
                "marker_sha256,created_at) VALUES "
                "(:marker_id,:build_id,:tenant_id,:actor_id,:build_authority_sha256,"
                ":marker_sha256,:created_at)"
            ),
            {
                **dict(row),
                "marker_id": marker_id,
                "build_authority_sha256": digest,
                "marker_sha256": _canonical_sha256(marker_authority),
            },
        )
        connection.execute(
            insert_build,
            {
                **row_value,
                "source_bundle_sha256": source_sha256,
                "authority_sha256": digest,
            },
        )
        connection.execute(
            sa.text(
                "UPDATE workflow_jobs SET job_json = job_json || "
                "jsonb_build_object("
                "'build_provenance_sha256', CAST(:authority_sha256 AS text)) "
                "WHERE tenant_id=:tenant_id AND command_id=:command_id"
            ),
            {
                "authority_sha256": digest,
                "tenant_id": row["tenant_id"],
                "command_id": row["command_id"],
            },
        )
        updated_job_json = {
            **dict(job["job_json"]),
            "build_provenance_sha256": digest,
        }
        terminal_job = {**dict(job), "job_json": updated_job_json}
        certification_authority_digest: str | None = None
        if certification_row is not None and artifact_row is not None:
            artifact_authority = {
                "tenant_id": artifact_row["tenant_id"],
                "actor_id": artifact_row["actor_id"],
                "content_hash": artifact_row["content_hash"],
                "build_id": artifact_row["build_id"],
                "skill_id": artifact_row["skill_id"],
                "artifact_sha256": artifact_row["artifact_sha256"],
                "source_sha256": artifact_row["source_sha256"],
                "artifact_uri": artifact_row["artifact_uri"],
                "metadata": artifact_row["metadata_json"],
            }
            artifact_authority_digest = _canonical_sha256(artifact_authority)
            certification_authority_digest = _backfill_legacy_certification_provenance(
                connection,
                build=row,
                build_request=request,
                build_authority_sha256=digest,
                workflow=job,
                certification=certification_row,
                artifact_authority_sha256=artifact_authority_digest,
            )
            _backfill_legacy_activation_provenance(
                connection,
                build=row,
                build_authority_sha256=digest,
                certification=certification_row,
                certification_authority_sha256=certification_authority_digest,
                artifact_authority_sha256=artifact_authority_digest,
            )
        if bool(row["terminal"]):
            if len(terminal_receipts) != 1:
                raise RuntimeError("legacy terminal Build receipt authority is ambiguous")
            _backfill_legacy_build_terminal_authority(
                connection,
                build=row,
                build_authority_sha256=digest,
                command=command,
                workflow=terminal_job,
                receipt=terminal_receipts[0],
                certification_id=(
                    certification_row["certification_id"] if certification_row is not None else None
                ),
                certification_authority_sha256=certification_authority_digest,
            )
        build_authorities[str(row["build_id"])] = (row_value, digest)

    runs = list(
        connection.execute(
            sa.text(
                "SELECT run_id, tenant_id, actor_id, session_id, command_id, created_at, "
                "run_json FROM game_runs ORDER BY run_id"
            )
        ).mappings()
    )
    insert_run = sa.text(
        "INSERT INTO skill_run_provenance "
        "(run_id, build_id, provenance_kind, build_authority_sha256, tenant_id, "
        "actor_id, session_id, activation_id, activation_sha256, registry_revision, "
        "activation_authority_sha256, certification_id, certification_sha256, "
        "certification_authority_sha256, "
        "artifact_sha256, artifact_authority_sha256, "
        "draft_revision_row_id, draft_sha256, "
        "assistance_authority, authority_sha256, created_at) VALUES "
        "(:run_id, :build_id, 'LEGACY_V04', :build_authority_sha256, :tenant_id, "
        ":actor_id, :session_id, :activation_id, :activation_sha256, "
        ":registry_revision, :activation_authority_sha256, :certification_id, "
        ":certification_sha256, :certification_authority_sha256, "
        ":artifact_sha256, :artifact_authority_sha256, "
        "NULL, NULL, "
        "'NONE', :authority_sha256, :created_at)"
    )
    for run in runs:
        value = run["run_json"]
        skill = value.get("skill") if isinstance(value, Mapping) else None
        artifact_sha256 = skill.get("artifact_sha256") if isinstance(skill, Mapping) else None
        certification_id = skill.get("certification_id") if isinstance(skill, Mapping) else None
        candidates = list(
            connection.execute(
                sa.text(
                    "SELECT build_id FROM skill_certifications WHERE tenant_id=:tenant "
                    "AND actor_id=:actor AND certification_id=:certification "
                    "AND artifact_sha256=:artifact"
                ),
                {
                    "tenant": run["tenant_id"],
                    "actor": run["actor_id"],
                    "certification": certification_id,
                    "artifact": artifact_sha256,
                },
            ).scalars()
        )
        if (
            not isinstance(value, Mapping)
            or value.get("run_id") != run["run_id"]
            or value.get("session_id") != run["session_id"]
            or value.get("command_id") != run["command_id"]
            or len(candidates) != 1
            or candidates[0] not in build_authorities
        ):
            raise RuntimeError("legacy Run cannot close to one certified Build")
        build_id = str(candidates[0])
        build_row, build_digest = build_authorities[build_id]
        if build_row["tenant_id"] != run["tenant_id"] or build_row["actor_id"] != run["actor_id"]:
            raise RuntimeError("legacy Run and Build authority scopes differ")
        certification = (
            connection.execute(
                sa.text(
                    "SELECT certification_sha256,certification_json,skill_id,"
                    "skill_version_id FROM skill_certifications "
                    "WHERE tenant_id=:tenant AND actor_id=:actor "
                    "AND certification_id=:certification AND artifact_sha256=:artifact "
                    "AND build_id=:build"
                ),
                {
                    "tenant": run["tenant_id"],
                    "actor": run["actor_id"],
                    "certification": certification_id,
                    "artifact": artifact_sha256,
                    "build": build_id,
                },
            )
            .mappings()
            .one_or_none()
        )
        artifact = (
            connection.execute(
                sa.text(
                    "SELECT tenant_id,actor_id,content_hash,build_id,skill_id,"
                    "artifact_sha256,source_sha256,artifact_uri,metadata_json "
                    "FROM skill_artifacts WHERE tenant_id=:tenant AND actor_id=:actor "
                    "AND build_id=:build AND artifact_sha256=:artifact"
                ),
                {
                    "tenant": run["tenant_id"],
                    "actor": run["actor_id"],
                    "build": build_id,
                    "artifact": artifact_sha256,
                },
            )
            .mappings()
            .one_or_none()
        )
        if (
            certification is None
            or artifact is None
            or not isinstance(skill, Mapping)
            or skill.get("skill_id") != certification["skill_id"]
            or skill.get("skill_version_id") != certification["skill_version_id"]
            or not isinstance(certification["certification_json"], Mapping)
            or _canonical_sha256(certification["certification_json"])
            != certification["certification_sha256"]
        ):
            raise RuntimeError("legacy Run Skill/Certification bytes are corrupt")
        certification_provenance = (
            connection.execute(
                sa.text(
                    "SELECT authority_sha256 FROM skill_certification_provenance "
                    "WHERE certification_id=:certification_id AND tenant_id=:tenant_id "
                    "AND actor_id=:actor_id AND build_id=:build_id"
                ),
                {
                    "certification_id": certification_id,
                    "tenant_id": run["tenant_id"],
                    "actor_id": run["actor_id"],
                    "build_id": build_id,
                },
            )
            .mappings()
            .one_or_none()
        )
        if certification_provenance is None:
            raise RuntimeError("legacy Run Certification seal disappeared")
        artifact_authority_digest = _canonical_sha256(
            {
                "tenant_id": artifact["tenant_id"],
                "actor_id": artifact["actor_id"],
                "content_hash": artifact["content_hash"],
                "build_id": artifact["build_id"],
                "skill_id": artifact["skill_id"],
                "artifact_sha256": artifact["artifact_sha256"],
                "source_sha256": artifact["source_sha256"],
                "artifact_uri": artifact["artifact_uri"],
                "metadata": artifact["metadata_json"],
            }
        )
        authority = {
            "authority_type": "SKILL_RUN_PROVENANCE",
            "authority_version": "1.0.0",
            "run_id": run["run_id"],
            "build_id": build_id,
            "provenance_kind": "LEGACY_V04",
            "build_authority_sha256": build_digest,
            "tenant_id": run["tenant_id"],
            "actor_id": run["actor_id"],
            "session_id": run["session_id"],
            "activation_id": None,
            "activation_sha256": None,
            "activation_authority_sha256": None,
            "registry_revision": None,
            "certification_id": certification_id,
            "certification_sha256": certification["certification_sha256"],
            "certification_authority_sha256": certification_provenance["authority_sha256"],
            "artifact_sha256": artifact_sha256,
            "artifact_authority_sha256": artifact_authority_digest,
            "draft_revision_row_id": None,
            "draft_sha256": None,
            "assistance_authority": "NONE",
        }
        connection.execute(
            insert_run,
            {
                **dict(run),
                "build_id": build_id,
                "build_authority_sha256": build_digest,
                "activation_id": None,
                "activation_sha256": None,
                "activation_authority_sha256": None,
                "registry_revision": None,
                "certification_id": certification_id,
                "certification_sha256": certification["certification_sha256"],
                "certification_authority_sha256": certification_provenance["authority_sha256"],
                "artifact_sha256": artifact_sha256,
                "artifact_authority_sha256": artifact_authority_digest,
                "authority_sha256": _canonical_sha256(authority),
            },
        )


def _validate_legacy_build_launch_policy_and_evidence(
    connection: sa.engine.Connection,
    *,
    row: Mapping[str, object],
    build: Mapping[str, object],
    request: Mapping[str, object],
) -> tuple[Mapping[str, object], Mapping[str, object], list[Mapping[str, object]]]:
    origin = build.get("request_context")
    actor = origin.get("actor") if isinstance(origin, Mapping) else None
    content = origin.get("content_ref") if isinstance(origin, Mapping) else None
    if not isinstance(actor, Mapping) or not isinstance(content, Mapping):
        raise RuntimeError("legacy Build Launch/Policy/Evidence authority is corrupt")
    launches = list(
        connection.execute(
            sa.text(
                "SELECT tenant_id,authority_id,actor_id,content_unit_id,content_version,"
                "content_hash,world_id,learner_id,agent_profile_id,build_policy_id,channel,"
                "teaching_spec_version,authority_sha256,active,created_at "
                "FROM launch_authorities WHERE tenant_id=:tenant_id AND actor_id=:actor_id "
                "AND content_unit_id=:content_unit_id AND content_version=:content_version "
                "AND content_hash=:content_hash AND active"
            ),
            {
                "tenant_id": row["tenant_id"],
                "actor_id": row["actor_id"],
                "content_unit_id": content.get("unit_id"),
                "content_version": content.get("version"),
                "content_hash": content.get("content_hash"),
            },
        ).mappings()
    )
    if len(launches) != 1:
        raise RuntimeError("legacy Build Launch/Policy/Evidence authority is corrupt")
    launch = launches[0]
    policy = (
        connection.execute(
            sa.text(
                "SELECT tenant_id,build_policy_id,actor_id,content_hash,compiler_profile,"
                "compiler_version,sandbox_image_digest,test_suite_version,allowed_capabilities,"
                "max_source_files,max_source_bytes,policy_json,policy_sha256,active,created_at "
                "FROM build_policies WHERE tenant_id=:tenant_id "
                "AND build_policy_id=:build_policy_id AND actor_id=:actor_id "
                "AND content_hash=:content_hash AND active"
            ),
            dict(launch),
        )
        .mappings()
        .one_or_none()
    )
    policy_json = policy["policy_json"] if policy is not None else None
    parameter_schema = (
        policy_json.get("parameter_schema") if isinstance(policy_json, Mapping) else None
    )
    parameter_schema_is_valid = False
    if isinstance(parameter_schema, Mapping):
        try:
            validator_for(parameter_schema).check_schema(parameter_schema)
        except Exception:
            pass
        else:
            parameter_schema_is_valid = True
    requested_capabilities = request.get("requested_capabilities")
    allowed_capabilities = policy["allowed_capabilities"] if policy is not None else None
    evidence_rows = list(
        connection.execute(
            sa.text(
                "SELECT evidence_id,tenant_id,actor_id,content_hash,command_id,recorded_at,"
                "evidence_json FROM game_evidence WHERE tenant_id=:tenant_id "
                "AND actor_id=:actor_id AND command_id=:command_id ORDER BY evidence_id"
            ),
            dict(row),
        ).mappings()
    )
    evidence_count = len(evidence_rows)
    if (
        policy is None
        or launch["tenant_id"] != actor.get("tenant_id")
        or launch["actor_id"] != actor.get("actor_id")
        or launch["channel"] != "GAME"
        or policy["tenant_id"] != row["tenant_id"]
        or policy["actor_id"] != row["actor_id"]
        or policy["content_hash"] != content.get("content_hash")
        or not isinstance(policy_json, Mapping)
        or _canonical_sha256(policy_json) != policy["policy_sha256"]
        or policy_json.get("schema_version") != "1.0.0"
        or policy_json.get("compiler_profile") != policy["compiler_profile"]
        or policy_json.get("compiler_version") != policy["compiler_version"]
        or policy_json.get("test_suite_version") != policy["test_suite_version"]
        or not isinstance(policy_json.get("compiler_image"), str)
        or not policy_json["compiler_image"].endswith(f"@{policy['sandbox_image_digest']}")
        or not isinstance(parameter_schema, Mapping)
        or not parameter_schema_is_valid
        or "x-yaya-certification" in parameter_schema
        or (("type" in parameter_schema) == ("oneOf" in parameter_schema))
        or request.get("compiler_profile") != policy["compiler_profile"]
        or request.get("test_suite_version") != policy["test_suite_version"]
        or not isinstance(requested_capabilities, list)
        or not isinstance(allowed_capabilities, list)
        or any(not isinstance(item, str) or not item for item in allowed_capabilities)
        or any(
            not isinstance(item, str) or not item or item not in allowed_capabilities
            for item in requested_capabilities
        )
        or len(set(requested_capabilities)) != len(requested_capabilities)
        or (row["status"] == "CERTIFIED" and evidence_count != 1)
        or (row["status"] != "CERTIFIED" and evidence_count != 0)
    ):
        raise RuntimeError("legacy Build Launch/Policy/Evidence authority is corrupt")
    return launch, policy, evidence_rows


def _legacy_command_context_and_versions_are_closed(record: Mapping[str, object]) -> bool:
    try:
        parsed = command_record_from_data(dict(record))
        canonical = command_record_data(parsed)
    except Exception:
        return False
    return canonical == dict(record)


def _validate_legacy_certified_build_authority(
    *,
    row: Mapping[str, object],
    build: Mapping[str, object],
    request: Mapping[str, object],
    command: Mapping[str, object],
    job: Mapping[str, object],
    receipt: Mapping[str, object],
    launch: Mapping[str, object],
    policy: Mapping[str, object],
    artifact: Mapping[str, object],
    certification: Mapping[str, object],
    evidence_rows: list[Mapping[str, object]],
    source_sha256: str,
) -> None:
    """Mirror the current public CERTIFIED Build closure before sealing v0.4 bytes."""

    record = command["record_json"]
    policy_json = policy["policy_json"]
    metadata = artifact["metadata_json"]
    certification_json = certification["certification_json"]
    output = receipt["receipt_json"]
    if (
        not isinstance(record, Mapping)
        or not isinstance(policy_json, Mapping)
        or not isinstance(metadata, Mapping)
        or not isinstance(certification_json, Mapping)
        or not isinstance(output, Mapping)
        or len(evidence_rows) != 1
    ):
        raise RuntimeError("legacy certified Build authority is corrupt")
    evidence_row = evidence_rows[0]
    evidence = evidence_row["evidence_json"]
    if not isinstance(evidence, Mapping):
        raise RuntimeError("legacy certified Build authority is corrupt")
    evidence_ref = evidence.get("evidence_ref")
    evidence_source = evidence.get("source")
    evidence_payload = evidence.get("payload")
    evidence_integrity = evidence.get("integrity")
    artifact_wire = build.get("artifact")
    certification_wire = build.get("certification")
    versions = build.get("versions")
    parameter_schema = policy_json.get("parameter_schema")
    capabilities = certification_json.get("capabilities")
    if (
        not isinstance(evidence_ref, Mapping)
        or not isinstance(evidence_source, Mapping)
        or not isinstance(evidence_payload, Mapping)
        or not isinstance(evidence_integrity, Mapping)
        or not isinstance(artifact_wire, Mapping)
        or not isinstance(certification_wire, Mapping)
        or not isinstance(versions, Mapping)
        or not isinstance(parameter_schema, Mapping)
        or not isinstance(capabilities, list)
    ):
        raise RuntimeError("legacy certified Build authority is corrupt")
    skill_version_id = output.get("skill_version_id")
    artifact_sha256 = output.get("artifact_sha256")
    certification_id = output.get("certification_id")
    evidence_id = output.get("evidence_id")
    certification_tuple = {
        "build_id": row["build_id"],
        "skill_id": row["skill_id"],
        "skill_version_id": skill_version_id,
        "source_sha256": source_sha256,
        "artifact_sha256": artifact_sha256,
        "certification_id": certification_id,
        "build_policy_id": policy["build_policy_id"],
        "policy_sha256": policy["policy_sha256"],
        "actor_id": row["actor_id"],
        "content_hash": artifact["content_hash"],
    }
    expected_schema = {
        **dict(parameter_schema),
        "x-yaya-certification": {
            "schema_version": "1.0.0",
            "build_id": row["build_id"],
            "skill_id": row["skill_id"],
            "skill_version_id": skill_version_id,
            "source_sha256": source_sha256,
            "artifact_sha256": artifact_sha256,
            "certification_id": certification_id,
            "build_policy_id": policy["build_policy_id"],
            "policy_sha256": policy["policy_sha256"],
            "actor_id": row["actor_id"],
            "content_hash": artifact["content_hash"],
            "capabilities": capabilities,
        },
    }
    expected_schema_sha256 = _canonical_sha256(expected_schema)
    record_versions = record.get("versions")
    expected_versions = (
        {key: item for key, item in record_versions.items() if item is not None}
        if isinstance(record_versions, Mapping)
        else None
    )
    if isinstance(expected_versions, dict):
        expected_versions.update(
            {
                "policy_version": policy["build_policy_id"],
                "skill_version": skill_version_id,
                "artifact_sha256": artifact_sha256,
                "compiler_version": metadata.get("compiler_version"),
                "sandbox_image_digest": metadata.get("compiler_image"),
                "test_suite_version": metadata.get("test_suite_version"),
            }
        )
    expected_result = {
        "result_type": "RESOURCE_CREATED",
        "resource_type": "SKILL_BUILD",
        "resource_id": row["build_id"],
        "resource_url": f"/v1/skill-builds/{row['build_id']}",
    }
    expected_output_keys = {
        "build_id",
        "skill_version_id",
        "artifact_sha256",
        "certification_id",
        "evidence_id",
        "build_identity",
    }
    expected_metadata_keys = {
        "schema_version",
        "artifact_sha256",
        "source_sha256",
        "build_identity",
        "size_bytes",
        "compiler_profile",
        "compiler_version",
        "compiler_image",
        "test_suite_version",
        "policy_sha256",
        "parameter_schema",
        "parameter_schema_sha256",
    }
    expected_certification_keys = {
        "schema_version",
        "certification_id",
        "build_id",
        "skill_id",
        "skill_version_id",
        "artifact_sha256",
        "source_sha256",
        "actor_id",
        "content_hash",
        "build_policy_id",
        "policy_sha256",
        "capabilities",
        "issued_at",
        "parameter_schema",
        "parameter_schema_sha256",
    }
    expected_evidence_keys = {
        "request_context",
        "evidence_ref",
        "subject",
        "source",
        "occurred_at",
        "recorded_at",
        "integrity",
        "payload",
        "related_evidence",
        "versions",
    }
    content = build.get("request_context")
    content_ref = content.get("content_ref") if isinstance(content, Mapping) else None
    size_bytes = metadata.get("size_bytes")
    if (
        set(policy_json)
        != {
            "schema_version",
            "compiler_image",
            "compiler_profile",
            "compiler_version",
            "test_suite_version",
            "compile_flags",
            "public_tests",
            "hidden_tests",
            "limits",
            "parameter_schema",
        }
        or not isinstance(content_ref, Mapping)
        or any(
            not isinstance(item, str) or not item
            for item in certification_tuple.values()
        )
        or command["status"] != "APPLIED"
        or command["terminal"] is not True
        or record.get("status") != "APPLIED"
        or record.get("stage") != "COMPLETE"
        or record.get("terminal") is not True
        or record.get("result") != expected_result
        or record.get("error") is not None
        or not _legacy_command_evidence_ref_matches(
            record.get("evidence_refs"), evidence_ref
        )
        or command["updated_at"] != row["updated_at"]
        or job["status"] != "SUCCEEDED"
        or job["phase"] != "COMPLETE"
        or job["lease_owner"] is not None
        or job["lease_expires_at"] is not None
        or job["next_attempt_at"] is not None
        or job["last_error_json"] is not None
        or not isinstance(job["attempt"], int)
        or job["attempt"] < 1
        or not isinstance(job["fencing_token"], int)
        or job["fencing_token"] < 1
        or receipt["receipt_id"]
        != _scoped_identifier("receipt", str(row["tenant_id"]), str(job["job_id"]), "BUILD_CERTIFIED")
        or receipt["tenant_id"] != row["tenant_id"]
        or receipt["job_id"] != job["job_id"]
        or receipt["step_name"] != "BUILD_CERTIFIED"
        or receipt["fencing_token"] != job["fencing_token"]
        or receipt["input_sha256"] != job["request_sha256"]
        or receipt["output_sha256"] != _canonical_sha256(output)
        or not row["updated_at"] <= receipt["completed_at"] <= job["updated_at"]
        or set(output) != expected_output_keys
        or output.get("build_id") != row["build_id"]
        or not isinstance(output.get("build_identity"), str)
        or not output.get("build_identity")
        or build.get("skill_version_id") != skill_version_id
        or set(artifact_wire)
        != {
            "artifact_sha256",
            "source_sha256",
            "compiler_profile",
            "compiler_version",
            "test_suite_version",
        }
        or artifact_wire.get("artifact_sha256") != artifact_sha256
        or artifact_wire.get("source_sha256") != source_sha256
        or artifact_wire.get("compiler_profile") != metadata.get("compiler_profile")
        or artifact_wire.get("compiler_version") != metadata.get("compiler_version")
        or artifact_wire.get("test_suite_version") != metadata.get("test_suite_version")
        or set(certification_wire) != {"certification_id", "issued_at", "capabilities"}
        or certification_wire.get("certification_id") != certification_id
        or certification_wire.get("capabilities") != capabilities
        or artifact["tenant_id"] != row["tenant_id"]
        or artifact["build_id"] != row["build_id"]
        or artifact["actor_id"] != row["actor_id"]
        or artifact["content_hash"] != content_ref.get("content_hash")
        or artifact["skill_id"] != row["skill_id"]
        or artifact["artifact_sha256"] != artifact_sha256
        or artifact["source_sha256"] != source_sha256
        or artifact["artifact_uri"] != f"artifact://sha256/{artifact_sha256}"
        or artifact["created_at"] != row["updated_at"]
        or set(metadata) != expected_metadata_keys
        or metadata.get("schema_version") != "1.0.0"
        or metadata.get("artifact_sha256") != artifact_sha256
        or metadata.get("source_sha256") != source_sha256
        or metadata.get("build_identity") != output.get("build_identity")
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 1
        or metadata.get("compiler_profile") != policy["compiler_profile"]
        or metadata.get("compiler_version") != policy["compiler_version"]
        or metadata.get("compiler_image") != policy_json.get("compiler_image")
        or metadata.get("test_suite_version") != policy["test_suite_version"]
        or metadata.get("policy_sha256") != policy["policy_sha256"]
        or metadata.get("parameter_schema") != expected_schema
        or metadata.get("parameter_schema_sha256") != expected_schema_sha256
        or certification["tenant_id"] != row["tenant_id"]
        or certification["build_id"] != row["build_id"]
        or certification["actor_id"] != row["actor_id"]
        or certification["content_hash"] != content_ref.get("content_hash")
        or certification["skill_id"] != row["skill_id"]
        or certification["skill_version_id"] != skill_version_id
        or certification["artifact_sha256"] != artifact_sha256
        or certification["certification_id"] != certification_id
        or certification["certification_sha256"] != _canonical_sha256(certification_json)
        or certification["certified_at"] != row["updated_at"]
        or set(certification_json) != expected_certification_keys
        or certification_json.get("schema_version") != "1.0.0"
        or certification_json.get("certification_id") != certification_id
        or certification_json.get("build_id") != row["build_id"]
        or certification_json.get("skill_id") != row["skill_id"]
        or certification_json.get("skill_version_id") != skill_version_id
        or certification_json.get("artifact_sha256") != artifact_sha256
        or certification_json.get("source_sha256") != source_sha256
        or certification_json.get("actor_id") != row["actor_id"]
        or certification_json.get("content_hash") != content_ref.get("content_hash")
        or certification_json.get("build_policy_id") != policy["build_policy_id"]
        or certification_json.get("policy_sha256") != policy["policy_sha256"]
        or capabilities != request.get("requested_capabilities")
        or any(item not in policy["allowed_capabilities"] for item in capabilities)
        or certification_json.get("parameter_schema") != expected_schema
        or certification_json.get("parameter_schema_sha256") != expected_schema_sha256
        or not _wire_timestamp_matches(certification_wire.get("issued_at"), row["updated_at"])
        or not _wire_timestamp_matches(certification_json.get("issued_at"), row["updated_at"])
        or evidence_row["evidence_id"] != evidence_id
        or evidence_row["tenant_id"] != row["tenant_id"]
        or evidence_row["actor_id"] != row["actor_id"]
        or evidence_row["content_hash"] != content_ref.get("content_hash")
        or evidence_row["command_id"] != row["command_id"]
        or evidence_row["recorded_at"] != row["updated_at"]
        or set(evidence) != expected_evidence_keys
        or evidence.get("request_context") != build.get("request_context")
        or evidence.get("subject") != {"learner_id": launch["learner_id"]}
        or set(evidence_ref)
        != {"evidence_id", "evidence_type", "created_at", "sha256", "uri"}
        or evidence_ref.get("evidence_id") != evidence_id
        or evidence_ref.get("evidence_type") != "TEST_REPORT"
        or evidence_ref.get("created_at") != _iso_datetime(row["updated_at"])
        or evidence_ref.get("sha256") != _canonical_sha256(evidence_payload)
        or evidence_ref.get("uri") != f"/v1/evidence/{evidence_id}"
        or set(evidence_source) != {"source_type", "source_id", "command_id", "world_id"}
        or evidence_source.get("source_type") != "SKILL_BUILD"
        or evidence_source.get("source_id") != row["build_id"]
        or evidence_source.get("command_id") != row["command_id"]
        or evidence_source.get("world_id") != launch["world_id"]
        or set(evidence_payload)
        != {
            "evidence_kind",
            "build_id",
            "skill_id",
            "skill_version_id",
            "artifact_sha256",
            "test_suite_version",
            "outcome",
        }
        or evidence_payload.get("evidence_kind") != "BUILD_CERTIFICATION"
        or evidence_payload.get("build_id") != row["build_id"]
        or evidence_payload.get("skill_id") != row["skill_id"]
        or evidence_payload.get("skill_version_id") != skill_version_id
        or evidence_payload.get("artifact_sha256") != artifact_sha256
        or evidence_payload.get("test_suite_version") != metadata.get("test_suite_version")
        or evidence_payload.get("outcome") != "CERTIFIED"
        or evidence.get("occurred_at") != _iso_datetime(row["updated_at"])
        or evidence.get("recorded_at") != _iso_datetime(row["updated_at"])
        or evidence_integrity
        != {
            "payload_sha256": evidence_ref.get("sha256"),
            "previous_evidence_sha256": None,
        }
        or evidence.get("related_evidence") != []
        or evidence.get("versions") != versions
        or expected_versions is None
        or dict(versions) != expected_versions
        or not _legacy_certified_build_phases_match(build.get("phases"), row["updated_at"])
        or build.get("failure") is not None
    ):
        raise RuntimeError("legacy certified Build authority is corrupt")


def _legacy_command_evidence_ref_matches(
    value: object,
    expected: Mapping[str, object],
) -> bool:
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], Mapping):
        return False
    actual = value[0]
    try:
        actual_created_at = datetime.fromisoformat(
            str(actual.get("created_at")).replace("Z", "+00:00")
        )
        expected_created_at = datetime.fromisoformat(
            str(expected.get("created_at")).replace("Z", "+00:00")
        )
    except ValueError:
        return False
    return (
        set(actual) == {"evidence_id", "evidence_type", "created_at", "sha256", "uri"}
        and actual.get("evidence_id") == expected.get("evidence_id")
        and actual.get("evidence_type") == expected.get("evidence_type")
        and actual_created_at == expected_created_at
        and actual.get("sha256") == expected.get("sha256")
        and actual.get("uri") == expected.get("uri")
    )


def _legacy_certified_build_phases_match(value: object, finished_at: object) -> bool:
    names = ("VALIDATE_SOURCE", "COMPILE", "PUBLIC_TEST", "HIDDEN_TEST", "CERTIFY")
    if not isinstance(value, list) or len(value) != len(names):
        return False
    if not isinstance(finished_at, datetime) or finished_at.tzinfo is None:
        return False
    started_at: datetime | None = None
    for name, raw in zip(names, value, strict=True):
        if not isinstance(raw, Mapping) or set(raw) != {
            "name",
            "status",
            "started_at",
            "finished_at",
            "diagnostic_codes",
        }:
            return False
        try:
            phase_started = datetime.fromisoformat(
                str(raw.get("started_at")).replace("Z", "+00:00")
            )
            phase_finished = datetime.fromisoformat(
                str(raw.get("finished_at")).replace("Z", "+00:00")
            )
        except ValueError:
            return False
        if started_at is None:
            started_at = phase_started
        if (
            phase_started.tzinfo is None
            or phase_finished.tzinfo is None
            or raw.get("name") != name
            or raw.get("status") != "PASSED"
            or raw.get("diagnostic_codes") != []
            or phase_started != started_at
            or phase_finished != finished_at
            or phase_started > phase_finished
        ):
            return False
    return True


def _validate_legacy_build_terminal_authority(
    *,
    row: Mapping[str, object],
    build: Mapping[str, object],
    command: Mapping[str, object],
    job: Mapping[str, object],
    receipts: list[Mapping[str, object]],
    source_sha256: str,
) -> None:
    """Reject v0.4 Build terminal bytes that current readers would reject."""

    expected_build_keys = {
        "request_context",
        "build_id",
        "skill_id",
        "skill_version_id",
        "status",
        "terminal",
        "created_at",
        "updated_at",
        "artifact",
        "certification",
        "phases",
        "failure",
        "evidence_refs",
        "versions",
    }
    if set(build) != expected_build_keys:
        raise RuntimeError("legacy Build command/request authority is corrupt")
    if not bool(row["terminal"]):
        record = command["record_json"]
        record_versions = record.get("versions") if isinstance(record, Mapping) else None
        expected_versions = (
            {key: item for key, item in record_versions.items() if item is not None}
            if isinstance(record_versions, Mapping)
            else None
        )
        if (
            receipts
            or row["status"] != "ACCEPTED"
            or build.get("status") != "ACCEPTED"
            or build.get("terminal") is not False
            or build.get("skill_version_id") is not None
            or build.get("artifact") is not None
            or build.get("certification") is not None
            or build.get("failure") is not None
            or build.get("evidence_refs") != []
            or build.get("versions") != expected_versions
            or not isinstance(record, Mapping)
            or record.get("terminal") is not False
            or command["terminal"] is not False
            or job["status"] in {"SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTER"}
        ):
            raise RuntimeError("legacy Build nonterminal authority is corrupt")
        return
    if row["status"] == "CERTIFIED":
        # The certified branch is closed together with its immutable Artifact,
        # Certification, Evidence and BUILD_CERTIFIED receipt below.
        if len(receipts) != 1 or receipts[0]["step_name"] != "BUILD_CERTIFIED":
            raise RuntimeError("legacy Build terminal receipt authority is corrupt")
        return
    if row["status"] != "REJECTED" or len(receipts) != 1:
        raise RuntimeError("legacy Build terminal receipt authority is corrupt")

    record = command["record_json"]
    receipt = receipts[0]
    output = receipt["receipt_json"]
    failure = build.get("failure")
    details = failure.get("details") if isinstance(failure, Mapping) else None
    if (
        not isinstance(record, Mapping)
        or not isinstance(output, Mapping)
        or not isinstance(failure, Mapping)
        or not isinstance(details, Mapping)
    ):
        raise RuntimeError("legacy Build terminal receipt authority is corrupt")
    expected_command_error = {**dict(failure), "stage": "VALIDATE"}
    diagnostics = output.get("diagnostic_codes")
    failure_stage = output.get("failure_stage")
    expected_receipt_id = _scoped_identifier(
        "receipt",
        str(row["tenant_id"]),
        str(job["job_id"]),
        "BUILD_REJECTED",
    )
    if (
        record.get("status") != "REJECTED"
        or record.get("stage") != "VALIDATE"
        or record.get("terminal") is not True
        or record.get("result") is not None
        or record.get("error") != expected_command_error
        or record.get("evidence_refs") != []
        or build.get("skill_version_id") is not None
        or build.get("artifact") is not None
        or build.get("certification") is not None
        or build.get("evidence_refs") != []
        or build.get("versions") != record.get("versions")
        or job["status"] != "FAILED"
        or job["phase"] != failure_stage
        or job["lease_owner"] is not None
        or job["lease_expires_at"] is not None
        or job["next_attempt_at"] is not None
        or job["last_error_json"] != failure
        or not isinstance(job["attempt"], int)
        or job["attempt"] < 1
        or not isinstance(job["fencing_token"], int)
        or job["fencing_token"] < 1
        or receipt["receipt_id"] != expected_receipt_id
        or receipt["tenant_id"] != row["tenant_id"]
        or receipt["job_id"] != job["job_id"]
        or receipt["step_name"] != "BUILD_REJECTED"
        or receipt["fencing_token"] != job["fencing_token"]
        or receipt["input_sha256"] != job["request_sha256"]
        or receipt["output_sha256"] != _canonical_sha256(output)
        or not row["updated_at"] <= receipt["completed_at"] <= job["updated_at"]
        or set(output)
        != {
            "build_id",
            "failure_code",
            "failure_stage",
            "diagnostic_codes",
            "source_sha256",
            "build_identity",
        }
        or output.get("build_id") != row["build_id"]
        or not isinstance(failure_stage, str)
        or failure_stage
        not in {"VALIDATE_SOURCE", "COMPILE", "PUBLIC_TEST", "HIDDEN_TEST", "CERTIFY"}
        or failure.get("stage") != failure_stage
        or not isinstance(diagnostics, list)
        or any(not isinstance(item, str) for item in diagnostics)
        or details.get("pipeline_code") != output.get("failure_code")
        or details.get("diagnostic_codes") != diagnostics
        or output.get("source_sha256") != source_sha256
        or not isinstance(output.get("build_identity"), str)
        or not output.get("build_identity")
        or not _legacy_rejected_build_phases_match(
            build.get("phases"),
            row["updated_at"],
            failure_stage,
            diagnostics,
        )
    ):
        raise RuntimeError("legacy Build terminal receipt authority is corrupt")


def _legacy_rejected_build_phases_match(
    value: object,
    finished_at: object,
    failed_stage: str,
    diagnostics: list[object],
) -> bool:
    names = ("VALIDATE_SOURCE", "COMPILE", "PUBLIC_TEST", "HIDDEN_TEST", "CERTIFY")
    if not isinstance(value, list) or len(value) != len(names):
        return False
    if not isinstance(finished_at, datetime) or finished_at.tzinfo is None:
        return False
    failure_index = names.index(failed_stage)
    started_at: datetime | None = None
    for index, (name, raw) in enumerate(zip(names, value, strict=True)):
        if not isinstance(raw, Mapping) or set(raw) != {
            "name",
            "status",
            "started_at",
            "finished_at",
            "diagnostic_codes",
        }:
            return False
        if raw.get("name") != name:
            return False
        if index > failure_index:
            if raw != {
                "name": name,
                "status": "SKIPPED",
                "started_at": None,
                "finished_at": None,
                "diagnostic_codes": [],
            }:
                return False
            continue
        try:
            phase_started = datetime.fromisoformat(
                str(raw.get("started_at")).replace("Z", "+00:00")
            )
            phase_finished = datetime.fromisoformat(
                str(raw.get("finished_at")).replace("Z", "+00:00")
            )
        except ValueError:
            return False
        if phase_started.tzinfo is None or phase_finished.tzinfo is None:
            return False
        if started_at is None:
            started_at = phase_started
        expected_codes = diagnostics if index == failure_index else []
        expected_status = "FAILED" if index == failure_index else "PASSED"
        if (
            raw.get("status") != expected_status
            or raw.get("diagnostic_codes") != expected_codes
            or phase_started != started_at
            or phase_finished != finished_at
            or phase_started > phase_finished
        ):
            return False
    return True


def _assert_int2_provenance_completeness() -> None:
    """Every pre-cutover business authority must receive exactly one seal."""

    connection = op.get_bind()
    pairs = (
        ("skill_builds", "skill_build_provenance"),
        ("skill_certifications", "skill_certification_provenance"),
        ("skill_activations", "skill_activation_provenance"),
        ("game_runs", "skill_run_provenance"),
    )
    for source, projection in pairs:
        source_count = int(connection.scalar(sa.text(f"SELECT count(*) FROM {source}")) or 0)
        projection_count = int(
            connection.scalar(sa.text(f"SELECT count(*) FROM {projection}")) or 0
        )
        if source_count != projection_count:
            raise RuntimeError(
                f"legacy {source} authority closure is incomplete: "
                f"{source_count} source rows, {projection_count} sealed rows"
            )
    terminal_build_count = int(
        connection.scalar(sa.text("SELECT count(*) FROM skill_builds WHERE terminal")) or 0
    )
    terminal_seal_count = int(
        connection.scalar(sa.text("SELECT count(*) FROM skill_build_terminal_authority")) or 0
    )
    if terminal_build_count != terminal_seal_count:
        raise RuntimeError(
            "legacy terminal Build authority closure is incomplete: "
            f"{terminal_build_count} source rows, {terminal_seal_count} sealed rows"
        )


def _backfill_legacy_certification_provenance(
    connection: sa.Connection,
    *,
    build: Mapping[str, object],
    build_request: Mapping[str, object],
    build_authority_sha256: str,
    workflow: Mapping[str, object],
    certification: Mapping[str, object],
    artifact_authority_sha256: str,
) -> str:
    terminal_workflow = (
        connection.execute(
            sa.text(
                "SELECT job_id,tenant_id,command_id,operation,subject_type,subject_id,"
                "phase,status,attempt,fencing_token,lease_owner,lease_expires_at,"
                "next_attempt_at,request_sha256,job_json,last_error_json,created_at,updated_at "
                "FROM workflow_jobs WHERE tenant_id=:tenant_id AND job_id=:job_id"
            ),
            {**dict(build), **dict(workflow)},
        )
        .mappings()
        .one_or_none()
    )
    if terminal_workflow is None:
        raise RuntimeError("legacy Certification workflow authority is missing")
    workflow = terminal_workflow
    command = (
        connection.execute(
            sa.text(
                "SELECT command_id,tenant_id,actor_id,command_type,status,revision,terminal,"
                "accepted_at,updated_at,record_json FROM commands WHERE "
                "tenant_id=:tenant_id AND command_id=:command_id"
            ),
            {**dict(build), **dict(workflow)},
        )
        .mappings()
        .one_or_none()
    )
    receipt = (
        connection.execute(
            sa.text(
                "SELECT receipt_id,tenant_id,job_id,step_name,fencing_token,input_sha256,"
                "output_sha256,receipt_json,completed_at "
                "FROM job_step_receipts WHERE tenant_id=:tenant_id AND job_id=:job_id "
                "AND step_name='BUILD_CERTIFIED'"
            ),
            {**dict(build), **dict(workflow)},
        )
        .mappings()
        .one_or_none()
    )
    wire = certification["certification_json"]
    output = receipt["receipt_json"] if receipt is not None else None
    if (
        workflow.get("status") != "SUCCEEDED"
        or command is None
        or receipt is None
        or receipt["input_sha256"] != workflow.get("request_sha256")
        or not isinstance(output, Mapping)
        or _canonical_sha256(output) != receipt["output_sha256"]
        or output.get("build_id") != build["build_id"]
        or output.get("artifact_sha256") != certification["artifact_sha256"]
        or output.get("certification_id") != certification["certification_id"]
        or not isinstance(wire, Mapping)
        or not isinstance(wire.get("policy_sha256"), str)
        or _SHA256.fullmatch(wire["policy_sha256"]) is None
    ):
        raise RuntimeError("legacy Certification workflow authority is corrupt")
    workflow_authority_sha256 = _canonical_sha256(_certification_workflow_job_authority(workflow))
    command_authority_sha256 = _canonical_sha256(_certification_command_authority(command))
    receipt_authority_sha256 = _canonical_sha256(_certification_receipt_authority(receipt))
    authority = {
        "authority_type": "SKILL_CERTIFICATION_PROVENANCE",
        "authority_version": "1.0.0",
        "certification_id": certification["certification_id"],
        "tenant_id": build["tenant_id"],
        "actor_id": build["actor_id"],
        "build_id": build["build_id"],
        "build_authority_sha256": build_authority_sha256,
        "build_request_sha256": _canonical_sha256(build_request),
        "workflow_job_id": workflow["job_id"],
        "workflow_request_sha256": workflow["request_sha256"],
        "workflow_job_sha256": workflow_authority_sha256,
        "command_authority_sha256": command_authority_sha256,
        "build_receipt_id": receipt["receipt_id"],
        "build_receipt_sha256": receipt["output_sha256"],
        "build_receipt_authority_sha256": receipt_authority_sha256,
        "policy_sha256": wire["policy_sha256"],
        "artifact_sha256": certification["artifact_sha256"],
        "artifact_authority_sha256": artifact_authority_sha256,
        "certification_sha256": certification["certification_sha256"],
    }
    digest = _canonical_sha256(authority)
    connection.execute(
        sa.text(
            "INSERT INTO skill_certification_provenance "
            "(certification_id,tenant_id,actor_id,build_id,build_authority_sha256,"
            "build_request_sha256,workflow_job_id,workflow_request_sha256,"
            "workflow_job_sha256,command_authority_sha256,build_receipt_id,"
            "build_receipt_sha256,build_receipt_authority_sha256,policy_sha256,artifact_sha256,"
            "artifact_authority_sha256,certification_sha256,authority_sha256,created_at) "
            "VALUES (:certification_id,:tenant_id,:actor_id,:build_id,"
            ":build_authority_sha256,:build_request_sha256,:workflow_job_id,"
            ":workflow_request_sha256,:workflow_job_sha256,:command_authority_sha256,"
            ":build_receipt_id,:build_receipt_sha256,:build_receipt_authority_sha256,"
            ":policy_sha256,:artifact_sha256,:artifact_authority_sha256,"
            ":certification_sha256,:authority_sha256,:created_at)"
        ),
        {
            **authority,
            "authority_sha256": digest,
            "created_at": certification["certified_at"],
        },
    )
    return digest


def _certification_workflow_job_authority(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "authority_type": "SKILL_CERTIFICATION_WORKFLOW_JOB",
        "authority_version": "1.0.0",
        "job_id": row["job_id"],
        "tenant_id": row["tenant_id"],
        "command_id": row["command_id"],
        "operation": row["operation"],
        "subject_type": row["subject_type"],
        "subject_id": row["subject_id"],
        "phase": row["phase"],
        "status": row["status"],
        "attempt": row["attempt"],
        "fencing_token": row["fencing_token"],
        "lease_owner": row["lease_owner"],
        "lease_expires_at": _iso_or_none(row["lease_expires_at"]),
        "next_attempt_at": _iso_or_none(row["next_attempt_at"]),
        "request_sha256": row["request_sha256"],
        "job": row["job_json"],
        "last_error": row["last_error_json"],
        "created_at": _iso_datetime(row["created_at"]),
        "updated_at": _iso_datetime(row["updated_at"]),
    }


def _certification_command_authority(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "authority_type": "SKILL_CERTIFICATION_COMMAND",
        "authority_version": "1.0.0",
        "command_id": row["command_id"],
        "tenant_id": row["tenant_id"],
        "actor_id": row["actor_id"],
        "command_type": row["command_type"],
        "status": row["status"],
        "revision": row["revision"],
        "terminal": row["terminal"],
        "accepted_at": _iso_datetime(row["accepted_at"]),
        "updated_at": _iso_datetime(row["updated_at"]),
        "record": row["record_json"],
    }


def _certification_receipt_authority(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "authority_type": "SKILL_CERTIFICATION_RECEIPT",
        "authority_version": "1.0.0",
        "receipt_id": row["receipt_id"],
        "tenant_id": row["tenant_id"],
        "job_id": row["job_id"],
        "step_name": row["step_name"],
        "fencing_token": row["fencing_token"],
        "input_sha256": row["input_sha256"],
        "output_sha256": row["output_sha256"],
        "receipt": row["receipt_json"],
        "completed_at": _iso_datetime(row["completed_at"]),
    }


def _backfill_legacy_activation_provenance(
    connection: sa.Connection,
    *,
    build: Mapping[str, object],
    build_authority_sha256: str,
    certification: Mapping[str, object],
    certification_authority_sha256: str,
    artifact_authority_sha256: str,
) -> None:
    """Seal every historical Activation without using a mutable current head."""

    expected_count = int(
        connection.scalar(
            sa.text(
                "SELECT count(*) FROM skill_activations WHERE tenant_id=:tenant_id "
                "AND actor_id=:actor_id AND certification_id=:certification_id "
                "AND artifact_sha256=:artifact_sha256"
            ),
            {
                **dict(build),
                "certification_id": certification["certification_id"],
                "artifact_sha256": certification["artifact_sha256"],
            },
        )
        or 0
    )
    rows = list(
        connection.execute(
            sa.text(
                "SELECT activation.activation_id,activation.tenant_id,"
                "activation.actor_id,activation.certification_id,"
                "activation.artifact_sha256,activation.registry_revision,"
                "activation.activation_sha256,activation.activation_json,"
                "activation.activated_at,entry.entry_sha256,entry.entry_json,"
                "job.job_id,job.command_id,job.operation,job.subject_type,"
                "job.subject_id,job.phase,job.request_sha256,job.job_json,job.status,"
                "job.attempt,job.fencing_token,job.lease_owner,job.lease_expires_at,"
                "job.next_attempt_at,job.last_error_json,job.created_at AS job_created_at,"
                "job.updated_at AS job_updated_at,receipt.receipt_id,"
                "receipt.step_name,receipt.fencing_token AS receipt_fencing_token,"
                "receipt.input_sha256,receipt.output_sha256,receipt.receipt_json,"
                "receipt.completed_at FROM skill_activations AS activation "
                "JOIN registry_entries AS entry ON "
                "entry.tenant_id=activation.tenant_id AND "
                "entry.actor_id=activation.actor_id AND "
                "entry.content_hash=activation.content_hash AND "
                "entry.world_id=activation.world_id AND "
                "entry.agent_profile_id=activation.agent_profile_id AND "
                "entry.revision=activation.registry_revision AND "
                "entry.skill_id=activation.skill_id AND "
                "entry.skill_version_id=activation.skill_version_id AND "
                "entry.certification_id=activation.certification_id AND "
                "entry.artifact_sha256=activation.artifact_sha256 "
                "JOIN workflow_jobs AS job ON job.tenant_id=activation.tenant_id "
                "AND job.operation='ACTIVATE_SKILL_VERSION' "
                "AND job.subject_type='SKILL_ACTIVATION' "
                "AND job.subject_id=activation.activation_id "
                "JOIN job_step_receipts AS receipt ON "
                "receipt.tenant_id=job.tenant_id AND receipt.job_id=job.job_id "
                "AND receipt.step_name='REGISTRY_ACTIVATED' "
                "WHERE activation.tenant_id=:tenant_id "
                "AND activation.actor_id=:actor_id "
                "AND activation.certification_id=:certification_id "
                "AND activation.artifact_sha256=:artifact_sha256"
            ),
            {
                **dict(build),
                "certification_id": certification["certification_id"],
                "artifact_sha256": certification["artifact_sha256"],
            },
        ).mappings()
    )
    if len(rows) != expected_count:
        raise RuntimeError("legacy Activation closure is incomplete")
    for row in rows:
        wire = row["activation_json"]
        entry = row["entry_json"]
        old_receipt = row["receipt_json"]
        job_json = row["job_json"]
        expected_job_id = _scoped_identifier("job", str(row["tenant_id"]), str(row["command_id"]))
        expected_receipt_id = _scoped_identifier(
            "receipt",
            str(row["tenant_id"]),
            str(row["job_id"]),
            "REGISTRY_ACTIVATED",
        )
        if (
            row["status"] != "SUCCEEDED"
            or row["phase"] != "COMPLETE"
            or row["operation"] != "ACTIVATE_SKILL_VERSION"
            or row["subject_type"] != "SKILL_ACTIVATION"
            or row["subject_id"] != row["activation_id"]
            or row["job_id"] != expected_job_id
            or row["receipt_id"] != expected_receipt_id
            or row["step_name"] != "REGISTRY_ACTIVATED"
            or row["attempt"] < 1
            or row["fencing_token"] < 1
            or row["receipt_fencing_token"] != row["fencing_token"]
            or row["lease_owner"] is not None
            or row["lease_expires_at"] is not None
            or row["next_attempt_at"] is not None
            or row["last_error_json"] is not None
            or row["completed_at"] > row["job_updated_at"]
            or not isinstance(job_json, Mapping)
            or set(job_json)
            != {
                "schema_version",
                "request_context",
                "activation_id",
                "authority_id",
                "expected_registry_revision",
                "activation_scope",
                "skill",
                "reason",
            }
            or job_json.get("schema_version") != "1.0.0"
            or job_json.get("activation_id") != row["activation_id"]
            or job_json.get("authority_id") != entry.get("authority_id")
            or job_json.get("expected_registry_revision") != wire.get("previous_registry_revision")
            or not isinstance(wire, Mapping)
            or _canonical_sha256(wire) != row["activation_sha256"]
            or wire.get("activation_id") != row["activation_id"]
            or wire.get("certification_id") != row["certification_id"]
            or wire.get("artifact_sha256") != row["artifact_sha256"]
            or wire.get("registry_revision") != row["registry_revision"]
            or not isinstance(entry, Mapping)
            or _canonical_sha256(entry) != row["entry_sha256"]
            or entry.get("activation_id") != row["activation_id"]
            or entry.get("certification_id") != row["certification_id"]
            or entry.get("artifact_sha256") != row["artifact_sha256"]
            or entry.get("revision") != row["registry_revision"]
            or row["input_sha256"] != row["request_sha256"]
            or not isinstance(old_receipt, Mapping)
            or set(old_receipt)
            != {
                "activation_id",
                "previous_registry_revision",
                "registry_revision",
                "entry_sha256",
                "activation_sha256",
            }
            or old_receipt.get("activation_id") != row["activation_id"]
            or old_receipt.get("registry_revision") != row["registry_revision"]
            or old_receipt.get("entry_sha256") != row["entry_sha256"]
            or old_receipt.get("activation_sha256") != row["activation_sha256"]
            or _canonical_sha256(old_receipt) != row["output_sha256"]
        ):
            raise RuntimeError("legacy Activation authority is corrupt")
        updated_job_json = {
            **dict(job_json),
            "build_provenance_sha256": build_authority_sha256,
            "certification_sha256": certification["certification_sha256"],
            "artifact_authority_sha256": artifact_authority_sha256,
        }
        updated_receipt = {
            **dict(old_receipt),
            "certification_sha256": certification["certification_sha256"],
            "artifact_authority_sha256": artifact_authority_sha256,
            "build_provenance_sha256": build_authority_sha256,
        }
        workflow_job_authority = {
            "authority_type": "SKILL_ACTIVATION_WORKFLOW_JOB",
            "authority_version": "1.0.0",
            "job_id": row["job_id"],
            "tenant_id": row["tenant_id"],
            "command_id": row["command_id"],
            "operation": row["operation"],
            "subject_type": row["subject_type"],
            "subject_id": row["subject_id"],
            "phase": row["phase"],
            "status": row["status"],
            "attempt": row["attempt"],
            "fencing_token": row["fencing_token"],
            "lease_owner": row["lease_owner"],
            "lease_expires_at": _iso_or_none(row["lease_expires_at"]),
            "next_attempt_at": _iso_or_none(row["next_attempt_at"]),
            "request_sha256": row["request_sha256"],
            "job": updated_job_json,
            "last_error": row["last_error_json"],
            "created_at": _iso_or_none(row["job_created_at"]),
            "updated_at": _iso_or_none(row["job_updated_at"]),
        }
        activation_receipt_authority = {
            "authority_type": "SKILL_ACTIVATION_RECEIPT",
            "authority_version": "1.0.0",
            "receipt_id": row["receipt_id"],
            "tenant_id": row["tenant_id"],
            "job_id": row["job_id"],
            "step_name": row["step_name"],
            "fencing_token": row["receipt_fencing_token"],
            "input_sha256": row["input_sha256"],
            "output_sha256": _canonical_sha256(updated_receipt),
            "receipt": updated_receipt,
            "completed_at": _iso_or_none(row["completed_at"]),
        }
        authority = {
            "authority_type": "SKILL_ACTIVATION_PROVENANCE",
            "authority_version": "1.0.0",
            "activation_id": row["activation_id"],
            "tenant_id": row["tenant_id"],
            "actor_id": row["actor_id"],
            "build_id": build["build_id"],
            "build_authority_sha256": build_authority_sha256,
            "certification_id": row["certification_id"],
            "certification_sha256": certification["certification_sha256"],
            "certification_authority_sha256": certification_authority_sha256,
            "artifact_sha256": row["artifact_sha256"],
            "artifact_authority_sha256": artifact_authority_sha256,
            "registry_revision": row["registry_revision"],
            "activation_sha256": row["activation_sha256"],
            "launch_authority_id": entry["authority_id"],
            "entry_sha256": row["entry_sha256"],
            "workflow_job_id": row["job_id"],
            "workflow_request_sha256": row["request_sha256"],
            "workflow_job_sha256": _canonical_sha256(workflow_job_authority),
            "activation_receipt_id": row["receipt_id"],
            "activation_receipt_sha256": _canonical_sha256(activation_receipt_authority),
        }
        authority_sha256 = _canonical_sha256(authority)
        connection.execute(
            sa.text(
                "INSERT INTO skill_activation_provenance "
                "(activation_id,tenant_id,actor_id,build_id,"
                "build_authority_sha256,certification_id,certification_sha256,"
                "certification_authority_sha256,artifact_sha256,"
                "artifact_authority_sha256,registry_revision,"
                "activation_sha256,launch_authority_id,entry_sha256,"
                "workflow_job_id,workflow_request_sha256,workflow_job_sha256,"
                "activation_receipt_id,activation_receipt_sha256,"
                "authority_sha256,created_at) VALUES "
                "(:activation_id,:tenant_id,:actor_id,:build_id,"
                ":build_authority_sha256,:certification_id,:certification_sha256,"
                ":certification_authority_sha256,:artifact_sha256,"
                ":artifact_authority_sha256,:registry_revision,"
                ":activation_sha256,:launch_authority_id,:entry_sha256,"
                ":workflow_job_id,:workflow_request_sha256,:workflow_job_sha256,"
                ":activation_receipt_id,:activation_receipt_sha256,"
                ":authority_sha256,:created_at)"
            ),
            {
                **dict(row),
                "build_id": build["build_id"],
                "build_authority_sha256": build_authority_sha256,
                "certification_sha256": certification["certification_sha256"],
                "certification_authority_sha256": certification_authority_sha256,
                "artifact_authority_sha256": artifact_authority_sha256,
                "authority_sha256": authority_sha256,
                "created_at": row["activated_at"],
            },
        )
        connection.execute(
            sa.text(
                "UPDATE workflow_jobs SET job_json=CAST(:job_json AS jsonb) "
                "WHERE tenant_id=:tenant_id AND job_id=:job_id"
            ),
            {
                **dict(row),
                "job_json": json.dumps(updated_job_json, ensure_ascii=False),
            },
        )
        connection.execute(
            sa.text(
                "UPDATE job_step_receipts SET receipt_json=CAST(:receipt AS jsonb),"
                "output_sha256=:output_sha256 WHERE tenant_id=:tenant_id "
                "AND receipt_id=:receipt_id"
            ),
            {
                **dict(row),
                "receipt": json.dumps(updated_receipt, ensure_ascii=False),
                "output_sha256": _canonical_sha256(updated_receipt),
            },
        )


def _legacy_build_command_receipt_authority(
    row: Mapping[str, object],
) -> dict[str, object]:
    return {
        "authority_type": "SKILL_BUILD_COMMAND_RECEIPT",
        "authority_version": "1.0.0",
        "receipt_id": row["receipt_id"],
        "tenant_id": row["tenant_id"],
        "actor_id": row["actor_id"],
        "operation": row["operation"],
        "idempotency_key": row["idempotency_key"],
        "request_sha256": row["request_sha256"],
        "command_id": row["command_id"],
        "accepted_at": _iso_datetime(row["accepted_at"]),
    }


def _legacy_build_authority(
    row: Mapping[str, object], source_bundle_sha256: str
) -> dict[str, object]:
    return {
        "authority_type": "SKILL_BUILD_PROVENANCE",
        "authority_version": "1.0.0",
        "build_id": row["build_id"],
        "provenance_kind": "LEGACY_V04",
        "legacy_marker_id": row["legacy_marker_id"],
        "tenant_id": row["tenant_id"],
        "actor_id": row["actor_id"],
        "build_request_sha256": row["build_request_sha256"],
        "command_receipt_id": row["command_receipt_id"],
        "command_receipt_authority_sha256": row["command_receipt_authority_sha256"],
        "workflow_job_id": row["workflow_job_id"],
        "workflow_request_sha256": row["workflow_request_sha256"],
        "session_id": None,
        "draft_id": None,
        "skill_id": row["skill_id"],
        "draft_revision_row_id": None,
        "draft_revision": None,
        "draft_sha256": None,
        "source_bundle_sha256": source_bundle_sha256,
        "origin_accepted_revision_row_id": None,
        "patch_id": None,
        "patch_decision_id": None,
        "assistance_authority": "NONE",
    }


def _legacy_build_terminal_command_authority(
    row: Mapping[str, object],
) -> dict[str, object]:
    return {
        "authority_type": "SKILL_BUILD_TERMINAL_COMMAND",
        "authority_version": "1.0.0",
        "command_id": row["command_id"],
        "tenant_id": row["tenant_id"],
        "actor_id": row["actor_id"],
        "command_type": row["command_type"],
        "status": row["status"],
        "revision": row["revision"],
        "terminal": row["terminal"],
        "accepted_at": _iso_datetime(row["accepted_at"]),
        "updated_at": _iso_datetime(row["updated_at"]),
        "record": row["record_json"],
    }


def _legacy_build_terminal_workflow_authority(
    row: Mapping[str, object],
) -> dict[str, object]:
    return {
        "authority_type": "SKILL_BUILD_TERMINAL_WORKFLOW",
        "authority_version": "1.0.0",
        "job_id": row["job_id"],
        "tenant_id": row["tenant_id"],
        "command_id": row["command_id"],
        "operation": row["operation"],
        "subject_type": row["subject_type"],
        "subject_id": row["subject_id"],
        "phase": row["phase"],
        "status": row["status"],
        "attempt": row["attempt"],
        "fencing_token": row["fencing_token"],
        "lease_owner": row["lease_owner"],
        "lease_expires_at": _iso_or_none(row["lease_expires_at"]),
        "next_attempt_at": _iso_or_none(row["next_attempt_at"]),
        "request_sha256": row["request_sha256"],
        "job": row["job_json"],
        "last_error": row["last_error_json"],
        "created_at": _iso_datetime(row["created_at"]),
        "updated_at": _iso_datetime(row["updated_at"]),
    }


def _legacy_build_terminal_receipt_authority(
    row: Mapping[str, object],
) -> dict[str, object]:
    return {
        "authority_type": "SKILL_BUILD_TERMINAL_RECEIPT",
        "authority_version": "1.0.0",
        "receipt_id": row["receipt_id"],
        "tenant_id": row["tenant_id"],
        "job_id": row["job_id"],
        "step_name": row["step_name"],
        "fencing_token": row["fencing_token"],
        "input_sha256": row["input_sha256"],
        "output_sha256": row["output_sha256"],
        "receipt": row["receipt_json"],
        "completed_at": _iso_datetime(row["completed_at"]),
    }


def _legacy_build_terminal_authority(
    *,
    build: Mapping[str, object],
    build_authority_sha256: str,
    command: Mapping[str, object],
    workflow: Mapping[str, object],
    receipt: Mapping[str, object],
    certification_id: object,
    certification_authority_sha256: object,
) -> dict[str, object]:
    return {
        "authority_type": "SKILL_BUILD_TERMINAL_AUTHORITY",
        "authority_version": "1.0.0",
        "build_id": build["build_id"],
        "tenant_id": build["tenant_id"],
        "actor_id": build["actor_id"],
        "build_authority_sha256": build_authority_sha256,
        "terminal_status": build["status"],
        "command_id": command["command_id"],
        "command_authority_sha256": _canonical_sha256(
            _legacy_build_terminal_command_authority(command)
        ),
        "workflow_job_id": workflow["job_id"],
        "workflow_job_sha256": _canonical_sha256(
            _legacy_build_terminal_workflow_authority(workflow)
        ),
        "terminal_receipt_id": receipt["receipt_id"],
        "terminal_receipt_authority_sha256": _canonical_sha256(
            _legacy_build_terminal_receipt_authority(receipt)
        ),
        "certification_id": certification_id,
        "certification_authority_sha256": certification_authority_sha256,
    }


def _backfill_legacy_build_terminal_authority(
    connection: sa.engine.Connection,
    *,
    build: Mapping[str, object],
    build_authority_sha256: str,
    command: Mapping[str, object],
    workflow: Mapping[str, object],
    receipt: Mapping[str, object],
    certification_id: object,
    certification_authority_sha256: object,
) -> None:
    authority = _legacy_build_terminal_authority(
        build=build,
        build_authority_sha256=build_authority_sha256,
        command=command,
        workflow=workflow,
        receipt=receipt,
        certification_id=certification_id,
        certification_authority_sha256=certification_authority_sha256,
    )
    connection.execute(
        sa.text(
            "INSERT INTO skill_build_terminal_authority "
            "(build_id,tenant_id,actor_id,build_authority_sha256,terminal_status,"
            "command_id,command_authority_sha256,workflow_job_id,workflow_job_sha256,"
            "terminal_receipt_id,terminal_receipt_authority_sha256,certification_id,"
            "certification_authority_sha256,authority_sha256,created_at) VALUES "
            "(:build_id,:tenant_id,:actor_id,:build_authority_sha256,:terminal_status,"
            ":command_id,:command_authority_sha256,:workflow_job_id,:workflow_job_sha256,"
            ":terminal_receipt_id,:terminal_receipt_authority_sha256,:certification_id,"
            ":certification_authority_sha256,:authority_sha256,:created_at)"
        ),
        {
            **authority,
            "authority_sha256": _canonical_sha256(authority),
            "created_at": receipt["completed_at"],
        },
    )


def _backfill_legacy_learner_objectives() -> None:
    """Freeze conservative NONE assistance for already-projected v0.4 Runs."""

    connection = op.get_bind()
    legacy_rows = list(
        connection.execute(
            sa.text(
                "SELECT job_id,tenant_id,status,fencing_token,projection_json,"
                "request_sha256,result_json,result_sha256,completed_at "
                "FROM learner_projection_jobs ORDER BY job_id"
            )
        ).mappings()
    )
    for legacy in legacy_rows:
        objective = legacy["projection_json"]
        if not isinstance(objective, Mapping) or legacy["request_sha256"] != _canonical_sha256(
            objective
        ):
            raise RuntimeError("legacy Learner objective request bytes drifted")
        if legacy["status"] != "SUCCEEDED":
            continue
        result = legacy["result_json"]
        receipt = (
            connection.execute(
                sa.text(
                    "SELECT receipt_id,step_name,fencing_token,input_sha256,output_sha256,"
                    "receipt_json,completed_at FROM job_step_receipts "
                    "WHERE tenant_id=:tenant_id AND job_id=:job_id "
                    "AND step_name='LEARNER_PROJECTION_COMMITTED'"
                ),
                dict(legacy),
            )
            .mappings()
            .one_or_none()
        )
        if (
            not isinstance(result, Mapping)
            or legacy["result_sha256"] != _canonical_sha256(result)
            or legacy["completed_at"] is None
            or receipt is None
        ):
            raise RuntimeError("legacy Learner terminal result bytes drifted")
        expected_receipt_id = _scoped_identifier(
            "receipt",
            str(legacy["tenant_id"]),
            str(legacy["job_id"]),
            "LEARNER_PROJECTION_COMMITTED",
        )
        receipt_wire = {
            "receipt_id": receipt["receipt_id"],
            "step_name": receipt["step_name"],
            "fencing_token": receipt["fencing_token"],
            "input_sha256": receipt["input_sha256"],
            "output_sha256": receipt["output_sha256"],
            "receipt_json": receipt["receipt_json"],
            "completed_at": _iso_datetime(receipt["completed_at"]),
        }
        if (
            receipt["receipt_id"] != expected_receipt_id
            or receipt["step_name"] != "LEARNER_PROJECTION_COMMITTED"
            or receipt["fencing_token"] != legacy["fencing_token"]
            or receipt["input_sha256"] != legacy["request_sha256"]
            or not isinstance(receipt["receipt_json"], Mapping)
            or receipt["output_sha256"] != _canonical_sha256(receipt["receipt_json"])
            or result.get("projection_receipt") != receipt_wire
        ):
            raise RuntimeError("legacy Learner terminal receipt bytes drifted")
    learner_count = int(
        connection.scalar(sa.text("SELECT count(*) FROM learner_projection_jobs")) or 0
    )
    rows = list(
        connection.execute(
            sa.text(
                "SELECT learner.job_id,learner.tenant_id,learner.actor_id,"
                "learner.session_id,learner.command_id,learner.content_hash,"
                "learner.run_id,learner.status,"
                "learner.projection_json,learner.result_json,"
                "run.authority_sha256 AS run_authority_sha256,"
                "run.provenance_kind,run.build_id,run.build_authority_sha256,"
                "run.activation_id,run.activation_sha256,"
                "run.activation_authority_sha256,run.registry_revision,"
                "run.certification_id,"
                "run.certification_sha256,run.certification_authority_sha256,"
                "run.artifact_sha256,"
                "run.artifact_authority_sha256,run.draft_revision_row_id,"
                "run.draft_sha256,run.assistance_authority,build.authority_sha256 "
                "AS build_sha,build.origin_accepted_revision_row_id,build.patch_id,"
                "build.patch_decision_id FROM learner_projection_jobs AS learner "
                "JOIN game_runs AS game ON game.run_id=learner.run_id "
                "AND game.tenant_id=learner.tenant_id "
                "AND game.actor_id=learner.actor_id "
                "AND game.session_id=learner.session_id "
                "AND game.command_id=learner.command_id "
                "AND game.content_hash=learner.content_hash "
                "JOIN skill_run_provenance AS run ON run.run_id=learner.run_id "
                "AND run.tenant_id=learner.tenant_id "
                "AND run.actor_id=learner.actor_id "
                "AND run.session_id=learner.session_id "
                "JOIN skill_build_provenance AS build ON build.build_id=run.build_id "
                "AND build.tenant_id=learner.tenant_id "
                "AND build.actor_id=learner.actor_id"
            )
        ).mappings()
    )
    if learner_count != len(rows) or len({row["job_id"] for row in rows}) != len(rows):
        raise RuntimeError("legacy Learner projection authority closure is incomplete")
    for row in rows:
        objective = row["projection_json"]
        if not isinstance(objective, Mapping) or "assistance" in objective:
            raise RuntimeError("legacy Learner objective shape cannot be backfilled")
        if (
            row["provenance_kind"] != "LEGACY_V04"
            or row["assistance_authority"] != "NONE"
            or row["build_authority_sha256"] != row["build_sha"]
        ):
            raise RuntimeError("legacy Learner provenance is not conservative")
        assistance = {
            "authority_version": "1.0.0",
            "provenance_kind": row["provenance_kind"],
            "run_id": row["run_id"],
            "run_authority_sha256": row["run_authority_sha256"],
            "build_id": row["build_id"],
            "build_authority_sha256": row["build_sha"],
            "activation_id": row["activation_id"],
            "activation_sha256": row["activation_sha256"],
            "activation_authority_sha256": row["activation_authority_sha256"],
            "registry_revision": row["registry_revision"],
            "certification_id": row["certification_id"],
            "certification_sha256": row["certification_sha256"],
            "certification_authority_sha256": row["certification_authority_sha256"],
            "artifact_sha256": row["artifact_sha256"],
            "artifact_authority_sha256": row["artifact_authority_sha256"],
            "draft_revision_row_id": row["draft_revision_row_id"],
            "origin_accepted_revision_row_id": row["origin_accepted_revision_row_id"],
            "draft_sha256": row["draft_sha256"],
            "assistance_authority": row["assistance_authority"],
            "patch_id": row["patch_id"],
            "patch_decision_id": row["patch_decision_id"],
            "used_skill_patch": False,
        }
        updated = {**dict(objective), "assistance": assistance}
        request_sha256 = _canonical_sha256(updated)
        receipt = (
            connection.execute(
                sa.text(
                    "SELECT receipt_id,step_name,fencing_token,input_sha256,output_sha256,"
                    "receipt_json,completed_at FROM job_step_receipts "
                    "WHERE tenant_id=:tenant_id AND job_id=:job_id "
                    "AND step_name='LEARNER_PROJECTION_COMMITTED'"
                ),
                dict(row),
            )
            .mappings()
            .one_or_none()
        )
        connection.execute(
            sa.text(
                "UPDATE learner_projection_jobs SET projection_json=CAST(:objective AS jsonb), "
                "request_sha256=:request_sha256 WHERE tenant_id=:tenant_id "
                "AND job_id=:job_id"
            ),
            {
                **dict(row),
                "objective": json.dumps(updated, ensure_ascii=False),
                "request_sha256": request_sha256,
            },
        )
        connection.execute(
            sa.text(
                "UPDATE job_step_receipts SET input_sha256=:request_sha256 "
                "WHERE tenant_id=:tenant_id AND job_id=:job_id "
                "AND step_name='LEARNER_PROJECTION_COMMITTED'"
            ),
            {
                **dict(row),
                "request_sha256": request_sha256,
            },
        )
        if row["status"] == "SUCCEEDED":
            result = row["result_json"]
            if not isinstance(result, Mapping) or receipt is None:
                raise RuntimeError("legacy terminal Learner closure is incomplete")
            old_wire = {
                "receipt_id": receipt["receipt_id"],
                "step_name": receipt["step_name"],
                "fencing_token": receipt["fencing_token"],
                "input_sha256": receipt["input_sha256"],
                "output_sha256": receipt["output_sha256"],
                "receipt_json": receipt["receipt_json"],
                "completed_at": _iso_datetime(receipt["completed_at"]),
            }
            if result.get("projection_receipt") != old_wire:
                raise RuntimeError("legacy terminal Learner receipt bytes drifted")
            new_wire = {**old_wire, "input_sha256": request_sha256}
            updated_result = {**dict(result), "projection_receipt": new_wire}
            connection.execute(
                sa.text(
                    "UPDATE learner_projection_jobs SET result_json=CAST(:result AS jsonb), "
                    "result_sha256=:result_sha256 WHERE tenant_id=:tenant_id "
                    "AND job_id=:job_id"
                ),
                {
                    **dict(row),
                    "result": json.dumps(updated_result, ensure_ascii=False),
                    "result_sha256": _canonical_sha256(updated_result),
                },
            )


def _downgrade_legacy_learner_objectives() -> None:
    connection = op.get_bind()
    int2_state = int(
        connection.scalar(
            sa.text(
                "SELECT "
                "(SELECT count(*) FROM skill_build_provenance "
                "WHERE provenance_kind <> 'LEGACY_V04') + "
                "(SELECT count(*) FROM product_skill_patch_proposals) + "
                "(SELECT count(*) FROM product_skill_patch_decisions) + "
                "(SELECT count(*) FROM product_draft_revision_assistance) + "
                "(SELECT count(*) FROM product_skill_draft_revisions "
                "WHERE source_kind <> 'STUDENT' OR parent_revision_row_id IS NOT NULL)"
            )
        )
        or 0
    )
    if int2_state:
        raise RuntimeError("cannot downgrade after INT2 Patch authority exists")
    rows = connection.execute(
        sa.text(
            "SELECT job_id,tenant_id,actor_id,session_id,run_id,status,fencing_token,"
            "projection_json,"
            "request_sha256,result_json,result_sha256,completed_at "
            "FROM learner_projection_jobs"
        )
    ).mappings()
    for row in rows:
        objective = row["projection_json"]
        if not isinstance(objective, Mapping) or not isinstance(
            objective.get("assistance"), Mapping
        ):
            raise RuntimeError("INT2 Learner objective is not downgrade-compatible")
        receipt = (
            connection.execute(
                sa.text(
                    "SELECT receipt_id,step_name,fencing_token,input_sha256,output_sha256,"
                    "receipt_json,completed_at FROM job_step_receipts "
                    "WHERE tenant_id=:tenant_id AND job_id=:job_id "
                    "AND step_name='LEARNER_PROJECTION_COMMITTED'"
                ),
                dict(row),
            )
            .mappings()
            .one_or_none()
        )
        if row["request_sha256"] != _canonical_sha256(objective):
            raise RuntimeError("INT2 Learner objective request bytes drifted")
        if row["status"] == "SUCCEEDED":
            result = row["result_json"]
            if (
                not isinstance(result, Mapping)
                or row["result_sha256"] != _canonical_sha256(result)
                or row["completed_at"] is None
                or receipt is None
            ):
                raise RuntimeError("INT2 Learner terminal result bytes drifted")
            expected_receipt_id = _scoped_identifier(
                "receipt",
                str(row["tenant_id"]),
                str(row["job_id"]),
                "LEARNER_PROJECTION_COMMITTED",
            )
            receipt_wire = {
                "receipt_id": receipt["receipt_id"],
                "step_name": receipt["step_name"],
                "fencing_token": receipt["fencing_token"],
                "input_sha256": receipt["input_sha256"],
                "output_sha256": receipt["output_sha256"],
                "receipt_json": receipt["receipt_json"],
                "completed_at": _iso_datetime(receipt["completed_at"]),
            }
            if (
                receipt["receipt_id"] != expected_receipt_id
                or receipt["step_name"] != "LEARNER_PROJECTION_COMMITTED"
                or receipt["fencing_token"] != row["fencing_token"]
                or receipt["input_sha256"] != row["request_sha256"]
                or not isinstance(receipt["receipt_json"], Mapping)
                or receipt["output_sha256"] != _canonical_sha256(receipt["receipt_json"])
                or result.get("projection_receipt") != receipt_wire
            ):
                raise RuntimeError("INT2 Learner terminal receipt bytes drifted")
        assistance = objective["assistance"]
        provenance = (
            connection.execute(
                sa.text(
                    "SELECT run.provenance_kind,run.run_id,run.authority_sha256 "
                    "AS run_authority_sha256,run.build_id,run.build_authority_sha256,"
                    "run.activation_id,run.activation_sha256,run.activation_authority_sha256,"
                    "run.registry_revision,run.certification_id,run.certification_sha256,"
                    "run.certification_authority_sha256,run.artifact_sha256,"
                    "run.artifact_authority_sha256,run.draft_revision_row_id,"
                    "run.draft_sha256,run.assistance_authority,build.authority_sha256 "
                    "AS build_sha,build.origin_accepted_revision_row_id,build.patch_id,"
                    "build.patch_decision_id FROM skill_run_provenance AS run "
                    "JOIN skill_build_provenance AS build ON build.build_id=run.build_id "
                    "AND build.tenant_id=run.tenant_id AND build.actor_id=run.actor_id "
                    "WHERE run.run_id=:run_id AND run.tenant_id=:tenant_id "
                    "AND run.actor_id=:actor_id AND run.session_id=:session_id"
                ),
                dict(row),
            )
            .mappings()
            .one_or_none()
        )
        expected_assistance = (
            {
                "authority_version": "1.0.0",
                "provenance_kind": provenance["provenance_kind"],
                "run_id": provenance["run_id"],
                "run_authority_sha256": provenance["run_authority_sha256"],
                "build_id": provenance["build_id"],
                "build_authority_sha256": provenance["build_sha"],
                "activation_id": provenance["activation_id"],
                "activation_sha256": provenance["activation_sha256"],
                "activation_authority_sha256": provenance["activation_authority_sha256"],
                "registry_revision": provenance["registry_revision"],
                "certification_id": provenance["certification_id"],
                "certification_sha256": provenance["certification_sha256"],
                "certification_authority_sha256": provenance["certification_authority_sha256"],
                "artifact_sha256": provenance["artifact_sha256"],
                "artifact_authority_sha256": provenance["artifact_authority_sha256"],
                "draft_revision_row_id": provenance["draft_revision_row_id"],
                "origin_accepted_revision_row_id": provenance["origin_accepted_revision_row_id"],
                "draft_sha256": provenance["draft_sha256"],
                "assistance_authority": provenance["assistance_authority"],
                "patch_id": provenance["patch_id"],
                "patch_decision_id": provenance["patch_decision_id"],
                "used_skill_patch": False,
            }
            if provenance is not None
            else None
        )
        if (
            provenance is None
            or provenance["provenance_kind"] != "LEGACY_V04"
            or provenance["assistance_authority"] != "NONE"
            or provenance["build_authority_sha256"] != provenance["build_sha"]
            or assistance != expected_assistance
        ):
            raise RuntimeError("INT2 Learner assistance authority is not downgrade-compatible")
        updated = {key: value for key, value in objective.items() if key != "assistance"}
        request_sha256 = _canonical_sha256(updated)
        connection.execute(
            sa.text(
                "UPDATE learner_projection_jobs SET projection_json=CAST(:objective AS jsonb), "
                "request_sha256=:request_sha256 WHERE tenant_id=:tenant_id "
                "AND job_id=:job_id"
            ),
            {
                **dict(row),
                "objective": json.dumps(updated, ensure_ascii=False),
                "request_sha256": request_sha256,
            },
        )
        connection.execute(
            sa.text(
                "UPDATE job_step_receipts SET input_sha256=:request_sha256 "
                "WHERE tenant_id=:tenant_id AND job_id=:job_id "
                "AND step_name='LEARNER_PROJECTION_COMMITTED'"
            ),
            {
                **dict(row),
                "request_sha256": request_sha256,
            },
        )
        if row["status"] == "SUCCEEDED":
            result = row["result_json"]
            if not isinstance(result, Mapping) or receipt is None:
                raise RuntimeError("INT2 terminal Learner closure is incomplete")
            old_wire = {
                "receipt_id": receipt["receipt_id"],
                "step_name": receipt["step_name"],
                "fencing_token": receipt["fencing_token"],
                "input_sha256": receipt["input_sha256"],
                "output_sha256": receipt["output_sha256"],
                "receipt_json": receipt["receipt_json"],
                "completed_at": _iso_datetime(receipt["completed_at"]),
            }
            if result.get("projection_receipt") != old_wire:
                raise RuntimeError("INT2 terminal Learner receipt bytes drifted")
            new_wire = {**old_wire, "input_sha256": request_sha256}
            updated_result = {**dict(result), "projection_receipt": new_wire}
            connection.execute(
                sa.text(
                    "UPDATE learner_projection_jobs SET result_json=CAST(:result AS jsonb), "
                    "result_sha256=:result_sha256 WHERE tenant_id=:tenant_id "
                    "AND job_id=:job_id"
                ),
                {
                    **dict(row),
                    "result": json.dumps(updated_result, ensure_ascii=False),
                    "result_sha256": _canonical_sha256(updated_result),
                },
            )


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_INT2_APPEND_ONLY_TABLES = (
    "product_skill_draft_revisions",
    "product_skill_patch_proposals",
    "product_skill_patch_evidence",
    "product_skill_patch_decisions",
    "product_draft_revision_assistance",
    "skill_build_provenance",
    "skill_build_terminal_authority",
    "skill_certification_provenance",
    "skill_activation_provenance",
    "skill_run_provenance",
)


def _install_int2_append_only_guards() -> None:
    """Seal INT2 authority rows after migration backfill completes."""

    op.execute(
        "CREATE FUNCTION int2_reject_authority_mutation() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        "RAISE EXCEPTION 'INT2 authority rows are append-only' "
        "USING ERRCODE = 'integrity_constraint_violation'; END; $$"
    )
    for table in _INT2_APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW "
            "EXECUTE FUNCTION int2_reject_authority_mutation()"
        )
    op.execute(
        "CREATE TRIGGER trg_int2_legacy_build_markers_sealed "
        "BEFORE INSERT OR UPDATE OR DELETE ON int2_legacy_build_markers "
        "FOR EACH ROW EXECUTE FUNCTION int2_reject_authority_mutation()"
    )


def _drop_int2_append_only_guards() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_int2_legacy_build_markers_sealed ON int2_legacy_build_markers"
    )
    for table in _INT2_APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
    op.execute("DROP FUNCTION IF EXISTS int2_reject_authority_mutation()")


def _backfill_current_draft_revisions() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT draft.tenant_id,draft.actor_id,draft.session_id,draft.draft_id,"
            "draft.skill_id,draft.revision,draft.draft_sha256,draft.created_at,"
            "draft.updated_at,draft.draft_json,session.session_json "
            "FROM product_skill_drafts AS draft JOIN agent_sessions AS session ON "
            "session.tenant_id=draft.tenant_id AND session.actor_id=draft.actor_id "
            "AND session.session_id=draft.session_id"
        )
    ).mappings()
    insert = sa.text(
        "INSERT INTO product_skill_draft_revisions "
        "(parent_revision_row_id, tenant_id, actor_id, session_id, draft_id, skill_id, revision, draft_sha256, "
        "entrypoint, source_bundle_sha256, source_kind, patch_id, "
        "created_at, draft_json) VALUES "
        "(NULL, :tenant_id, :actor_id, :session_id, :draft_id, :skill_id, :revision, "
        ":draft_sha256, :entrypoint, :source_bundle_sha256, 'STUDENT', NULL, "
        ":created_at, CAST(:draft_json AS jsonb))"
    )
    for row in rows:
        draft = row["draft_json"]
        if not isinstance(draft, Mapping):
            raise RuntimeError("existing Product Draft JSON is not an object")
        source = draft.get("source_bundle")
        if not isinstance(source, Mapping) or not isinstance(source.get("entrypoint"), str):
            raise RuntimeError("existing Product Draft has no canonical source bundle")
        session_wire = row["session_json"]
        if not isinstance(session_wire, Mapping):
            raise RuntimeError("existing Product Draft owner Session is invalid")
        _validate_legacy_draft(dict(row), draft, source, session_wire)
        connection.execute(
            insert,
            {
                **dict(row),
                "entrypoint": source["entrypoint"],
                "source_bundle_sha256": _source_bundle_sha256(source),
                # The one backfilled immutable row represents the current
                # legacy head revision.  For revision > 1 that revision was
                # created at the mutable head's updated_at, not at the
                # original Draft creation time.
                "created_at": row["updated_at"],
                "draft_json": json.dumps(draft, ensure_ascii=False),
            },
        )


def _source_bundle_sha256(value: Mapping[str, object]) -> str:
    if set(value) != {"language", "entrypoint", "files"} or value.get("language") != "CPP20":
        raise RuntimeError("existing Product Draft source bundle shape is invalid")
    entrypoint = value.get("entrypoint")
    if not isinstance(entrypoint, str) or _SOURCE_PATH.fullmatch(entrypoint) is None:
        raise RuntimeError("existing Product Draft entrypoint is invalid")
    files = value.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= 32:
        raise RuntimeError("existing Product Draft source files are not an array")
    projection: list[tuple[str, str]] = []
    paths: set[str] = set()
    total_bytes = 0
    for item in files:
        if not isinstance(item, Mapping) or set(item) != {"path", "content", "content_sha256"}:
            raise RuntimeError("existing Product Draft source file is not an object")
        path = item.get("path")
        content = item.get("content")
        content_sha256 = item.get("content_sha256")
        if (
            not isinstance(path, str)
            or _SOURCE_PATH.fullmatch(path) is None
            or path.lower() in paths
            or not isinstance(content, str)
            or any(0xD800 <= ord(character) <= 0xDFFF for character in content)
            or not isinstance(content_sha256, str)
            or _SHA256.fullmatch(content_sha256) is None
        ):
            raise RuntimeError("existing Product Draft source file identity is invalid")
        encoded_content = content.encode("utf-8")
        total_bytes += len(encoded_content)
        if total_bytes > 1_048_576 or hashlib.sha256(encoded_content).hexdigest() != content_sha256:
            raise RuntimeError("existing Product Draft source content hash is invalid")
        paths.add(path.lower())
        projection.append((path, content_sha256))
    if sum(1 for item in projection if item[0] == entrypoint) != 1:
        raise RuntimeError("existing Product Draft entrypoint is not one source file")
    encoded = json.dumps(
        projection, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_SOURCE_PATH = re.compile(
    r"^(?=.{1,240}$)[A-Za-z0-9_]"
    r"(?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?"
    r"(?:/[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?)*$"
)
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def _validate_legacy_draft(
    row: Mapping[str, object],
    draft: Mapping[str, object],
    source: Mapping[str, object],
    session: Mapping[str, object],
) -> None:
    """Never turn a corrupt mutable legacy head into immutable authority."""

    required = {
        "request_context",
        "session_id",
        "draft_id",
        "skill_id",
        "revision",
        "content_ref",
        "display_name",
        "source_bundle",
        "draft_sha256",
        "created_at",
        "updated_at",
        "last_applied_patch_id",
        "links",
    }
    if set(draft) != required:
        raise RuntimeError("existing Product Draft resource shape is invalid")
    if draft.get("last_applied_patch_id") is not None:
        raise RuntimeError("pre-019 Product Draft claims an unprovable dormant Patch application")
    for key in ("session_id", "draft_id", "skill_id"):
        if key in row and key in draft and draft.get(key) != row.get(key):
            raise RuntimeError(f"existing Product Draft {key} mirror drifted")
    if (
        session.get("session_id") != row.get("session_id")
        or session.get("status") != "ACTIVE"
        or session.get("content") != draft.get("content_ref")
    ):
        raise RuntimeError("existing Product Draft owner/content authority drifted")
    origin = draft.get("request_context")
    actor = origin.get("actor") if isinstance(origin, Mapping) else None
    origin_content = origin.get("content_ref") if isinstance(origin, Mapping) else None
    if (
        not isinstance(actor, Mapping)
        or actor.get("tenant_id") != row.get("tenant_id")
        or actor.get("actor_id") != row.get("actor_id")
        or not isinstance(origin_content, Mapping)
        or origin_content != draft.get("content_ref")
    ):
        raise RuntimeError("existing Product Draft request authority drifted")
    revision = draft.get("revision")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or revision != row.get("revision")
        or draft.get("draft_sha256") != row.get("draft_sha256")
        or not isinstance(row.get("draft_sha256"), str)
        or _SHA256.fullmatch(str(row.get("draft_sha256"))) is None
    ):
        raise RuntimeError("existing Product Draft revision/hash mirror drifted")
    try:
        created_at = _iso_datetime(row.get("created_at"))
        updated_at = _iso_datetime(row.get("updated_at"))
    except RuntimeError as error:
        raise RuntimeError("existing Product Draft timestamps are invalid") from error
    if (
        draft.get("created_at") != created_at
        or draft.get("updated_at") != updated_at
        or cast_datetime(row.get("updated_at")) < cast_datetime(row.get("created_at"))
    ):
        raise RuntimeError("existing Product Draft timestamp mirrors drifted")
    source_sha256 = _source_bundle_sha256(source)
    del source_sha256
    projection = {
        key: draft.get(key)
        for key in (
            "session_id",
            "draft_id",
            "skill_id",
            "content_ref",
            "display_name",
            "source_bundle",
        )
    }
    if any(value is None for value in projection.values()):
        raise RuntimeError("existing Product Draft canonical projection is incomplete")
    encoded = json.dumps(
        _canonical_value(projection),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != row.get("draft_sha256"):
        raise RuntimeError("existing Product Draft full authority hash is invalid")


def _canonical_value(value: object) -> object:
    """Self-contained YAYA_CANONICAL_JSON_V1 for immutable migrations."""

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise RuntimeError("existing Product Draft contains an invalid Unicode scalar")
        return value
    if isinstance(value, int):
        if not -(2**53 - 1) <= value <= 2**53 - 1:
            raise RuntimeError("existing Product Draft contains an unsafe integer")
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or value != 0:
            raise RuntimeError("existing Product Draft contains a noncanonical number")
        return 0
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise RuntimeError("existing Product Draft has a non-text object key")
            normalized[key] = _canonical_value(item)
        return normalized
    raise RuntimeError("existing Product Draft has an unsupported JSON value")
