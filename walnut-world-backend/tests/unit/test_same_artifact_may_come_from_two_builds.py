"""Writing the same correct code twice must not be a database conflict.

Build artifacts are content-addressed: identical source produces identical
bytes. A learner reaches the same sha whenever they solve a task the same way
again, revert an edit, or rebuild after changing nothing that matters -- and two
learners who write the same solution reach it too.

skill_artifacts used to make that impossible. Its primary key was
(tenant_id, artifact_sha256) while uq_skill_artifact_closure spanned that plus
build_id, actor_id and content_hash: one constraint forbidding what the other
explicitly allowed. The Build worker writes the artifact row inside the
transaction that certifies the Build, so the second Build to produce those bytes
raised UniqueViolationError, rolled back, retried five times and dead-lettered.
The Build row stayed at COMPILING and the learner could not run at all.

It was observed exactly this way: a child solved the level, and every later
attempt at the same correct solution was refused by the database *because* it
was correct in the same way. That is the worst possible failure to have -- it
punishes only the learners who succeed.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT / "src"))

from walnut_backend.adapters.postgres.models import SkillArtifactRow  # noqa: E402


class ArtifactIdentityIncludesTheBuildTests(unittest.TestCase):
    def test_build_id_is_part_of_the_primary_key(self) -> None:
        key = {column.name for column in SkillArtifactRow.__table__.primary_key}
        self.assertEqual(key, {"tenant_id", "artifact_sha256", "build_id"})

    def test_the_primary_key_does_not_contradict_the_closure_constraint(self) -> None:
        # The closure constraint always allowed one artifact to arrive from more
        # than one Build. A primary key narrower than it would forbid exactly
        # that, which is how the lock-out happened.
        closure = next(
            constraint
            for constraint in SkillArtifactRow.__table__.constraints
            if getattr(constraint, "name", None) == "uq_skill_artifact_closure"
        )
        closure_columns = {column.name for column in closure.columns}
        key_columns = {column.name for column in SkillArtifactRow.__table__.primary_key}
        self.assertTrue(
            key_columns.issubset(closure_columns),
            "the primary key must not forbid a combination the closure allows",
        )
        self.assertIn("build_id", closure_columns)


class ArtifactRowStillDescribesOneBuildTests(unittest.TestCase):
    """Widening identity must not blur whose Build an artifact belongs to."""

    def test_the_build_owning_fields_are_still_required(self) -> None:
        columns = SkillArtifactRow.__table__.columns
        for name in ("build_id", "actor_id", "content_hash", "source_sha256"):
            self.assertFalse(columns[name].nullable, name)


if __name__ == "__main__":
    unittest.main()
