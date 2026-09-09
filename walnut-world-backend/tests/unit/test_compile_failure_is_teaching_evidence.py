"""A learner stuck on compiler errors must be visible to the teaching policy.

A rejected Build used to produce no Evidence at all. Only Runs did. The pedagogy
policy decides what 叮当 may say from validated failure Evidence, so a child who
could not get their code to compile looked, to the whole teaching system, like a
child who had never failed: `failure_count` stayed 0, the phase never left
REVIEW/HEURISTIC, and every hint came back as another opening-level question
about the same thing. Observed live: 8 compile failures, 0 Evidence rows, and 25
consecutive `question` responses that were the same question reworded.

These tests pin the two halves of the fix that do not need a database:

* one class of compile failure is identified stably, so repeats of the *same*
  mistake can be recognised and different mistakes are not merged;
* a rejected Build can carry the exact failed-attempt identity required at the
  bug-agent threshold without inventing a Run.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT / "src"))

from yaya_agent_contracts import EvidenceRef  # noqa: E402
from yaya_agent_runtime import BUG_FAILURE_THRESHOLD, GameEvent  # noqa: E402

from walnut_backend.workers.turn_worker import (  # noqa: E402
    _compile_failure_key,
)


def _rejection(
    *,
    stage: str = "COMPILE",
    code: str = "SANDBOX_COMPILE_ERROR",
    diagnostics: list[str] | None = None,
) -> dict[str, object]:
    return {
        "evidence_kind": "BUILD_REJECTION",
        "failure_stage": stage,
        "failure_code": code,
        "diagnostic_codes": ["error: expected primary-expression"]
        if diagnostics is None
        else diagnostics,
    }


class CompileFailureIdentityTests(unittest.TestCase):
    """`_compile_failure_key` decides what counts as "the same mistake again"."""

    def test_the_same_rejection_yields_the_same_key(self) -> None:
        self.assertEqual(_compile_failure_key(_rejection()), _compile_failure_key(_rejection()))

    def test_diagnostic_order_does_not_change_the_key(self) -> None:
        # The compiler is free to report diagnostics in any order; a learner who
        # made one mistake must not look like they made two.
        forward = _rejection(diagnostics=["error: A", "error: B"])
        reverse = _rejection(diagnostics=["error: B", "error: A"])
        self.assertEqual(_compile_failure_key(forward), _compile_failure_key(reverse))

    def test_a_different_diagnostic_is_a_different_failure(self) -> None:
        self.assertNotEqual(
            _compile_failure_key(_rejection(diagnostics=["error: A"])),
            _compile_failure_key(_rejection(diagnostics=["error: B"])),
        )

    def test_a_different_stage_is_a_different_failure(self) -> None:
        # Failing to compile and failing the test suite are not the same
        # struggle, and must not be counted as a repeat of one another.
        self.assertNotEqual(
            _compile_failure_key(_rejection(stage="COMPILE")),
            _compile_failure_key(_rejection(stage="TEST")),
        )

    def test_a_missing_diagnostic_list_still_yields_a_key(self) -> None:
        # Evidence written by an older worker, or a failure with no per-line
        # diagnostics, must degrade to a usable key rather than raising.
        bare = {"evidence_kind": "BUILD_REJECTION", "failure_stage": "COMPILE"}
        self.assertIsInstance(_compile_failure_key(bare), str)
        self.assertEqual(_compile_failure_key(bare), _compile_failure_key(dict(bare)))


class BuildOnlyBugThresholdTests(unittest.TestCase):
    def test_exact_rejected_build_reaches_the_bug_threshold_without_a_run(self) -> None:
        evidence = EvidenceRef(
            evidence_id="evidence_build_rejection_0001",
            evidence_type="TEST_REPORT",
            created_at=datetime(2026, 9, 2, tzinfo=UTC),
            sha256="a" * 64,
        )
        event = GameEvent(
            event_id="gameevent_build_rejection_0001",
            event_type="hint_requested",
            student_id="student_build_rejection_0001",
            task_id="task_build_rejection_0001",
            session_id="session_build_rejection_0001",
            turn_id="turn_build_rejection_0001",
            command_id="cmd_build_rejection_0001",
            occurred_at=datetime(2026, 9, 2, tzinfo=UTC),
            expected_world_revision=0,
            build_id="build_rejection_0001",
            failure_count=BUG_FAILURE_THRESHOLD,
            failure_key="compile:COMPILE:SANDBOX_COMPILE_ERROR:error",
            evidence_refs=(evidence,),
        )

        self.assertIsNone(event.run_id)
        self.assertEqual(event.build_id, "build_rejection_0001")
        self.assertEqual(event.failure_count, BUG_FAILURE_THRESHOLD)


if __name__ == "__main__":
    unittest.main()
