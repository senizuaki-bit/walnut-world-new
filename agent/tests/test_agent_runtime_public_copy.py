from __future__ import annotations

import sys
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from agent_runtime_fixtures import (  # noqa: E402
    NOW,
    WORLD_ID,
    make_context,
    make_event,
    make_evidence,
    make_learner_profile,
    make_operation,
    make_role_config,
    make_session,
    make_skill,
    make_task,
)
from yaya_agent_contracts import EvidenceRef, EvidenceType, WorldCommitReceipt  # noqa: E402
from yaya_agent_runtime import (  # noqa: E402
    PEDAGOGY_POLICY_VERSION,
    DecisionDraft,
    LearnerInference,
    RunResultSnapshot,
    SkillVersionSummary,
    TeachingDirective,
    TeachingPhase,
    TurnContext,
    validate_decision,
    world_commit_receipt_sha256,
)
from yaya_agent_runtime.errors import InvalidAgentOutput  # noqa: E402
from yaya_agent_runtime.evidence import build_evidence_aliases  # noqa: E402


def _draft(
    role: str,
    response_type: str,
    message: str,
    *,
    question: str | None = None,
    learner_inference: LearnerInference | None = None,
) -> DecisionDraft:
    return DecisionDraft(
        role=role,
        response_type=response_type,
        message=message,
        question=question,
        hint_level=None,
        learner_inference=learner_inference,
        skill_patch=None,
        requires_student_confirmation=False,
    )


def _failed_run(event, operation, *, suffix: str = "0001") -> RunResultSnapshot:
    return RunResultSnapshot(
        run_id=f"run_failed_history_{suffix}",
        session_id=event.session_id,
        turn_id=f"turn_failed_history_{suffix}",
        command_id=f"cmd_failed_history_{suffix}",
        world_id=WORLD_ID,
        skill_ref=event.skill_ref,
        task_success=False,
        world_revision_before=event.expected_world_revision,
        world_revision_after=event.expected_world_revision,
        world_difference={"watered_plots": 7, "total_plots": 8},
        failed_actions=({"reason": "short_loop"},),
        failure_key="watering_loop_short",
        evidence_refs=(make_evidence(f"evidence_failed_history_{suffix}"),),
        world_commit=None,
        request_context=operation,
    )


