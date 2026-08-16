from __future__ import annotations

import hashlib
import unittest
from collections.abc import Mapping
from dataclasses import replace
from typing import cast

from agent_runtime_fixtures import (
    NOW,
    make_event,
    make_evidence,
    make_operation,
    make_task,
)
from yaya_agent_backend.codec import plain
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    EvidenceType,
    OperationContext,
    SkillRef,
    canonical_json_sha256,
)
from yaya_agent_runtime import (
    GameEvent,
    RunOutcomeInvariantError,
    RunResultSnapshot,
    derive_run_outcome_event,
)


def _failed_run(operation: OperationContext | None = None) -> RunResultSnapshot:
    authority = operation or make_operation()
    root = make_event("run_skill_requested")
    return RunResultSnapshot(
        run_id="run_watering_0001",
        session_id=root.session_id,
        turn_id=root.turn_id,
        command_id=root.command_id,
        world_id="world_watering_0001",
        skill_ref=cast(SkillRef, root.skill_ref),
        task_success=False,
        world_revision_before=root.expected_world_revision,
        world_revision_after=root.expected_world_revision,
        world_difference={"score": 0},
        failed_actions=({"reason": "watering_loop_short"},),
        failure_key="watering_loop_short",
        evidence_refs=(make_evidence("evidence_sandbox_0001", EvidenceType.SANDBOX_LOG),),
        world_commit=None,
        request_context=authority,
    )


class RunOutcomeTests(unittest.TestCase):
    def test_preserves_frozen_a8_identity_and_role_insensitive_actor_scope(self) -> None:
        root = make_event("run_skill_requested")
        run_operation = make_operation()
        task_operation = replace(
            run_operation,
            actor=ActorRef(
                tenant_id=run_operation.actor.tenant_id,
                actor_id=run_operation.actor.actor_id,
                actor_type=run_operation.actor.actor_type,
                roles=("game:player", "content:reader"),
            ),
        )
        run = _failed_run(run_operation)

        outcome = derive_run_outcome_event(
            root_event=root,
            run=run,
            task=make_task(task_operation),
            failure_count=3,
            occurred_at=NOW,
        )

        framed = "".join(
            f"{len(part)}:{part}"
            for part in (root.command_id, root.turn_id, run.run_id, "run_failed")
        )
        self.assertEqual(
            outcome.event_id,
            f"evt_outcome_{hashlib.sha256(framed.encode('utf-8')).hexdigest()[:24]}",
        )
        self.assertEqual(outcome.event_type, "run_failed")
        self.assertEqual(outcome.failure_count, 3)
        self.assertEqual(outcome.failure_key, run.failure_key)
        self.assertEqual(outcome.evidence_refs, run.evidence_refs)
        self.assertEqual(
            canonical_json_sha256(cast(Mapping[str, object], plain(outcome))),
            "0d60b58b110434a4e02f1b973a2506f145695c6019cd3aeb93c497fdd224120c",
        )

    def test_preserves_legacy_root_student_bytes_when_adapter_authority_allows_them(self) -> None:
        root = replace(
            make_event("run_skill_requested"),
            student_id="student_legacy_root_0001",
        )
        run = _failed_run()
        task = make_task()

        outcome = derive_run_outcome_event(
            root_event=root,
            run=run,
            task=task,
            failure_count=2,
            occurred_at=NOW,
        )

        framed = "".join(
            f"{len(part)}:{part}"
            for part in (root.command_id, root.turn_id, run.run_id, "run_failed")
        )
        legacy = GameEvent(
            event_id=f"evt_outcome_{hashlib.sha256(framed.encode('utf-8')).hexdigest()[:24]}",
            event_type="run_failed",
            student_id=root.student_id,
            task_id=root.task_id,
            session_id=root.session_id,
            turn_id=root.turn_id,
            command_id=root.command_id,
            occurred_at=NOW,
            expected_world_revision=run.world_revision_before,
            skill_ref=run.skill_ref,
            run_id=run.run_id,
            failure_count=2,
            failure_key=run.failure_key,
            evidence_refs=run.evidence_refs,
            payload={"concept": task.knowledge_points[0]},
        )
        self.assertEqual(outcome, legacy)
        self.assertEqual(plain(outcome), plain(legacy))
        self.assertEqual(
            canonical_json_sha256(cast(Mapping[str, object], plain(outcome))),
            canonical_json_sha256(cast(Mapping[str, object], plain(legacy))),
        )

    def test_rejects_cross_actor_task_authority(self) -> None:
        root = make_event("run_skill_requested")
        operation = make_operation()
        other = replace(
            operation,
            actor=ActorRef(
                tenant_id=operation.actor.tenant_id,
                actor_id="student_other_0001",
                actor_type=ActorType.STUDENT,
                roles=operation.actor.roles,
            ),
        )
        with self.assertRaisesRegex(RunOutcomeInvariantError, "Task cannot supply"):
            derive_run_outcome_event(
                root_event=root,
                run=_failed_run(operation),
                task=make_task(other),
                failure_count=1,
                occurred_at=NOW,
            )

    def test_failed_run_requires_positive_exact_suffix(self) -> None:
        for failure_count in (0, -1):
            with self.subTest(failure_count=failure_count):
                with self.assertRaisesRegex(RunOutcomeInvariantError, "exact failure suffix"):
                    derive_run_outcome_event(
                        root_event=make_event("run_skill_requested"),
                        run=_failed_run(),
                        task=make_task(),
                        failure_count=failure_count,
                        occurred_at=NOW,
                    )


if __name__ == "__main__":
    unittest.main()
