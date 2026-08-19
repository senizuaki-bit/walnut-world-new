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
* the streak reported to the policy stops below the bug-agent threshold, because
  a hint at that threshold must name an exact failed Run and a compile rejection
  has none.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT / "src"))

from yaya_agent_runtime import BUG_FAILURE_THRESHOLD  # noqa: E402

from walnut_backend.workers.turn_worker import (  # noqa: E402
    _COMPILE_FAILURE_REPORT_CEILING,
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


class ReportedStreakStaysBelowTheBugThresholdTests(unittest.TestCase):
    """The cap is a contract requirement, not a tuning choice."""

    def test_the_ceiling_is_below_the_bug_threshold(self) -> None:
        # GameEvent rejects a hint_requested at or above the threshold unless it
        # names an exact failed Run. Compile rejections have no Run, so reporting
        # the true streak there would make the event unconstructible.
        self.assertLess(_COMPILE_FAILURE_REPORT_CEILING, BUG_FAILURE_THRESHOLD)

    def test_the_ceiling_still_leaves_the_review_phase(self) -> None:
        # It has to be at least 1, or the policy would keep seeing "never failed"
        # and this whole change would buy the learner nothing.
        self.assertGreaterEqual(_COMPILE_FAILURE_REPORT_CEILING, 1)


if __name__ == "__main__":
    unittest.main()
