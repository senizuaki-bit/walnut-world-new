"""Create the backend-owned INT1 authority, workflow, registry, and learner foundation."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "014_int1_authority_foundation"
down_revision = "013_product_workspaces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_product_content_authority",
        "product_content_units",
        ["tenant_id", "unit_id", "version", "content_hash"],
    )
    op.create_unique_constraint(
        "uq_world_snapshot_authority",
        "world_snapshots",
        ["tenant_id", "world_id", "actor_id", "content_hash"],
    )

    op.create_table(
        "learner_profiles",
        sa.Column("tenant_id", sa.String(length=96), primary_key=True),
        sa.Column("learner_id", sa.String(length=128), primary_key=True),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("profile_sha256", sa.String(length=64), nullable=False),
        sa.Column("profile_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "learner_id",
            "actor_id",
            "content_hash",
            name="uq_learner_profile_authority",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[a-f0-9]{64}$'", name="ck_learner_profile_content_hash"
        ),
        sa.CheckConstraint(
            "profile_sha256 ~ '^[a-f0-9]{64}$'", name="ck_learner_profile_sha256"
        ),
    )
    op.create_table(
        "agent_profiles",
        sa.Column("tenant_id", sa.String(length=96), primary_key=True),
        sa.Column("agent_profile_id", sa.String(length=128), primary_key=True),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("profile_sha256", sa.String(length=64), nullable=False),
        sa.Column("profile_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "agent_profile_id",
            "actor_id",
            "content_hash",
            name="uq_agent_profile_authority",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[a-f0-9]{64}$'", name="ck_agent_profile_content_hash"
        ),
        sa.CheckConstraint(
            "profile_sha256 ~ '^[a-f0-9]{64}$'", name="ck_agent_profile_sha256"
        ),
    )
    op.create_table(
        "build_policies",
        sa.Column("tenant_id", sa.String(length=96), primary_key=True),
        sa.Column("build_policy_id", sa.String(length=128), primary_key=True),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("compiler_profile", sa.String(length=128), nullable=False),
        sa.Column("compiler_version", sa.String(length=128), nullable=False),
        sa.Column("sandbox_image_digest", sa.String(length=512), nullable=False),
        sa.Column("test_suite_version", sa.String(length=128), nullable=False),
        sa.Column("allowed_capabilities", postgresql.JSONB(), nullable=False),
        sa.Column("max_source_files", sa.Integer(), nullable=False),
        sa.Column("max_source_bytes", sa.BigInteger(), nullable=False),
        sa.Column("policy_json", postgresql.JSONB(), nullable=False),
        sa.Column("policy_sha256", sa.String(length=64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "build_policy_id",
            "actor_id",
            "content_hash",
            name="uq_build_policy_authority",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[a-f0-9]{64}$'", name="ck_build_policy_content_hash"
        ),
        sa.CheckConstraint(
            "policy_sha256 ~ '^[a-f0-9]{64}$'", name="ck_build_policy_sha256"
        ),
        sa.CheckConstraint(
            "sandbox_image_digest ~ '^sha256:[a-f0-9]{64}$'",
            name="ck_build_policy_sandbox_digest",
        ),
        sa.CheckConstraint("max_source_files > 0", name="ck_build_policy_max_source_files"),
        sa.CheckConstraint("max_source_bytes > 0", name="ck_build_policy_max_source_bytes"),
    )
    op.create_index(
        "uq_build_policy_active_scope",
        "build_policies",
        ["tenant_id", "actor_id", "content_hash"],
        unique=True,
        postgresql_where=sa.text("active IS TRUE"),
    )
    op.create_table(
        "launch_authorities",
        sa.Column("tenant_id", sa.String(length=96), primary_key=True),
        sa.Column("authority_id", sa.String(length=128), primary_key=True),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("content_unit_id", sa.String(length=128), nullable=False),
        sa.Column("content_version", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("world_id", sa.String(length=128), nullable=False),
        sa.Column("learner_id", sa.String(length=128), nullable=False),
        sa.Column("agent_profile_id", sa.String(length=128), nullable=False),
        sa.Column("build_policy_id", sa.String(length=128), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("teaching_spec_version", sa.String(length=128), nullable=False),
        sa.Column("authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "authority_id",
            "actor_id",
            "content_hash",
            "world_id",
            "learner_id",
            "agent_profile_id",
            name="uq_launch_authority_closure",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "authority_id",
            "actor_id",
            "content_hash",
            "world_id",
            "agent_profile_id",
            name="uq_launch_authority_registry_scope",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "content_unit_id", "content_version", "content_hash"],
            [
                "product_content_units.tenant_id",
                "product_content_units.unit_id",
                "product_content_units.version",
                "product_content_units.content_hash",
            ],
            name="fk_launch_authority_content",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "world_id", "actor_id", "content_hash"],
            [
                "world_snapshots.tenant_id",
                "world_snapshots.world_id",
                "world_snapshots.actor_id",
                "world_snapshots.content_hash",
            ],
            name="fk_launch_authority_world",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "learner_id", "actor_id", "content_hash"],
            [
                "learner_profiles.tenant_id",
                "learner_profiles.learner_id",
                "learner_profiles.actor_id",
                "learner_profiles.content_hash",
            ],
            name="fk_launch_authority_learner",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_profile_id", "actor_id", "content_hash"],
            [
                "agent_profiles.tenant_id",
                "agent_profiles.agent_profile_id",
                "agent_profiles.actor_id",
                "agent_profiles.content_hash",
            ],
            name="fk_launch_authority_agent_profile",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "build_policy_id", "actor_id", "content_hash"],
            [
                "build_policies.tenant_id",
                "build_policies.build_policy_id",
                "build_policies.actor_id",
                "build_policies.content_hash",
            ],
            name="fk_launch_authority_build_policy",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[a-f0-9]{64}$'", name="ck_launch_authority_content_hash"
        ),
        sa.CheckConstraint(
            "authority_sha256 ~ '^[a-f0-9]{64}$'", name="ck_launch_authority_sha256"
        ),
        sa.CheckConstraint("channel IN ('GAME')", name="ck_launch_authority_channel"),
    )
    op.create_index(
        "uq_launch_authority_active_actor",
        "launch_authorities",
        ["tenant_id", "actor_id"],
        unique=True,
        postgresql_where=sa.text("active IS TRUE"),
    )
    op.create_index(
        "ix_launch_authority_resolution",
        "launch_authorities",
        ["tenant_id", "actor_id", "content_hash", "world_id"],
    )

    op.create_table(
        "workflow_jobs",
        sa.Column("job_id", sa.String(length=128), primary_key=True),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("command_id", sa.String(length=128), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("subject_type", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("phase", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("lease_owner", sa.String(length=128)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("job_json", postgresql.JSONB(), nullable=False),
        sa.Column("last_error_json", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["command_id"], ["commands.command_id"], name="fk_workflow_job_command"
        ),
        sa.UniqueConstraint("tenant_id", "job_id", name="uq_workflow_job_tenant"),
        sa.UniqueConstraint("tenant_id", "command_id", name="uq_workflow_job_command"),
        sa.CheckConstraint(
            "status IN ('ACCEPTED','READY','CLAIMED','RUNNING','RETRY_WAIT',"
            "'SUCCEEDED','FAILED','CANCELLED','DEAD_LETTER')",
            name="ck_workflow_job_status",
        ),
        sa.CheckConstraint("fencing_token >= 0", name="ck_workflow_job_fencing_token"),
        sa.CheckConstraint("attempt >= 0", name="ck_workflow_job_attempt"),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_workflow_job_lease_pair",
        ),
    )
    op.create_index(
        "ix_workflow_jobs_ready",
        "workflow_jobs",
        ["status", "next_attempt_at", "lease_expires_at"],
    )
    op.create_table(
        "job_step_receipts",
        sa.Column("receipt_id", sa.String(length=128), primary_key=True),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("step_name", sa.String(length=64), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("output_sha256", sa.String(length=64), nullable=False),
        sa.Column("receipt_json", postgresql.JSONB(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["workflow_jobs.tenant_id", "workflow_jobs.job_id"],
            name="fk_job_step_workflow",
        ),
        sa.UniqueConstraint("tenant_id", "job_id", "step_name", name="uq_job_step_once"),
        sa.CheckConstraint("fencing_token > 0", name="ck_job_step_fencing_token"),
        sa.CheckConstraint(
            "input_sha256 ~ '^[a-f0-9]{64}$'", name="ck_job_step_input_sha256"
        ),
        sa.CheckConstraint(
            "output_sha256 ~ '^[a-f0-9]{64}$'", name="ck_job_step_output_sha256"
        ),
    )

    op.create_table(
        "skill_artifacts",
        sa.Column("tenant_id", sa.String(length=96), primary_key=True),
        sa.Column("artifact_sha256", sa.String(length=64), primary_key=True),
        sa.Column("build_id", sa.String(length=128), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("skill_id", sa.String(length=128), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_uri", sa.String(length=1024), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["build_id"], ["skill_builds.build_id"], name="fk_skill_artifact_build"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "artifact_sha256",
            "build_id",
            "actor_id",
            "content_hash",
            name="uq_skill_artifact_closure",
        ),
        sa.CheckConstraint(
            "artifact_sha256 ~ '^[a-f0-9]{64}$'", name="ck_skill_artifact_sha256"
        ),
        sa.CheckConstraint(
            "source_sha256 ~ '^[a-f0-9]{64}$'", name="ck_skill_artifact_source_sha256"
        ),
    )
    op.create_table(
        "skill_certifications",
        sa.Column("certification_id", sa.String(length=128), primary_key=True),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("build_id", sa.String(length=128), nullable=False),
        sa.Column("skill_id", sa.String(length=128), nullable=False),
        sa.Column("skill_version_id", sa.String(length=128), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("certification_sha256", sa.String(length=64), nullable=False),
        sa.Column("certification_json", postgresql.JSONB(), nullable=False),
        sa.Column("certified_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "artifact_sha256", "build_id", "actor_id", "content_hash"],
            [
                "skill_artifacts.tenant_id",
                "skill_artifacts.artifact_sha256",
                "skill_artifacts.build_id",
                "skill_artifacts.actor_id",
                "skill_artifacts.content_hash",
            ],
            name="fk_skill_certification_artifact",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "certification_id",
            "skill_id",
            "skill_version_id",
            "artifact_sha256",
            "actor_id",
            "content_hash",
            name="uq_skill_certification_closure",
        ),
        sa.CheckConstraint(
            "certification_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_skill_certification_sha256",
        ),
    )
    op.create_table(
        "skill_certification_revocations",
        sa.Column("revocation_id", sa.String(length=128), primary_key=True),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("certification_id", sa.String(length=128), nullable=False),
        sa.Column("revocation_sha256", sa.String(length=64), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("revocation_json", postgresql.JSONB(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["certification_id"],
            ["skill_certifications.certification_id"],
            name="fk_revocation_certification",
        ),
        sa.UniqueConstraint(
            "tenant_id", "certification_id", name="uq_certification_revocation"
        ),
        sa.CheckConstraint(
            "revocation_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_certification_revocation_sha256",
        ),
    )

    op.create_table(
        "current_session_bindings",
        sa.Column("binding_id", sa.String(length=128), primary_key=True),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("authority_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("world_id", sa.String(length=128), nullable=False),
        sa.Column("learner_id", sa.String(length=128), nullable=False),
        sa.Column("agent_profile_id", sa.String(length=128), nullable=False),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["agent_sessions.session_id"], name="fk_current_session_resource"
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "authority_id",
                "actor_id",
                "content_hash",
                "world_id",
                "learner_id",
                "agent_profile_id",
            ],
            [
                "launch_authorities.tenant_id",
                "launch_authorities.authority_id",
                "launch_authorities.actor_id",
                "launch_authorities.content_hash",
                "launch_authorities.world_id",
                "launch_authorities.learner_id",
                "launch_authorities.agent_profile_id",
            ],
            name="fk_current_session_launch_authority",
        ),
        sa.UniqueConstraint(
            "tenant_id", "authority_id", name="uq_current_session_authority"
        ),
        sa.UniqueConstraint("tenant_id", "session_id", name="uq_current_session_identity"),
    )

    op.create_table(
        "registry_heads",
        sa.Column("tenant_id", sa.String(length=96), primary_key=True),
        sa.Column("actor_id", sa.String(length=128), primary_key=True),
        sa.Column("content_hash", sa.String(length=64), primary_key=True),
        sa.Column("world_id", sa.String(length=128), primary_key=True),
        sa.Column("agent_profile_id", sa.String(length=128), primary_key=True),
        sa.Column("authority_id", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "authority_id",
                "actor_id",
                "content_hash",
                "world_id",
                "agent_profile_id",
            ],
            [
                "launch_authorities.tenant_id",
                "launch_authorities.authority_id",
                "launch_authorities.actor_id",
                "launch_authorities.content_hash",
                "launch_authorities.world_id",
                "launch_authorities.agent_profile_id",
            ],
            name="fk_registry_head_launch_authority",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_registry_head_revision"),
        sa.CheckConstraint(
            "content_hash ~ '^[a-f0-9]{64}$'", name="ck_registry_head_content_hash"
        ),
    )
    op.create_table(
        "registry_entries",
        sa.Column("tenant_id", sa.String(length=96), primary_key=True),
        sa.Column("actor_id", sa.String(length=128), primary_key=True),
        sa.Column("content_hash", sa.String(length=64), primary_key=True),
        sa.Column("world_id", sa.String(length=128), primary_key=True),
        sa.Column("agent_profile_id", sa.String(length=128), primary_key=True),
        sa.Column("revision", sa.BigInteger(), primary_key=True),
        sa.Column("skill_id", sa.String(length=128), nullable=False),
        sa.Column("skill_version_id", sa.String(length=128), nullable=False),
        sa.Column("certification_id", sa.String(length=128), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("previous_revision", sa.BigInteger(), nullable=False),
        sa.Column("entry_sha256", sa.String(length=64), nullable=False),
        sa.Column("entry_json", postgresql.JSONB(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "actor_id",
                "content_hash",
                "world_id",
                "agent_profile_id",
            ],
            [
                "registry_heads.tenant_id",
                "registry_heads.actor_id",
                "registry_heads.content_hash",
                "registry_heads.world_id",
                "registry_heads.agent_profile_id",
            ],
            name="fk_registry_entry_head",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "certification_id",
                "skill_id",
                "skill_version_id",
                "artifact_sha256",
                "actor_id",
                "content_hash",
            ],
            [
                "skill_certifications.tenant_id",
                "skill_certifications.certification_id",
                "skill_certifications.skill_id",
                "skill_certifications.skill_version_id",
                "skill_certifications.artifact_sha256",
                "skill_certifications.actor_id",
                "skill_certifications.content_hash",
            ],
            name="fk_registry_entry_certification",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "actor_id",
            "content_hash",
            "world_id",
            "agent_profile_id",
            "revision",
            "skill_id",
            "skill_version_id",
            "certification_id",
            "artifact_sha256",
            name="uq_registry_entry_activation_closure",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_registry_entry_revision"),
        sa.CheckConstraint(
            "previous_revision >= 0", name="ck_registry_entry_previous_revision"
        ),
        sa.CheckConstraint("revision = previous_revision + 1", name="ck_registry_entry_chain"),
        sa.CheckConstraint(
            "entry_sha256 ~ '^[a-f0-9]{64}$'", name="ck_registry_entry_sha256"
        ),
    )
    op.create_index(
        "ix_registry_entries_current",
        "registry_entries",
        [
            "tenant_id",
            "actor_id",
            "content_hash",
            "world_id",
            "agent_profile_id",
            "revision",
        ],
    )
    op.create_table(
        "skill_activations",
        sa.Column("activation_id", sa.String(length=128), primary_key=True),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("world_id", sa.String(length=128), nullable=False),
        sa.Column("agent_profile_id", sa.String(length=128), nullable=False),
        sa.Column("skill_id", sa.String(length=128), nullable=False),
        sa.Column("skill_version_id", sa.String(length=128), nullable=False),
        sa.Column("certification_id", sa.String(length=128), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("previous_registry_revision", sa.BigInteger(), nullable=False),
        sa.Column("registry_revision", sa.BigInteger(), nullable=False),
        sa.Column("activation_sha256", sa.String(length=64), nullable=False),
        sa.Column("activation_json", postgresql.JSONB(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "actor_id",
                "content_hash",
                "world_id",
                "agent_profile_id",
                "registry_revision",
                "skill_id",
                "skill_version_id",
                "certification_id",
                "artifact_sha256",
            ],
            [
                "registry_entries.tenant_id",
                "registry_entries.actor_id",
                "registry_entries.content_hash",
                "registry_entries.world_id",
                "registry_entries.agent_profile_id",
                "registry_entries.revision",
                "registry_entries.skill_id",
                "registry_entries.skill_version_id",
                "registry_entries.certification_id",
                "registry_entries.artifact_sha256",
            ],
            name="fk_skill_activation_registry_entry",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "actor_id",
            "content_hash",
            "world_id",
            "agent_profile_id",
            "skill_id",
            "registry_revision",
            name="uq_skill_activation_registry_revision",
        ),
        sa.CheckConstraint(
            "previous_registry_revision >= 0", name="ck_activation_previous_revision"
        ),
        sa.CheckConstraint(
            "registry_revision >= 1", name="ck_activation_registry_revision"
        ),
        sa.CheckConstraint(
            "registry_revision = previous_registry_revision + 1", name="ck_activation_chain"
        ),
        sa.CheckConstraint(
            "activation_sha256 ~ '^[a-f0-9]{64}$'", name="ck_activation_sha256"
        ),
    )

    op.create_table(
        "learner_projection_jobs",
        sa.Column("job_id", sa.String(length=128), primary_key=True),
        sa.Column("tenant_id", sa.String(length=96), nullable=False),
        sa.Column("learner_id", sa.String(length=128), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("source_event_id", sa.String(length=132), nullable=False),
        sa.Column("expected_revision", sa.BigInteger(), nullable=False),
        sa.Column("through_sequence", sa.BigInteger(), nullable=False),
        sa.Column("projection_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["workflow_jobs.tenant_id", "workflow_jobs.job_id"],
            name="fk_learner_projection_workflow",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "learner_id", "actor_id", "content_hash"],
            [
                "learner_profiles.tenant_id",
                "learner_profiles.learner_id",
                "learner_profiles.actor_id",
                "learner_profiles.content_hash",
            ],
            name="fk_learner_projection_profile",
        ),
        sa.ForeignKeyConstraint(
            ["source_event_id"],
            ["domain_events.event_id"],
            name="fk_learner_projection_event",
        ),
        sa.UniqueConstraint(
            "tenant_id", "job_id", name="uq_learner_projection_job_tenant"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "learner_id",
            "source_event_id",
            name="uq_learner_projection_source",
        ),
        sa.CheckConstraint(
            "expected_revision >= 0", name="ck_learner_projection_revision"
        ),
        sa.CheckConstraint(
            "through_sequence >= 0", name="ck_learner_projection_sequence"
        ),
    )
    op.create_index(
        "ix_learner_projection_jobs_learner",
        "learner_projection_jobs",
        ["tenant_id", "learner_id", "through_sequence"],
    )


def downgrade() -> None:
    op.drop_index("ix_learner_projection_jobs_learner", table_name="learner_projection_jobs")
    op.drop_table("learner_projection_jobs")
    op.drop_table("skill_activations")
    op.drop_index("ix_registry_entries_current", table_name="registry_entries")
    op.drop_table("registry_entries")
    op.drop_table("registry_heads")
    op.drop_table("current_session_bindings")
    op.drop_table("skill_certification_revocations")
    op.drop_table("skill_certifications")
    op.drop_table("skill_artifacts")
    op.drop_table("job_step_receipts")
    op.drop_index("ix_workflow_jobs_ready", table_name="workflow_jobs")
    op.drop_table("workflow_jobs")
    op.drop_index("ix_launch_authority_resolution", table_name="launch_authorities")
    op.drop_index("uq_launch_authority_active_actor", table_name="launch_authorities")
    op.drop_table("launch_authorities")
    op.drop_index("uq_build_policy_active_scope", table_name="build_policies")
    op.drop_table("build_policies")
    op.drop_table("agent_profiles")
    op.drop_table("learner_profiles")
    op.drop_constraint(
        "uq_world_snapshot_authority", "world_snapshots", type_="unique"
    )
    op.drop_constraint(
        "uq_product_content_authority", "product_content_units", type_="unique"
    )
