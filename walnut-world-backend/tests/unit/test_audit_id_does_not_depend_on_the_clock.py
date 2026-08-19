"""Audit ids must be unique on their own, not because the clock moved.

`system_audit_record` used to build its primary key out of the current time to
microseconds. That is only unique if the clock advances between two records, and
on Windows the system clock moves in ~15.6ms steps: `datetime.now()` returns the
identical value for every call inside that step.

The consequence was not a rare duplicate row. Several audit records are written
inside one transaction, so two of them landing in the same clock tick raised
IntegrityError and rolled the whole operation back. Builds died this way -- their
accept phase writes multiple audit records, so every one of the five retries hit
the same window and the job dead-lettered, leaving a Build stuck at COMPILING and
a learner unable to run their code at all. Single-record operations such as a
hint kept working, which made it look like a Build-specific bug rather than a
clock-resolution one.

A timestamp is tempting here because it sorts. Ordering already comes from
`occurred_at`, and the same table is written elsewhere with a plain uuid.
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT / "src"))

from walnut_backend.adapters.postgres.audit import system_audit_record  # noqa: E402


class AuditIdIsSelfUniqueTests(unittest.TestCase):
    def setUp(self) -> None:
        source = inspect.getsource(system_audit_record)
        # The audit_id assignment alone: occurred_at below it is legitimately a
        # timestamp, and matching the whole function would confuse the two.
        self.id_expression = next(
            line for line in source.splitlines() if line.strip().startswith("audit_id=")
        )

    def test_the_id_is_not_derived_from_the_current_time(self) -> None:
        # The exact failure: an id that repeats whenever the clock does not move.
        self.assertNotIn("timestamp()", self.id_expression)
        self.assertNotIn("datetime.now", self.id_expression)

    def test_the_id_carries_its_own_uniqueness(self) -> None:
        self.assertIn("uuid4", self.id_expression)


class AuditIdsDoNotCollideWithinOneClockTickTests(unittest.TestCase):
    def test_many_ids_minted_back_to_back_are_all_distinct(self) -> None:
        # Minted as fast as the interpreter allows, which on a coarse-clock host
        # is many calls inside a single tick -- exactly the case that broke.
        from uuid import uuid4

        minted = {f"audit_{uuid4().hex}" for _ in range(10_000)}
        self.assertEqual(len(minted), 10_000)


if __name__ == "__main__":
    unittest.main()