class AgentRuntimeCanonicalPublicCopyTests(unittest.TestCase):
    def test_world_copy_replaces_model_8_of_8_claim_with_task_facts(self) -> None:
        context = make_context("world_agent")
        self.assertIsNotNone(context.world)
        assert context.world is not None
        plots = context.world.visible_state["plots"]
        self.assertEqual(sum(plot["hydration"] == 0 for plot in plots), 8)
        model_lie = "All 8/8 plots are watered and the task is already complete."

        validated = validate_decision(
            _draft("world_agent", "message", model_lie),
            make_role_config("world_agent"),
            context,
            (),
        )

        expected = (
            f"{context.task.story} 新任务“{context.task.title}”。可观察目标：{context.task.goal}"
        )
        self.assertEqual(validated.message, expected)
        self.assertNotIn("8/8", validated.message)
        self.assertNotIn("already complete", validated.message)

    def test_teaching_copy_replaces_false_success_in_message_and_question(self) -> None:
        operation = make_operation()
        event = make_event("run_failed")
        run = RunResultSnapshot(
            run_id=event.run_id,
            session_id=event.session_id,
            turn_id=event.turn_id,
            command_id=event.command_id,
            world_id=WORLD_ID,
            skill_ref=event.skill_ref,
            task_success=False,
            world_revision_before=event.expected_world_revision,
            world_revision_after=event.expected_world_revision,
            world_difference={"watered_plots": 7, "total_plots": 8},
            failed_actions=({"reason": "short_loop"},),
            failure_key=event.failure_key,
            evidence_refs=event.evidence_refs,
            world_commit=None,
            request_context=operation,
        )
        context = TurnContext(
            role="teaching_agent",
            event=event,
            task=make_task(operation),
            session=make_session(operation=operation),
            hint_level=0,
            skill=make_skill(operation),
            run_result=run,
            learner_profile=make_learner_profile(operation),
            teaching_directive=TeachingDirective(
                phase=TeachingPhase.RECTIFICATION,
                target_concept="for_loop",
                hint_level=0,
                allowed_response_types=("question", "hint"),
                patch_eligible=False,
                full_solution_eligible=False,
                required_evidence_ids=tuple(item.evidence_id for item in event.evidence_refs),
                reason_codes=(
                    "VALIDATED_FAILURE_EVIDENCE",
                    "PATCH_DISABLED_RUNTIME_STAGE",
                    "FULL_SOLUTION_DISABLED",
                ),
                pedagogy_policy_version=PEDAGOGY_POLICY_VERSION,
                learner_revision=0,
                teaching_spec_version="teaching-1",
            ),
        )
        fake_success = _draft(
            "teaching_agent",
            "question",
            "The run watered 8/8 plots successfully.",
            question="Since every plot succeeded, which success should we celebrate?",
            learner_inference=LearnerInference(
                concept="for_loop",
                score_delta=-0.1,
                confidence=0.9,
                reason="The current failed run is the bounded inference source.",
                evidence_ids=tuple(
                    build_evidence_aliases(context)[0][item.evidence_id]
                    for item in event.evidence_refs
                ),
            ),
        )

        validated = validate_decision(
            fake_success,
            make_role_config(
                "teaching_agent",
                allowed_events=("run_failed",),
                allowed_tools=(),
            ),
            context,
            (),
        )

        self.assertEqual(
            validated.message,
            "规范运行记录确认任务尚未完成；失败类型为 watering_loop_short。",
        )
        self.assertEqual(
            validated.question,
            "哪一步的可观察结果和你的预期不同？",
        )
        self.assertNotIn("8/8", validated.message)
        self.assertNotIn("succeeded", validated.question or "")

    def test_book_permanent_mastery_is_rejected_before_publication(self) -> None:
        operation = make_operation()
        event = make_event("task_completed")
        world_commit = WorldCommitReceipt(
            world_id=WORLD_ID,
            previous_revision=event.expected_world_revision,
            world_revision=event.expected_world_revision + 1,
            first_event_sequence=41,
            last_event_sequence=48,
            committed_at=NOW,
            state_hash="f" * 64,
        )
        world_evidence = EvidenceRef(
            "evidence_book_world_commit_0001",
            EvidenceType.WORLD_COMMIT,
            NOW,
            sha256=world_commit_receipt_sha256(world_commit),
        )
        success = RunResultSnapshot(
            run_id=event.run_id,
            session_id=event.session_id,
            turn_id=event.turn_id,
            command_id=event.command_id,
            world_id=WORLD_ID,
            skill_ref=event.skill_ref,
            task_success=True,
            world_revision_before=event.expected_world_revision,
            world_revision_after=event.expected_world_revision + 1,
            world_difference={"watered_plots": 8, "total_plots": 8},
            failed_actions=(),
            failure_key=None,
            evidence_refs=(*event.evidence_refs, world_evidence),
            world_commit=world_commit,
            request_context=operation,
        )
        skill = make_skill(operation)
        history = tuple(
            SkillVersionSummary(
                event.session_id,
                skill.ref.skill_id,
                f"skill_version_history_{index:04d}",
                str(index) * 64,
                f"Recorded version {index}.",
                operation,
            )
            for index in (1, 2)
        )
        context = TurnContext(
            role="book_agent",
            event=event,
            task=make_task(operation),
            session=make_session(operation=operation),
            hint_level=0,
            run_result=success,
            learner_profile=make_learner_profile(operation),
            session_runs=(
                _failed_run(event, operation, suffix="0001"),
                success,
                _failed_run(event, operation, suffix="0002"),
            ),
            skill_history=history,
            teaching_directive=TeachingDirective(
                phase=TeachingPhase.SUMMARIZATION,
                target_concept="for_loop",
                hint_level=0,
                allowed_response_types=("growth_summary",),
                patch_eligible=False,
                full_solution_eligible=False,
                required_evidence_ids=tuple(item.evidence_id for item in event.evidence_refs),
                reason_codes=(
                    "TASK_COMPLETED_WITH_SUCCESS_EVIDENCE",
                    "PATCH_DISABLED_RUNTIME_STAGE",
                    "FULL_SOLUTION_DISABLED",
                ),
                pedagogy_policy_version=PEDAGOGY_POLICY_VERSION,
                learner_revision=0,
                teaching_spec_version="teaching-1",
            ),
        )
        cases = (
            (
                "message_en",
                "You have permanent mastery now and never had a failed run.",
                "The current completed run is the bounded inference source.",
            ),
            (
                "message_zh",
                "你已经永久掌握这个概念，以后永不再犯任何错误。",
                "本次已完成运行是唯一的有界推断来源。",
            ),
            (
                "inference_reason_en",
                "This summary records one verified completed run.",
                "This evidence proves permanent mastery and that the learner will never fail again.",
            ),
            (
                "inference_reason_zh",
                "本总结只记录一次经过验证的成功运行。",
                "这份证据证明学生已经永久掌握，以后永不再犯。",
            ),
        )
        for name, message, inference_reason in cases:
            with self.subTest(name=name):
                publication = []
                with self.assertRaises(InvalidAgentOutput) as raised:
                    validated = validate_decision(
                        _draft(
                            "book_agent",
                            "growth_summary",
                            message,
                            learner_inference=LearnerInference(
                                concept="for_loop",
                                score_delta=0.1,
                                confidence=0.9,
                                reason=inference_reason,
                                evidence_ids=tuple(
                                    build_evidence_aliases(context)[0][item.evidence_id]
                                    for item in event.evidence_refs
                                ),
                            ),
                        ),
                        make_role_config(
                            "book_agent",
                            allowed_events=("task_completed",),
                            allowed_tools=(),
                        ),
                        context,
                        (),
                    )
                    publication.append(validated)
                self.assertEqual(raised.exception.code, "PERMANENT_LEARNER_JUDGMENT")
                self.assertEqual(publication, [])


