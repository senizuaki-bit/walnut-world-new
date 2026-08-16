"""Database shape gates for the INT2 Skill Patch authority chain."""

from __future__ import annotations

import warnings

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.exc import SAWarning

from walnut_backend.adapters.postgres.models import (
    Base,
    ProductDraftRevisionRow,
    ProductSkillPatchDecisionRow,
    ProductSkillPatchEvidenceRow,
    ProductSkillPatchProposalRow,
    SkillBuildProvenanceRow,
    SkillBuildRow,
)


def test_int2_migration_rejects_corrupt_legacy_draft_authority() -> None:
    import importlib.util
    from pathlib import Path

    migration_path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "019_int2_skill_patch_authority.py"
    )
    spec = importlib.util.spec_from_file_location("int2_migration_019", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    valid = {
        "language": "CPP20",
        "entrypoint": "src/main.cpp",
        "files": [
            {
                "path": "src/main.cpp",
                "content": "int main() { return 0; }\n",
                "content_sha256": __import__("hashlib").sha256(
                    b"int main() { return 0; }\n"
                ).hexdigest(),
            }
        ],
    }
    assert len(module._source_bundle_sha256(valid)) == 64
    for corrupt in (
        {**valid, "entrypoint": "src/missing.cpp"},
        {**valid, "entrypoint": ".hidden"},
        {
            **valid,
            "files": [
                *valid["files"],
                {
                    **valid["files"][0],
                    "path": "SRC/MAIN.CPP",
                },
            ],
        },
        {
            **valid,
            "files": [{**valid["files"][0], "content_sha256": "0" * 64}],
        },
    ):
        try:
            module._source_bundle_sha256(corrupt)
        except RuntimeError:
            pass
        else:
            raise AssertionError("corrupt legacy Draft source was accepted")


def test_int2_patch_authority_tables_and_build_foreign_keys_are_declared() -> None:
    assert ProductDraftRevisionRow.__tablename__ == "product_skill_draft_revisions"
    assert ProductSkillPatchProposalRow.__tablename__ == "product_skill_patch_proposals"
    assert ProductSkillPatchEvidenceRow.__tablename__ == "product_skill_patch_evidence"
    assert ProductSkillPatchDecisionRow.__tablename__ == "product_skill_patch_decisions"

    revisions = Base.metadata.tables[ProductDraftRevisionRow.__tablename__]
    proposals = Base.metadata.tables[ProductSkillPatchProposalRow.__tablename__]
    decisions = Base.metadata.tables[ProductSkillPatchDecisionRow.__tablename__]
    builds = Base.metadata.tables[SkillBuildRow.__tablename__]
    provenance = Base.metadata.tables[SkillBuildProvenanceRow.__tablename__]

    assert _constraint_names(revisions, UniqueConstraint) >= {
        "uq_product_draft_revision_identity",
        "uq_product_draft_revision_authority",
        "uq_product_draft_revision_patch_pair",
    }
    assert _constraint_names(revisions, CheckConstraint) >= {
        "ck_product_draft_revision_hashes",
        "ck_product_draft_revision_source",
    }
    assert _constraint_names(proposals, UniqueConstraint) >= {
        "uq_product_skill_patch_interaction",
        "uq_product_skill_patch_authority",
        "uq_product_skill_patch_base_pair",
    }
    assert _constraint_names(decisions, UniqueConstraint) >= {
        "uq_product_skill_patch_terminal_decision",
        "uq_product_skill_patch_accepted_draft",
        "uq_product_skill_patch_decision_accepted_triple",
    }
    assert _constraint_names(decisions, ForeignKeyConstraint) >= {
        "fk_product_skill_patch_decision_proposal",
        "fk_product_skill_patch_decision_proposal_base",
        "fk_product_skill_patch_decision_base_draft",
        "fk_product_skill_patch_decision_accepted_draft",
    }
    assert not _constraint_names(builds, ForeignKeyConstraint)
    assert _constraint_names(provenance, ForeignKeyConstraint) >= {
        "fk_skill_build_provenance_build",
        "fk_skill_build_provenance_draft",
        "fk_skill_build_provenance_patch",
        "fk_skill_build_provenance_accepted_decision",
    }
    assert _constraint_names(provenance, CheckConstraint) >= {
        "ck_skill_build_provenance_hashes",
        "ck_skill_build_provenance_assistance",
    }


def test_int2_patch_provenance_metadata_is_a_directional_dag() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", SAWarning)
        sorted_tables = tuple(Base.metadata.sorted_tables)

    assert len(sorted_tables) == len(Base.metadata.tables)
    assert not [item for item in caught if issubclass(item.category, SAWarning)]


def _constraint_names(table: object, kind: type[object]) -> set[str]:
    constraints = getattr(table, "constraints")
    return {
        str(constraint.name)
        for constraint in constraints
        if isinstance(constraint, kind)
    }
