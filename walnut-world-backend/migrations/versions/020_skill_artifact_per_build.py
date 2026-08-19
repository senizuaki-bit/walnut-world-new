"""Let two Builds produce the same artifact bytes.

Revision ID: 020_skill_artifact_per_build
Revises: 019_int2_skill_patch_authority

skill_artifacts kept two contradictory promises. Its primary key was
(tenant_id, artifact_sha256), which says one row per artifact content, while
uq_skill_artifact_closure spans (tenant_id, artifact_sha256, build_id, actor_id,
content_hash), which says the same artifact may legitimately arrive from more
than one Build. Only the second is true of the domain: the artifact is
content-addressed, so identical source produces identical bytes, and a learner
who writes the same correct code twice -- or reverts an edit, or simply solves
the task the same way again -- rebuilds to a sha they have already produced.

The primary key won, and the consequence was severe. The Build worker inserts
the artifact row inside the transaction that certifies the Build, so the second
Build raised UniqueViolationError, rolled back, retried five times, and
dead-lettered. The Build row stayed at COMPILING forever and the learner could
not run their code at all. Observed live: a child solved the level, and every
later attempt at the same correct solution was rejected by the database because
the solution was correct in exactly the same way.

This widens the primary key to match the closure constraint that was already
there. Each Build keeps its own row -- build_id, actor_id, content_hash,
source_sha256 and metadata are all properties of the Build that produced the
artifact, not of the bytes -- and the read path, which looks artifacts up by
build_id, keeps finding exactly one.
"""

from __future__ import annotations

from alembic import op

revision = "020_skill_artifact_per_build"
down_revision = "019_int2_skill_patch_authority"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("skill_artifacts_pkey", "skill_artifacts", type_="primary")
    op.create_primary_key(
        "skill_artifacts_pkey",
        "skill_artifacts",
        ["tenant_id", "artifact_sha256", "build_id"],
    )


def downgrade() -> None:
    # Narrowing again can only succeed while no artifact is shared between
    # Builds, which is the state this migration exists to stop requiring.
    op.drop_constraint("skill_artifacts_pkey", "skill_artifacts", type_="primary")
    op.create_primary_key(
        "skill_artifacts_pkey",
        "skill_artifacts",
        ["tenant_id", "artifact_sha256"],
    )
