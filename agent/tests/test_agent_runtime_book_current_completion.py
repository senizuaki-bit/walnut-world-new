"""A completed Run must not rescan old failed turns to produce its summary."""

from __future__ import annotations

import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from agent_runtime_fixtures import (  # noqa: E402
    NOW,
    WORLD_ID,
    InMemoryWateringInvocations,
    RecordingReads,
    TraceSink,
    make_event,
    make_world_state,
)
from yaya_agent_contracts import EvidenceRef, EvidenceType, WorldCommitReceipt  # noqa: E402
from yaya_agent_runtime import (  # noqa: E402
    AgentToolAuthorizationError,
    ContextBuilder,
    DecisionDraft,
    LearnerInference,
    PackagedRoleConfigProvider,
    PromptBuilder,
    RunResultSnapshot,
    SkillVersionSummary,
    TeachingPhase,
    build_default_tool_registry,
    validate_decision,
    world_commit_receipt_sha256,
)
from yaya_agent_runtime.context_builder import validate_context_for_role  # noqa: E402
from yaya_agent_runtime.evidence import build_evidence_aliases  # noqa: E402


class _BookReads(RecordingReads):
    def __init__(self) -> None:
        super().__init__()
        self.event = make_event("task_completed")
        commit = WorldCommitReceipt(WORLD_ID, 5, 6, 41, 48, NOW, "f" * 64)
        world_evidence = EvidenceRef(
            "evidence_book_world_commit_0001",
            EvidenceType.WORLD_COMMIT,
            NOW,
            sha256=world_commit_receipt_sha256(commit),
        )
        self.run = RunResultSnapshot(
            run_id=self.event.run_id,
            session_id=self.event.session_id,
            turn_id=self.event.turn_id,
            command_id=self.event.command_id,
            world_id=WORLD_ID,
            skill_ref=self.skill.ref,
            task_success=True,
            world_revision_before=5,
            world_revision_after=6,
            world_difference={"watered_plots": 8},
            failed_actions=(),
            failure_key=None,
            evidence_refs=(*self.event.evidence_refs, world_evidence),
            world_commit=commit,
            request_context=self.operation,
        )

    async def get_run(self, run_id, context):
        self.calls.append("get_run")
        assert run_id == self.run.run_id
        assert context == self.operation
        return self.run

    async def list_session_runs(self, *_args):
        raise AssertionError("completed Run must not depend on old Run history")

    async def list_skill_history(self, *_args):
        self.calls.append("list_skill_history")
        return (
            SkillVersionSummary(
                self.event.session_id,
                self.skill.ref.skill_id,
                self.skill.ref.skill_version_id,
                self.skill.source_sha256,
                "Current certified version.",
                self.operation,
            ),
        )


def _builder(reads):
    return ContextBuilder(
        tasks=reads,
        sessions=reads,
        skills=reads,
        runs=reads,
        counterexamples=reads,
        learners=reads,
        messages=reads,
        worlds=reads,
        role_configs=PackagedRoleConfigProvider.load(),
    )


def _draft(context):
    aliases, _ = build_evidence_aliases(context)
    directive = context.teaching_directive
    return DecisionDraft(
        role="book_agent",
        response_type="growth_summary",
        message="This completed run provides one bounded learning observation.",
        question=None,
        hint_level=None,
        learner_inference=LearnerInference(
            concept=directive.target_concept,
            score_delta=0.1,
            confidence=0.8,
            reason="The current run completed the observable task.",
            evidence_ids=tuple(aliases[item] for item in directive.required_evidence_ids),
        ),
        skill_patch=None,
        requires_student_confirmation=False,
    )


class BookCurrentCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_book_build_reads_current_completion_without_run_history(self) -> None:
        reads = _BookReads()
        context = await _builder(reads).build(reads.event, "book_agent", reads.operation)

        self.assertEqual(
            reads.calls,
            ["get_task", "get_session", "get_run", "list_skill_history", "get_profile"],
        )
        self.assertEqual(context.run_result, reads.run)
        self.assertEqual(context.session_runs, ())
        self.assertEqual(context.teaching_directive.phase, TeachingPhase.SUMMARIZATION)
        self.assertEqual(context.learner_profile, reads.learner_profile)
        validate_context_for_role(context)

    async def test_current_summary_keeps_learning_evidence_without_session_counts(self) -> None:
        reads = _BookReads()
        context = await _builder(reads).build(reads.event, "book_agent", reads.operation)
        config = PackagedRoleConfigProvider.load().get("book_agent")
        decision = validate_decision(_draft(context), config, context, ())

        self.assertIn("本次", decision.message)
        self.assertIn(context.task.title, decision.message)
        self.assertIn("你写的程序达成了当前任务目标", decision.message)
        self.assertNotIn("世界提交", decision.message)
        self.assertNotIn("Skill", decision.message)
        self.assertNotIn("本 Session 共记录", decision.message)
        self.assertEqual(
            decision.learner_inference.evidence_ids,
            context.teaching_directive.required_evidence_ids,
        )
        prompt = PromptBuilder().initial_messages(config, context, ())
        payload = json.loads(prompt[1].content)["turn_context"]
        self.assertNotIn("session_runs", payload)
        self.assertIn("run_result", payload)
        self.assertIn("本次完成", prompt[0].content)

    async def test_history_tool_is_absent_for_current_summary_and_retained_for_legacy(self) -> None:
        reads = _BookReads()
        context = await _builder(reads).build(reads.event, "book_agent", reads.operation)
        config = PackagedRoleConfigProvider.load().get("book_agent")
        registry = build_default_tool_registry(
            TraceSink(),
            InMemoryWateringInvocations(reads.operation, reads.skill, make_world_state()),
        )
        definitions = registry.model_definitions("book_agent", config.allowed_tools, context)
        self.assertNotIn("get_session_runs", {item["name"] for item in definitions})
        with self.assertRaises(AgentToolAuthorizationError) as rejected:
            registry.validate_call(
                role="book_agent",
                allowed_names=config.allowed_tools,
                name="get_session_runs",
                arguments={},
                turn_context=context,
            )
        self.assertEqual(rejected.exception.code, "TOOL_UNAVAILABLE_FOR_CONTEXT")

        legacy = replace(context, session_runs=(reads.run,))
        validate_context_for_role(legacy)
        legacy_definitions = registry.model_definitions("book_agent", config.allowed_tools, legacy)
        self.assertIn("get_session_runs", {item["name"] for item in legacy_definitions})
        legacy_decision = validate_decision(_draft(legacy), config, legacy, ())
        self.assertIn("本 Session 共记录 1 次运行，其中 0 次尚未完成", legacy_decision.message)


if __name__ == "__main__":
    unittest.main()
