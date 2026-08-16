from __future__ import annotations

import json
import sys
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from agent_runtime_fixtures import (  # noqa: E402
    NOW,
    STUDENT_ID,
    InMemoryWateringInvocations,
    RecordingReads,
    StaticRoleConfigs,
    TraceSink,
    make_context,
    make_event,
    make_evidence,
    make_operation,
    make_role_config,
    make_session,
    make_skill,
    make_skill_ref,
    make_task,
    make_world_state,
)
from jsonschema import Draft202012Validator  # noqa: E402
from yaya_agent_contracts import ContentRef, EvidenceType, WorldCommitReceipt  # noqa: E402
from yaya_agent_runtime import (  # noqa: E402
    AgentConfigurationError,
    AgentContextError,
    AgentTool,
    AgentToolAuthorizationError,
    AgentToolExecutionError,
    AgentToolInputError,
    CompileResultSnapshot,
    ContextBuilder,
    LearnerProfileSnapshot,
    PackagedRoleConfigProvider,
    PromptBuilder,
    RunResultSnapshot,
    TeachingDirective,
    TeachingPhase,
    ToolRegistry,
    ToolResult,
    TurnContext,
    build_default_tool_registry,
    world_commit_receipt_sha256,
)
from yaya_agent_runtime.model_output import build_model_output_schema  # noqa: E402


class _TeachingReads(RecordingReads):
    def __init__(self, *, compile_result=None, run_result=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.compile_result = compile_result
        self.run_result = run_result

    async def get_compile_result(self, build_id, context):
        del build_id, context
        self.calls.append("get_compile_result")
        return self.compile_result

    async def get_run(self, run_id, context):
        del run_id, context
        self.calls.append("get_run")
        return self.run_result

    async def get_profile(self, student_id, knowledge_points, context):
        del student_id, knowledge_points, context
        self.calls.append("get_profile")
        return LearnerProfileSnapshot(STUDENT_ID, 1, {}, self.operation, ())

    async def list_recent(self, session_id, limit, context):
        del session_id, limit, context
        self.calls.append("list_recent")
        return ()


class _RecordingHandler:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error

    async def __call__(self, arguments, turn_context, execution, operation_context):
        del turn_context, execution, operation_context
        self.calls += 1
        if self.error is not None:
            raise self.error
        return ToolResult(
            value={"received": dict(arguments)},
            summary={"accepted": True},
        )


def _closed_probe_schema(_context) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["value"],
        "properties": {"value": {"type": "integer", "minimum": 0}},
    }


def _context_builder(reads: RecordingReads, configs: StaticRoleConfigs) -> ContextBuilder:
    return ContextBuilder(
        tasks=reads,
        sessions=reads,
        skills=reads,
        runs=reads,
        counterexamples=reads,
        learners=reads,
        messages=reads,
        worlds=reads,
        role_configs=configs,
    )


class AgentRuntimeContextAndToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_world_context_fetches_task_session_world_and_learner(self) -> None:
        operation = make_operation()
        reads = RecordingReads(operation=operation)
        configs = StaticRoleConfigs(make_role_config("world_agent"))
        builder = _context_builder(reads, configs)

        context = await builder.build(make_event("task_started"), "world_agent", operation)

        self.assertEqual(
            reads.calls,
            ["get_task", "get_session", "get_snapshot", "get_profile"],
        )
        self.assertIsNotNone(context.world)
        self.assertIsNone(context.skill)
        self.assertEqual(context.available_skills, ())
        self.assertIsNotNone(context.learner_profile)
        self.assertIsNotNone(context.teaching_directive)
        self.assertEqual(context.recent_messages, ())

    async def test_next_turn_directive_consumes_latest_projected_learner_revision(self) -> None:
        operation = make_operation()
        for_loop_evidence = make_evidence("evidence_profile_for_loop_0001")
        sequence_evidence = make_evidence("evidence_profile_sequence_0001")
        observed_at = (NOW - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        next_review_at = (NOW + timedelta(days=2)).isoformat().replace("+00:00", "Z")
        profile = LearnerProfileSnapshot(
            student_id=STUDENT_ID,
            revision=2,
            competencies={
                "for_loop": {
                    "concept": "for_loop",
                    "evidence_stage": "DEMONSTRATED",
                    "assistance_level": 1,
                    "last_observed_at": observed_at,
                    "next_review_at": next_review_at,
                    "evidence_ids": [for_loop_evidence.evidence_id],
                },
                "sequence": {
                    "concept": "sequence",
                    "evidence_stage": "DEMONSTRATED",
                    "assistance_level": 0,
                    "last_observed_at": observed_at,
                    "next_review_at": next_review_at,
                    "evidence_ids": [sequence_evidence.evidence_id],
                },
            },
            request_context=operation,
            evidence_refs=(for_loop_evidence, sequence_evidence),
        )
        reads = RecordingReads(operation=operation, learner_profile=profile)
        builder = _context_builder(
            reads,
            StaticRoleConfigs(make_role_config("world_agent")),
        )

        context = await builder.build(make_event("task_started"), "world_agent", operation)

        directive = context.teaching_directive
        self.assertIsNotNone(directive)
        assert directive is not None
        self.assertEqual(context.learner_profile, profile)
        self.assertEqual(directive.learner_revision, 2)
        self.assertEqual(directive.phase, TeachingPhase.HEURISTIC)
        self.assertEqual(directive.target_concept, "for_loop")
        self.assertEqual(
            directive.required_evidence_ids,
            (for_loop_evidence.evidence_id,),
        )

    async def test_skill_context_fetches_only_execution_dependencies(self) -> None:
        operation = make_operation()
        reads = RecordingReads(operation=operation)
        configs = StaticRoleConfigs(make_role_config("xiaohutao"))
        builder = _context_builder(reads, configs)

        context = await builder.build(
            make_event("run_skill_requested"),
            "xiaohutao",
            operation,
        )

        self.assertEqual(
            reads.calls,
            [
                "get_task",
                "get_session",
                "get_snapshot",
                "get_bound_skill",
                "list_active_skills",
            ],
        )
        self.assertIsNotNone(context.world)
        self.assertIsNotNone(context.skill)
        self.assertEqual(len(context.available_skills), 1)
        self.assertIsNone(context.run_result)
        self.assertEqual(context.failure_history, ())

    async def test_context_rejects_authenticated_actor_cross_link_before_reads(self) -> None:
        operation = make_operation(actor_id="student_other_0001")
        reads = RecordingReads(operation=operation)
        configs = StaticRoleConfigs(make_role_config("world_agent"))

        with self.assertRaises(AgentContextError) as raised:
            await _context_builder(reads, configs).build(
                make_event("task_started"),
                "world_agent",
                operation,
            )

        self.assertEqual(raised.exception.code, "CONTEXT_ACTOR_MISMATCH")
        self.assertEqual(reads.calls, [])

    async def test_context_rejects_session_identity_cross_link_before_world_read(self) -> None:
        operation = make_operation()
        reads = RecordingReads(
            operation=operation,
            session=make_session(student_id="student_other_0001"),
        )
        configs = StaticRoleConfigs(make_role_config("world_agent"))

        with self.assertRaises(AgentContextError) as raised:
            await _context_builder(reads, configs).build(
                make_event("task_started"),
                "world_agent",
                operation,
            )

        self.assertEqual(raised.exception.code, "CONTEXT_SESSION_MISMATCH")
        self.assertEqual(reads.calls, ["get_task", "get_session"])

    async def test_context_rejects_task_with_different_pinned_content(self) -> None:
        operation = make_operation()
        foreign_operation = replace(
            operation,
            content_ref=ContentRef("YAYA_FARM_001", "1.0.1", "f" * 64),
        )
        reads = RecordingReads(
            operation=operation,
            task=make_task(foreign_operation),
        )
        configs = StaticRoleConfigs(make_role_config("world_agent"))

        with self.assertRaises(AgentContextError) as raised:
            await _context_builder(reads, configs).build(
                make_event("task_started"),
                "world_agent",
                operation,
            )

        self.assertEqual(raised.exception.code, "CONTEXT_CONTENT_MISMATCH")
        self.assertEqual(reads.calls, ["get_task", "get_session"])

    async def test_compile_event_evidence_must_match_exact_skill_version_result(self) -> None:
        operation = make_operation()
        event = make_event("compile_failed")
        compile_result = CompileResultSnapshot(
            build_id=event.build_id,
            skill_ref=make_skill_ref(),
            succeeded=False,
            diagnostics=("missing semicolon",),
            evidence_refs=(make_evidence("evidence_compile_other_0001"),),
            request_context=operation,
        )
        reads = _TeachingReads(operation=operation, compile_result=compile_result)
        configs = StaticRoleConfigs(
            make_role_config(
                "teaching_agent",
                allowed_events=("compile_failed",),
            )
        )

        with self.assertRaises(AgentContextError) as raised:
            await _context_builder(reads, configs).build(
                event,
                "teaching_agent",
                operation,
            )

        self.assertEqual(raised.exception.code, "CONTEXT_EVIDENCE_MISMATCH")

    async def test_run_failed_rejects_success_or_wrong_world_revision(self) -> None:
        operation = make_operation()
        event = make_event("run_failed")
        event_evidence = event.evidence_refs[0]
        world_commit = WorldCommitReceipt(
            world_id="world_watering_0001",
            previous_revision=5,
            world_revision=6,
            first_event_sequence=41,
            last_event_sequence=48,
            committed_at=NOW,
            state_hash="f" * 64,
        )
        world_evidence = replace(
            make_evidence(
                "evidence_world_commit_0001",
                EvidenceType.WORLD_COMMIT,
            ),
            sha256=world_commit_receipt_sha256(world_commit),
        )
        successful_run = RunResultSnapshot(
            run_id=event.run_id,
            session_id=event.session_id,
            turn_id=event.turn_id,
            command_id=event.command_id,
            world_id="world_watering_0001",
            skill_ref=make_skill_ref(),
            task_success=True,
            world_revision_before=5,
            world_revision_after=6,
            world_difference={"watered_plots": 8},
            failed_actions=(),
            failure_key=None,
            evidence_refs=(event_evidence, world_evidence),
            world_commit=world_commit,
            request_context=operation,
        )
        configs = StaticRoleConfigs(
            make_role_config(
                "teaching_agent",
                allowed_events=("run_failed",),
            )
        )

        with self.assertRaises(AgentContextError) as success_error:
            await _context_builder(
                _TeachingReads(operation=operation, run_result=successful_run),
                configs,
            ).build(event, "teaching_agent", operation)
        self.assertEqual(success_error.exception.code, "CONTEXT_FAILURE_KEY_MISMATCH")

        wrong_revision_run = RunResultSnapshot(
            run_id=event.run_id,
            session_id=event.session_id,
            turn_id=event.turn_id,
            command_id=event.command_id,
            world_id="world_watering_0001",
            skill_ref=make_skill_ref(),
            task_success=False,
            world_revision_before=99,
            world_revision_after=99,
            world_difference={"watered_plots": 7},
            failed_actions=({"reason": "short_loop"},),
            failure_key=event.failure_key,
            evidence_refs=(event_evidence,),
            world_commit=None,
            request_context=operation,
        )
        with self.assertRaises(AgentContextError) as revision_error:
            await _context_builder(
                _TeachingReads(operation=operation, run_result=wrong_revision_run),
                configs,
            ).build(event, "teaching_agent", operation)
        self.assertEqual(revision_error.exception.code, "CONTEXT_RUN_IDENTITY_MISMATCH")

    async def test_tool_registry_enforces_role_allowlist_and_closed_input(self) -> None:
        trace = TraceSink()
        handler = _RecordingHandler()
        registry = ToolRegistry(trace)
        registry.register(
            AgentTool(
                name="probe_tool",
                description="Accept one closed integer input.",
                schema_factory=_closed_probe_schema,
                allowed_roles=frozenset({"world_agent"}),
                handler=handler,
            )
        )
        operation = make_operation()
        context = make_context()

        with self.assertRaises(AgentToolAuthorizationError) as unauthorized:
            await registry.execute(
                role="world_agent",
                allowed_names=(),
                model_call_id="call_probe_0001",
                ordinal=1,
                name="probe_tool",
                arguments={"value": 1},
                turn_context=context,
                operation_context=operation,
            )
        self.assertEqual(unauthorized.exception.code, "TOOL_NOT_ALLOWED_BY_ROLE_CONFIG")

        with self.assertRaises(AgentToolInputError) as invalid:
            await registry.execute(
                role="world_agent",
                allowed_names=("probe_tool",),
                model_call_id="call_probe_0002",
                ordinal=1,
                name="probe_tool",
                arguments={"value": 1, "silent_extra": True},
                turn_context=context,
                operation_context=operation,
            )
        self.assertEqual(invalid.exception.code, "TOOL_INPUT_INVALID")
        self.assertEqual(invalid.exception.details["keyword"], "additionalProperties")
        self.assertEqual(handler.calls, 0)
        self.assertEqual(
            [event.name for event in trace.events],
            ["agent.tool.rejected", "agent.tool.rejected"],
        )

    async def test_context_unavailable_tool_is_hidden_and_rejected_without_execution(
        self,
    ) -> None:
        trace = TraceSink()
        handler = _RecordingHandler()
        registry = ToolRegistry(trace)
        registry.register(
            AgentTool(
                name="context_bound_tool",
                description="Only available when one validated context fact exists.",
                schema_factory=_closed_probe_schema,
                allowed_roles=frozenset({"world_agent"}),
                handler=handler,
                is_available=lambda _context: False,
            )
        )
        context = make_context()

        definitions = registry.model_definitions(
            "world_agent",
            ("context_bound_tool",),
            context,
        )
        self.assertEqual(definitions, ())

        with self.assertRaises(AgentToolAuthorizationError) as unavailable:
            await registry.execute(
                role="world_agent",
                allowed_names=("context_bound_tool",),
                model_call_id="call_context_unavailable_0001",
                ordinal=1,
                name="context_bound_tool",
                arguments={"value": 1},
                turn_context=context,
                operation_context=make_operation(),
            )

        self.assertEqual(unavailable.exception.code, "TOOL_UNAVAILABLE_FOR_CONTEXT")
        self.assertEqual(handler.calls, 0)
        self.assertEqual(trace.events[-1].name, "agent.tool.rejected")

    def test_default_teaching_tools_close_no_run_context_in_prompt_and_schema(self) -> None:
        operation = make_operation()
        evidence = make_evidence()
        profile = LearnerProfileSnapshot(
            STUDENT_ID,
            1,
            {
                "for_loop": {
                    "evidence_stage": "OBSERVED",
                    "assistance_level": 1,
                    "evidence_ids": [evidence.evidence_id],
                }
            },
            operation,
            (evidence,),
        )
        directive = TeachingDirective(
            phase=TeachingPhase.REVIEW,
            target_concept="for_loop",
            hint_level=1,
            allowed_response_types=("question", "hint"),
            patch_eligible=False,
            full_solution_eligible=False,
            required_evidence_ids=(evidence.evidence_id,),
            reason_codes=(
                "LEARNER_STAGE_OBSERVED",
                "PATCH_DISABLED_RUNTIME_STAGE",
                "FULL_SOLUTION_DISABLED",
            ),
            pedagogy_policy_version="pedagogy_policy_v1",
            learner_revision=1,
            teaching_spec_version="teaching-1",
        )
        event = make_event("hint_requested")
        context = TurnContext(
            role="teaching_agent",
            event=event,
            task=make_task(operation),
            session=make_session(operation=operation),
            hint_level=1,
            skill=make_skill(operation),
            learner_profile=profile,
            teaching_directive=directive,
        )
        trace = TraceSink()
        registry = build_default_tool_registry(
            trace,
            InMemoryWateringInvocations(operation, make_skill(operation), make_world_state()),
        )
        config = PackagedRoleConfigProvider.load().get("teaching_agent")

        definitions = registry.model_definitions(
            "teaching_agent",
            config.allowed_tools,
            context,
        )
        definition_names = {definition["name"] for definition in definitions}
        self.assertNotIn("get_current_run", definition_names)
        self.assertIn("get_learner_profile", definition_names)

        prompt = PromptBuilder().initial_messages(config, context, definitions)
        schema = build_model_output_schema(
            definitions,
            max_tool_calls=config.limits.max_tool_calls,
            role="teaching_agent",
            directive=directive,
            required_evidence_aliases=(),
        )
        self.assertNotIn(
            "get_current_run",
            "\n".join(message.content for message in prompt),
        )
        self.assertNotIn("get_current_run", json.dumps(schema, sort_keys=True))

        empty_directive = replace(
            directive,
            required_evidence_ids=(),
            reason_codes=(
                "LEARNER_CONCEPT_UNOBSERVED",
                "PATCH_DISABLED_RUNTIME_STAGE",
                "FULL_SOLUTION_DISABLED",
            ),
        )
        empty_context = replace(context, teaching_directive=empty_directive)
        empty_definitions = registry.model_definitions(
            "teaching_agent",
            config.allowed_tools,
            empty_context,
        )
        empty_prompt = PromptBuilder().initial_messages(
            config,
            empty_context,
            empty_definitions,
        )
        empty_system = empty_prompt[0].content
        self.assertIn("available_tools 是本轮完整且穷尽的工具集合", empty_system)
        self.assertIn("This directive has no required Evidence", empty_system)
        self.assertIn("keep learner_inference null", empty_system)

        with self.assertRaises(AgentToolAuthorizationError) as unavailable:
            registry.validate_call(
                role="teaching_agent",
                allowed_names=config.allowed_tools,
                name="get_current_run",
                arguments={},
                turn_context=context,
            )
        self.assertEqual(unavailable.exception.code, "TOOL_UNAVAILABLE_FOR_CONTEXT")

        run_event = make_event("run_failed")
        bound_run = RunResultSnapshot(
            run_id=run_event.run_id,
            session_id=run_event.session_id,
            turn_id=run_event.turn_id,
            command_id=run_event.command_id,
            world_id="world_watering_0001",
            skill_ref=run_event.skill_ref,
            task_success=False,
            world_revision_before=run_event.expected_world_revision,
            world_revision_after=run_event.expected_world_revision,
            world_difference={"watered_plots": 7},
            failed_actions=({"reason": "short_loop"},),
            failure_key=run_event.failure_key,
            evidence_refs=run_event.evidence_refs,
            world_commit=None,
            request_context=operation,
        )
        bound_context = replace(context, event=run_event, run_result=bound_run)
        bound_names = {
            definition["name"]
            for definition in registry.model_definitions(
                "teaching_agent",
                config.allowed_tools,
                bound_context,
            )
        }
        self.assertIn("get_current_run", bound_names)

    def test_directive_schema_binds_hint_fields_to_response_type(self) -> None:
        evidence = make_evidence()
        mixed = TeachingDirective(
            phase=TeachingPhase.REVIEW,
            target_concept="for_loop",
            hint_level=1,
            allowed_response_types=("question", "hint"),
            patch_eligible=False,
            full_solution_eligible=False,
            required_evidence_ids=(evidence.evidence_id,),
            reason_codes=(
                "LEARNER_STAGE_OBSERVED",
                "PATCH_DISABLED_RUNTIME_STAGE",
                "FULL_SOLUTION_DISABLED",
            ),
            pedagogy_policy_version="pedagogy_policy_v1",
            learner_revision=1,
            teaching_spec_version="teaching-1",
        )
        common = {
            "message": "One bounded evidence-based response.",
            "learner_inference": None,
            "skill_patch": None,
            "requires_student_confirmation": False,
        }
        mixed_schema = build_model_output_schema(
            (),
            max_tool_calls=0,
            role="teaching_agent",
            directive=mixed,
        )
        mixed_validator = Draft202012Validator(mixed_schema)
        question_decision = {
            **common,
            "role": "teaching_agent",
            "response_type": "question",
            "question": "Which loop bound omits the final item?",
            "hint_level": None,
        }
        question = {
            "kind": "decision",
            "decision": question_decision,
            "tool_calls": [],
        }
        hint_decision = {
            **common,
            "role": "teaching_agent",
            "response_type": "hint",
            "question": None,
            "hint_level": 1,
        }
        hint = {
            "kind": "decision",
            "decision": hint_decision,
            "tool_calls": [],
        }
        self.assertTrue(mixed_validator.is_valid(question))
        self.assertTrue(mixed_validator.is_valid(hint))
        self.assertFalse(
            mixed_validator.is_valid(
                {
                    **question,
                    "decision": {**question_decision, "hint_level": 1},
                }
            )
        )
        self.assertFalse(
            mixed_validator.is_valid(
                {
                    **hint,
                    "decision": {**hint_decision, "hint_level": None},
                }
            )
        )

        book = replace(
            mixed,
            phase=TeachingPhase.SUMMARIZATION,
            hint_level=0,
            allowed_response_types=("growth_summary",),
            reason_codes=(
                "TASK_COMPLETED_WITH_SUCCESS_EVIDENCE",
                "PATCH_DISABLED_RUNTIME_STAGE",
                "FULL_SOLUTION_DISABLED",
            ),
        )
        book_schema = build_model_output_schema(
            (),
            max_tool_calls=0,
            role="book_agent",
            directive=book,
        )
        book_validator = Draft202012Validator(book_schema)
        summary_decision = {
            **common,
            "role": "book_agent",
            "response_type": "growth_summary",
            "question": None,
            "hint_level": None,
        }
        summary = {
            "kind": "decision",
            "decision": summary_decision,
            "tool_calls": [],
        }
        self.assertTrue(book_validator.is_valid(summary))
        self.assertFalse(
            book_validator.is_valid(
                {
                    **summary,
                    "decision": {**summary_decision, "hint_level": 0},
                }
            )
        )

    async def test_tool_schema_must_explicitly_close_every_input_object(self) -> None:
        registry = ToolRegistry(TraceSink())
        registry.register(
            AgentTool(
                name="open_tool",
                description="An intentionally unsafe fixture schema.",
                schema_factory=lambda _context: {
                    "type": "object",
                    "required": [],
                    "properties": {},
                },
                allowed_roles=frozenset({"world_agent"}),
                handler=_RecordingHandler(),
            )
        )

        with self.assertRaises(AgentConfigurationError) as raised:
            registry.model_definitions("world_agent", ("open_tool",), make_context())

        self.assertEqual(raised.exception.code, "TOOL_SCHEMA_INVALID")
        self.assertEqual(raised.exception.details["keyword"], "additionalProperties")

    async def test_unexpected_tool_exception_is_converted_and_traced(self) -> None:
        trace = TraceSink()
        handler = _RecordingHandler(RuntimeError("secret implementation failure"))
        registry = ToolRegistry(trace)
        registry.register(
            AgentTool(
                name="probe_tool",
                description="Raise an unexpected implementation exception.",
                schema_factory=_closed_probe_schema,
                allowed_roles=frozenset({"world_agent"}),
                handler=handler,
            )
        )

        with self.assertRaises(AgentToolExecutionError) as raised:
            await registry.execute(
                role="world_agent",
                allowed_names=("probe_tool",),
                model_call_id="call_probe_0003",
                ordinal=1,
                name="probe_tool",
                arguments={"value": 1},
                turn_context=make_context(),
                operation_context=make_operation(),
            )

        self.assertEqual(raised.exception.code, "TOOL_HANDLER_FAILED")
        self.assertEqual(raised.exception.details["exception_type"], "RuntimeError")
        self.assertNotIn("secret implementation failure", str(raised.exception.details))
        self.assertEqual(trace.events[-1].name, "agent.tool.failed")
        self.assertEqual(trace.events[-1].fields["error_code"], "TOOL_HANDLER_FAILED")


if __name__ == "__main__":
    unittest.main()
