"""A rejected Build's Evidence is held to the same standard as a certification.

Rejections started recording Evidence so that a learner stuck on the compiler
becomes visible to the teaching policy -- before that, eight failed compiles
produced zero Evidence and the policy saw a child who had never failed.

Reading one back must therefore be strict: the row has to describe *this* Build,
under *this* Command, carrying the diagnostics the Build row already settled on.
Otherwise the Evidence a hint cites could disagree with the failure it came from.

(The companion rule -- that whether Evidence must exist at all is decided by the
receipt, so rejections settled before this change stay readable -- is exercised
end to end by tests/integration/test_int2_provenance_migration.py.)
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT / "src"))

from yaya_agent_contracts import canonical_json_sha256  # noqa: E402

from walnut_backend.adapters.postgres.skill_builds import (  # noqa: E402
    _rejection_evidence_matches,
)

SETTLED_AT = datetime(2026, 8, 19, 3, 21, 44, tzinfo=UTC)
EVIDENCE_ID = "evidence_buildreject_0001"
BUILD_ID = "build_0001"
COMMAND_ID = "cmd_0001"
DIAGNOSTICS = ["CPP_COMPILE_ERROR"]


@dataclass
class _Build:
    build_json: dict[str, Any]
    build_id: str = BUILD_ID
    skill_id: str = "skill_0001"
    tenant_id: str = "tenant_yaya"
    actor_id: str = "student_0001"
    command_id: str = COMMAND_ID
    updated_at: datetime = SETTLED_AT


@dataclass
class _ContentRef:
    content_hash: str = "a" * 64


@dataclass
class _RequestContext:
    content_ref: _ContentRef = field(default_factory=_ContentRef)


@dataclass
class _Command:
    request_context: _RequestContext = field(default_factory=_RequestContext)


@dataclass
class _Authority:
    world_id: str = "world_0001"
    learner_id: str = "learner_0001"


@dataclass
class _Evidence:
    evidence_json: dict[str, Any]
    evidence_id: str = EVIDENCE_ID
    tenant_id: str = "tenant_yaya"
    actor_id: str = "student_0001"
    content_hash: str = "a" * 64
    command_id: str = COMMAND_ID
    recorded_at: datetime = SETTLED_AT


def _build_row(*, diagnostics: list[str] | None = None, stage: str = "COMPILE") -> _Build:
    return _Build(
        build_json={
            "failure": {
                "stage": stage,
                "details": {
                    "diagnostic_codes": DIAGNOSTICS if diagnostics is None else diagnostics
                },
            },
            "phases": [
                {"name": "VALIDATE_SOURCE", "status": "PASSED"},
                {"name": stage, "status": "FAILED"},
            ],
        }
    )


def _payload(*, diagnostics: list[str] | None = None, stage: str = "COMPILE") -> dict[str, Any]:
    return {
        "evidence_kind": "BUILD_REJECTION",
        "build_id": BUILD_ID,
        "skill_id": "skill_0001",
        "test_suite_version": "suite-v1",
        "outcome": "REJECTED",
        "failure_stage": stage,
        "failure_code": "CPP_COMPILE_FAILED",
        "diagnostic_codes": DIAGNOSTICS if diagnostics is None else diagnostics,
    }


def _evidence_row(payload: dict[str, Any] | None = None, **overrides: Any) -> _Evidence:
    body = _payload() if payload is None else payload
    digest = canonical_json_sha256(body)
    data = {
        "payload": body,
        "source": {
            "source_type": "SKILL_BUILD",
            "source_id": BUILD_ID,
            "command_id": COMMAND_ID,
            "world_id": "world_0001",
        },
        "subject": {"learner_id": "learner_0001"},
        "evidence_ref": {
            "evidence_id": EVIDENCE_ID,
            "evidence_type": "TEST_REPORT",
            "created_at": "2026-08-19T03:21:44Z",
            "sha256": digest,
            "uri": f"/v1/evidence/{EVIDENCE_ID}",
        },
    }
    for key, value in overrides.items():
        data[key] = value
    return _Evidence(evidence_json=data)


def _matches(build: _Build, evidence: _Evidence, evidence_id: object = EVIDENCE_ID) -> bool:
    return _rejection_evidence_matches(
        build,  # type: ignore[arg-type]
        _Command(),  # type: ignore[arg-type]
        _Authority(),  # type: ignore[arg-type]
        evidence,  # type: ignore[arg-type]
        evidence_id,
    )


class WellFormedRejectionEvidenceTests(unittest.TestCase):
    def test_evidence_written_by_the_build_worker_is_accepted(self) -> None:
        self.assertTrue(_matches(_build_row(), _evidence_row()))


class RejectionEvidenceMustDescribeThisBuildTests(unittest.TestCase):
    def test_a_different_evidence_id_is_rejected(self) -> None:
        # The receipt names the Evidence; a row under a different id is not it.
        self.assertFalse(_matches(_build_row(), _evidence_row(), "evidence_other_0002"))

    def test_evidence_under_another_command_is_rejected(self) -> None:
        evidence = _evidence_row()
        evidence.command_id = "cmd_other_0002"
        self.assertFalse(_matches(_build_row(), evidence))

    def test_evidence_recorded_at_another_time_is_rejected(self) -> None:
        evidence = _evidence_row()
        evidence.recorded_at = datetime(2026, 8, 19, 4, 0, 0, tzinfo=UTC)
        self.assertFalse(_matches(_build_row(), evidence))

    def test_evidence_for_another_learner_is_rejected(self) -> None:
        self.assertFalse(
            _matches(_build_row(), _evidence_row(subject={"learner_id": "learner_other"}))
        )


class RejectionEvidenceMustAgreeWithTheBuildTests(unittest.TestCase):
    def test_diagnostics_that_disagree_with_the_build_row_are_rejected(self) -> None:
        # This is the point of the check: a hint cites this Evidence, so it must
        # not describe a failure other than the one the Build settled on.
        self.assertFalse(
            _matches(_build_row(diagnostics=["CPP_LINK_ERROR"]), _evidence_row())
        )

    def test_a_failure_stage_that_disagrees_is_rejected(self) -> None:
        self.assertFalse(_matches(_build_row(stage="HIDDEN_TEST"), _evidence_row()))

    def test_a_certification_payload_is_not_a_rejection(self) -> None:
        certified = _payload()
        certified["evidence_kind"] = "BUILD_CERTIFICATION"
        self.assertFalse(_matches(_build_row(), _evidence_row(certified)))


class RejectionEvidenceDigestIsRecomputedTests(unittest.TestCase):
    def test_a_stale_reference_digest_is_rejected(self) -> None:
        # The digest is recomputed from the payload rather than trusted, so a
        # payload edited after the fact cannot keep its old signature.
        evidence = _evidence_row()
        evidence.evidence_json["evidence_ref"]["sha256"] = "0" * 64
        self.assertFalse(_matches(_build_row(), evidence))

    def test_a_tampered_payload_no_longer_matches_its_reference(self) -> None:
        evidence = _evidence_row()
        evidence.evidence_json["payload"]["failure_code"] = "SOMETHING_ELSE"
        self.assertFalse(_matches(_build_row(), evidence))


if __name__ == "__main__":
    unittest.main()