if __name__ == "__main__":
    unittest.main()


class HintKeepsProviderTeachingProseTests(unittest.TestCase):
    """A hint is the one teaching turn whose prose is the deliverable.

    Every other teaching turn restates a canonical compile/run fact, so its copy
    is deterministic.  A hint has no such fact, and replacing it collapsed every
    hint to one content-free sentence -- the student pressed the button and
    learned nothing.  These pin that the model's own reading of the source
    survives, while the outcome-claim and permanent-judgment bans still hold.
    """

    def _hint_context(self) -> TurnContext:
        operation = make_operation()
        event = make_event("hint_requested")
        return TurnContext(
            role="teaching_agent",
            event=event,
            task=make_task(operation),
            session=make_session(operation=operation),
            hint_level=1,
            skill=make_skill(operation),
            learner_profile=make_learner_profile(operation),
            teaching_directive=TeachingDirective(
                phase=TeachingPhase.REVIEW,
                target_concept="for_loop",
                hint_level=1,
                allowed_response_types=("question", "hint"),
                patch_eligible=False,
                full_solution_eligible=False,
                required_evidence_ids=(),
                reason_codes=(
                    "LEARNER_REVISION_ZERO",
                    "PATCH_DISABLED_RUNTIME_STAGE",
                    "FULL_SOLUTION_DISABLED",
                ),
                pedagogy_policy_version=PEDAGOGY_POLICY_VERSION,
                learner_revision=0,
                teaching_spec_version="agent-teaching-v1",
            ),
        )

    def test_hint_question_retains_provider_message_and_question(self) -> None:
        context = self._hint_context()
        decision = validate_decision(
            _draft(
                "teaching_agent",
                "question",
                "你写的循环里用了 if 和 else if 来判断每个地块需要浇多少水。",
                question="如果 gap 正好等于 30，程序会输出 WATER 2 还是 WATER 1？",
            ),
            make_role_config("teaching_agent"),
            context,
            (),
        )
        self.assertEqual(
            decision.message,
            "你写的循环里用了 if 和 else if 来判断每个地块需要浇多少水。",
        )
        self.assertEqual(
            decision.question,
            "如果 gap 正好等于 30，程序会输出 WATER 2 还是 WATER 1？",
        )

    def test_hint_keeps_a_long_message_that_fits_the_role_limit(self) -> None:
        context = self._hint_context()
        config = make_role_config("teaching_agent")
        message = "边" * config.limits.max_message_chars
        decision = validate_decision(
            _draft("teaching_agent", "question", message, question="再看一次？"),
            config,
            context,
            (),
        )
        self.assertEqual(decision.message, message)

    def test_hint_over_the_role_limit_is_rejected_not_truncated(self) -> None:
        # Rejecting lets the model shorten on its repair round; truncating would
        # hand a child half a sentence.
        context = self._hint_context()
        config = make_role_config("teaching_agent")
        with self.assertRaises(InvalidAgentOutput) as raised:
            validate_decision(
                _draft(
                    "teaching_agent",
                    "question",
                    "边" * (config.limits.max_message_chars + 1),
                    question="再看一次？",
                ),
                config,
                context,
                (),
            )
        self.assertEqual(raised.exception.code, "MESSAGE_TOO_LONG")

    def test_hint_cannot_claim_the_task_succeeded(self) -> None:
        context = self._hint_context()
        with self.assertRaises(InvalidAgentOutput) as raised:
            validate_decision(
                _draft(
                    "teaching_agent",
                    "question",
                    "你已经全部完成了这一关。",
                    question="还要再看一遍吗？",
                ),
                make_role_config("teaching_agent"),
                context,
                (),
            )
        self.assertEqual(raised.exception.code, "HINT_CLAIMS_OUTCOME")

    def test_hint_cannot_claim_the_outcome_through_its_question(self) -> None:
        context = self._hint_context()
        with self.assertRaises(InvalidAgentOutput) as raised:
            validate_decision(
                _draft(
                    "teaching_agent",
                    "question",
                    "再看看这段循环。",
                    question="既然任务已经完成，要不要挑战下一关？",
                ),
                make_role_config("teaching_agent"),
                context,
                (),
            )
        self.assertEqual(raised.exception.code, "HINT_CLAIMS_OUTCOME")

    def test_hint_still_rejects_permanent_learner_judgment(self) -> None:
        context = self._hint_context()
        with self.assertRaises(InvalidAgentOutput) as raised:
            validate_decision(
                _draft(
                    "teaching_agent",
                    "question",
                    "你完全不会写循环。",
                    question="要不要换一关？",
                ),
                make_role_config("teaching_agent"),
                context,
                (),
            )
        self.assertEqual(raised.exception.code, "PERMANENT_LEARNER_JUDGMENT")
