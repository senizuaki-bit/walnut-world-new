"""Post-cutover Runs on sealed v0.4 Builds keep an exact Activation edge."""

from sqlalchemy import CheckConstraint

from walnut_backend.adapters.postgres.models import SkillRunProvenanceRow
from walnut_backend.adapters.postgres.skill_invocation import _run_provenance_kind


def test_legacy_build_new_run_uses_exact_activation_provenance_kind() -> None:
    assert _run_provenance_kind("LEGACY_V04") == "LEGACY_V04_ACTIVE"
    assert _run_provenance_kind("IMMUTABLE_DRAFT") == "IMMUTABLE_DRAFT"

    checks = " ".join(
        str(constraint.sqltext)
        for constraint in SkillRunProvenanceRow.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    )
    assert "provenance_kind = 'LEGACY_V04_ACTIVE'" in checks
    assert "activation_id IS NOT NULL" in checks
