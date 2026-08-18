"""One crashed attempt must not disable a Session forever.

Two independent places used to turn a single bad Turn into a permanent
lock-out, and both are pinned here.

**Crashed Run wreckage.** A Run row is written when the Sandbox result lands,
but the Turn that owns it still has to settle its Command. If the workflow dies
in between -- a dead-lettered job, a Command stuck at FAILED -- the Run survives
describing an attempt that never completed. History replay then demanded that
every Run agree with its Command, hit the mismatch, and dead-lettered the new
Turn too. The damage reproduced itself on every later attempt, and the rows are
append-only (`INT2 authority rows are append-only`), so it could not be cleaned
up either.

**Repeated Skill versions.** Re-activating a version the Registry already knows
is ordinary: a student who edits their code and changes it back rebuilds to an
identical artifact, so the version id repeats. Skill history treated that as
corruption, which meant one re-activation stopped the learner from ever
completing a task again -- history is replayed on every completion.

Neither relaxation weakens the real guarantees, and the last two tests pin that:
a Turn that *did* settle is still held to its Run in full.
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT / "src"))

from yaya_agent_contracts import CommandStatus  # noqa: E402

from walnut_backend.adapters.postgres.run_outcomes import (  # noqa: E402
    _abandoned_run_attempt,
    _validate_terminal_command,
)
from walnut_backend.adapters.postgres.workflow_jobs import (  # noqa: E402
    WorkflowInvariantError,
)


@dataclass
class _Command:
    terminal: bool
    status: CommandStatus
    evidence_refs: tuple[Any, ...] = ()
    links: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.links is None:
            self.links = {}


@dataclass
class _Run:
    run_id: str = "run_0001"
    task_success: bool = True
    evidence_refs: tuple[Any, ...] = ()


@dataclass
class _Job:
    status: str


@dataclass
class _Authority:
    command: _Command
    run: _Run
    job: _Job


def _settled_success() -> _Authority:
    """A Turn that completed normally: Command APPLIED, job SUCCEEDED."""

    return _Authority(
        command=_Command(
            terminal=True,
            status=CommandStatus.APPLIED,
            links={"run": "/v1/runs/run_0001"},
        ),
        run=_Run(),
        job=_Job(status="SUCCEEDED"),
    )


class CrashedAttemptTests(unittest.TestCase):
    def test_dead_lettered_job_is_wreckage(self) -> None:
        authority = _settled_success()
        authority.job = _Job(status="DEAD_LETTER")
        self.assertTrue(_abandoned_run_attempt(authority))

    def test_command_stuck_at_failed_is_wreckage(self) -> None:
        # The exact shape seen in production: the Sandbox produced a Run, then
        # the Turn died before its Command could settle.
        authority = _settled_success()
        authority.command = _Command(terminal=True, status=CommandStatus.FAILED)
        authority.job = _Job(status="DEAD_LETTER")
        self.assertTrue(_abandoned_run_attempt(authority))

    def test_non_terminal_command_is_wreckage(self) -> None:
        authority = _settled_success()
        authority.command = _Command(terminal=False, status=CommandStatus.APPLYING_WORLD)
        self.assertTrue(_abandoned_run_attempt(authority))

    def test_cancelled_command_is_wreckage(self) -> None:
        authority = _settled_success()
        authority.command = _Command(terminal=True, status=CommandStatus.CANCELLED)
        self.assertTrue(_abandoned_run_attempt(authority))

    def test_a_completed_success_is_real_history(self) -> None:
        self.assertFalse(_abandoned_run_attempt(_settled_success()))

    def test_a_completed_rejection_is_real_history(self) -> None:
        # A task the student got wrong still settles, and still counts.
        authority = _settled_success()
        authority.command = _Command(
            terminal=True,
            status=CommandStatus.REJECTED,
            links={"run": "/v1/runs/run_0001"},
        )
        authority.run = _Run(task_success=False)
        self.assertFalse(_abandoned_run_attempt(authority))


class SettledTurnsAreStillHeldToTheirRunTests(unittest.TestCase):
    """The relaxation must not become a way to smuggle a bad Command through."""

    def test_settled_command_disagreeing_with_its_run_is_still_rejected(self) -> None:
        # APPLIED claims success while the Run says the task failed.
        authority = _settled_success()
        authority.run = _Run(task_success=False)
        self.assertFalse(_abandoned_run_attempt(authority))
        with self.assertRaises(WorkflowInvariantError):
            _validate_terminal_command(authority)

    def test_settled_command_missing_its_run_link_is_still_rejected(self) -> None:
        authority = _settled_success()
        authority.command.links = {}
        self.assertFalse(_abandoned_run_attempt(authority))
        with self.assertRaises(WorkflowInvariantError):
            _validate_terminal_command(authority)

    def test_a_genuinely_consistent_settled_turn_passes(self) -> None:
        _validate_terminal_command(_settled_success())


if __name__ == "__main__":
    unittest.main()
