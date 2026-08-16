from __future__ import annotations

import asyncio
import sys
import unittest
from collections.abc import Mapping
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
    CommitStore,
    InMemoryWateringInvocations,
    RecordingReads,
    SequenceLlm,
    StaticRoleConfigs,
    TraceSink,
    decision_output,
    make_agent_decision,
    make_context,
    make_event,
    make_evidence,
    make_operation,
    make_reply,
    make_role_config,
    make_skill,
    make_skill_ref,
    make_versions,
    make_world_state,
    tool_calls_output,
)
from yaya_agent_contracts import EvidenceType  # noqa: E402
from yaya_agent_runtime import (  # noqa: E402
    AgentContextError,
    AgentHub,
    AgentPersistenceError,
    AgentTool,
    AgentToolExecutionError,
    ContextBuilder,
    LearnerProfileSnapshot,
    MessageSnapshot,
    PackagedRoleConfigProvider,
    PromptBuilder,
    RoleRouter,
    RunResultSnapshot,
    SharedAgentRuntime,
    ToolRegistry,
    ToolResult,
    build_default_tool_registry,
    skill_invocation_request_sha256,
)


def _empty_schema(_context) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [],
        "properties": {},
    }


def _tool_batch(*calls: dict[str, object]) -> dict[str, object]:
    return {"kind": "tool_calls", "decision": None, "tool_calls": list(calls)}


def _call(name: str, call_id: str) -> dict[str, object]:
    return {"call_id": call_id, "name": name, "arguments": {}}


def _runtime(
    llm,
    configs,
    tools,
    trace,
    *,
    prompts: PromptBuilder | None = None,
) -> SharedAgentRuntime:
    return SharedAgentRuntime(
        llm=llm,
        role_configs=configs,
        tools=tools,
        prompts=prompts or PromptBuilder(),
        trace=trace,
        versions=make_versions(),
        clock=lambda: NOW,
    )


def _context_builder(reads, configs, *, worlds=None) -> ContextBuilder:
    return ContextBuilder(
        tasks=reads,
        sessions=reads,
        skills=reads,
        runs=reads,
        counterexamples=reads,
        learners=reads,
        messages=reads,
        worlds=worlds or reads,
        role_configs=configs,
    )


def _failed_run(event, operation, *evidence, secret: str = "") -> RunResultSnapshot:
    return RunResultSnapshot(
        run_id=event.run_id,
        session_id=event.session_id,
        turn_id=event.turn_id,
        command_id=event.command_id,
        world_id="world_watering_0001",
        skill_ref=make_skill_ref(),
        task_success=False,
        world_revision_before=event.expected_world_revision,
        world_revision_after=event.expected_world_revision,
        world_difference={"watered_plots": 7, "diagnostic": secret},
        failed_actions=({"reason": "short_loop"},),
        failure_key=event.failure_key,
        evidence_refs=evidence,
        world_commit=None,
        request_context=operation,
    )


def _teaching_inference_output() -> dict[str, object]:
    return {
        "kind": "decision",
        "decision": {
            "role": "teaching_agent",
            "response_type": "question",
            "message": "The current run stopped after seven plots.",
            "question": "Which loop bound controls the eighth iteration?",
            "hint_level": None,
            "learner_inference": {
                "concept": "for_loop",
                "score_delta": 0.1,
                "confidence": 0.9,
                "reason": "The current failed run provides direct loop evidence.",
                "evidence_ids": ["evidence_001"],
            },
            "skill_patch": None,
            "requires_student_confirmation": False,
        },
        "tool_calls": [],
    }


class _GatedLlm(SequenceLlm):
    def __init__(self, replies) -> None:
        super().__init__(replies)
        self.first_request_started = asyncio.Event()
        self.release_first_request = asyncio.Event()

    async def generate(self, request, context):
        if not self.requests:
            self.first_request_started.set()
            await self.release_first_request.wait()
        return await super().generate(request, context)


class _CountingRuntime:
    def __init__(self, runtime: SharedAgentRuntime) -> None:
        self.runtime = runtime
        self.calls = 0

    def execution_budget_ms(self, role):
        return self.runtime.execution_budget_ms(role)

    async def run(self, role, context, operation_context):
        self.calls += 1
        return await self.runtime.run(role, context, operation_context)


class _TeachingReads(RecordingReads):
    def __init__(self, *, run_result=None, messages=(), **kwargs) -> None:
        super().__init__(**kwargs)
        self.run_result = run_result
        self.messages = tuple(messages)

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
        return self.messages


class _CapturingPrompts(PromptBuilder):
    def __init__(self) -> None:
        self.contents: list[str] = []

    def initial_messages(self, config, context, tool_definitions):
        messages = super().initial_messages(config, context, tool_definitions)
        self.contents.extend(message.content for message in messages)
        return messages


class _TamperingInvocations:
    def __init__(self, application, field: str) -> None:
        self.application = application
        self.field = field
        self.calls = 0

    async def invoke(self, request, context):
        self.calls += 1
        result = await self.application.invoke(request, context)
        run = result.run
        if self.field == "tenant_id":
            tenant_id = "tenant_other"
            request_sha256 = skill_invocation_request_sha256(
                tenant_id=tenant_id,
                invocation_id=result.invocation_id,
                session_id=run.session_id,
                turn_id=run.turn_id,
                command_id=run.command_id,
                world_id=run.world_id,
                expected_world_revision=run.world_revision_before,
                skill_ref=run.skill_ref,
                arguments=result.arguments,
            )
            return replace(result, tenant_id=tenant_id, request_sha256=request_sha256)
        arguments = {"length": 8}
        request_sha256 = skill_invocation_request_sha256(
            tenant_id=result.tenant_id,
            invocation_id=result.invocation_id,
            session_id=run.session_id,
            turn_id=run.turn_id,
            command_id=run.command_id,
            world_id=run.world_id,
            expected_world_revision=run.world_revision_before,
            skill_ref=run.skill_ref,
            arguments=arguments,
        )
        return replace(result, request_sha256=request_sha256, arguments=arguments)


class _EvidenceHandler:
    def __init__(self, evidence=(), error: Exception | None = None) -> None:
        self.evidence = tuple(evidence)
        self.error = error
        self.calls = 0

    async def __call__(self, arguments, turn_context, execution, operation_context):
        del arguments, turn_context, execution, operation_context
        self.calls += 1
        if self.error is not None:
            raise self.error
        return ToolResult(
            value={"ok": True},
            summary={"ok": True},
            evidence_refs=self.evidence,
        )


class _FailureTraceSink(TraceSink):
    async def record(self, event, context):
        if event.name == "agent.tool.failed":
            raise OSError("trace sink failed while recording tool failure")
        await super().record(event, context)


class _FailOnceTaskReads(RecordingReads):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.failures_remaining = 1

    async def get_task(self, task_id, context):
        if self.failures_remaining:
            self.failures_remaining -= 1
            self.calls.append("get_task")
            raise TimeoutError("transient task read failure")
        return await super().get_task(task_id, context)


class _SelectiveFailureTraceSink(TraceSink):
    def __init__(self, *failed_names: str) -> None:
        super().__init__()
        self.failed_names = frozenset(failed_names)
        self.attempted_names: list[str] = []

    async def record(self, event, context):
        self.attempted_names.append(event.name)
        if event.name in self.failed_names:
            raise OSError(f"trace sink rejected {event.name}")
        await super().record(event, context)


class AgentRuntimeSecondHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_failure_abandons_claim_and_same_event_can_retry(self) -> None:
        operation = make_operation()
        event = make_event("task_started")
        reads = _FailOnceTaskReads(operation=operation)
        configs = StaticRoleConfigs(
            make_role_config(
                "world_agent",
                allowed_events=("task_started",),
                allowed_tools=(),
            )
        )
        trace = TraceSink()
        llm = SequenceLlm([make_reply(decision_output("world_agent", "Retry used a fresh claim."))])
        turns = CommitStore()
        hub = AgentHub(
            router=RoleRouter(),
            contexts=_context_builder(reads, configs),
            runtime=_runtime(llm, configs, ToolRegistry(trace), trace),
            turns=turns,
        )

        with self.assertRaises(AgentContextError) as raised:
            await hub.handle(event, operation)

        self.assertEqual(raised.exception.code, "AGENT_CONTEXT_BUILD_FAILED")
        self.assertEqual(turns.claims, {})
        self.assertEqual(llm.requests, [])

        result = await hub.handle(event, operation)

        self.assertTrue(result.persisted)
        self.assertFalse(result.replayed)
        self.assertEqual(len(llm.requests), 1)
        self.assertEqual(len(turns.commits), 1)
        self.assertEqual(turns.claims, {})
        self.assertEqual(reads.calls.count("get_task"), 2)

    async def test_expired_lease_can_be_taken_over_with_new_fencing_token(self) -> None:
        operation = make_operation()
        event = make_event("task_started")
        current_time = [NOW]
        turns = CommitStore(clock=lambda: current_time[0], lease_seconds=5)

        first = await turns.claim(event, operation)
        self.assertIsNotNone(first.claim_id)
        self.assertEqual(first.claim_expires_at, NOW + timedelta(seconds=5))

        current_time[0] = NOW + timedelta(seconds=5)
        takeover = await turns.claim(event, operation)

        self.assertIsNotNone(takeover.claim_id)
        self.assertNotEqual(takeover.claim_id, first.claim_id)
        self.assertEqual(
            takeover.claim_expires_at,
            NOW + timedelta(seconds=10),
        )
        assert takeover.claim_id is not None
        receipt = await turns.commit(
            event,
            RoleRouter().route(event),
            make_agent_decision("Fresh fencing token committed."),
            takeover.claim_id,
            operation,
        )
        self.assertTrue(receipt.created)
        self.assertEqual(turns.claims, {})

    async def test_stale_fencing_token_cannot_commit_or_abandon_after_takeover(self) -> None:
        operation = make_operation()
        event = make_event("task_started")
        current_time = [NOW]
        turns = CommitStore(clock=lambda: current_time[0], lease_seconds=5)
        first = await turns.claim(event, operation)
        assert first.claim_id is not None
        current_time[0] = NOW + timedelta(seconds=6)
        takeover = await turns.claim(event, operation)
        assert takeover.claim_id is not None

        with self.assertRaises(AgentPersistenceError) as commit_error:
            await turns.commit(
                event,
                RoleRouter().route(event),
                make_agent_decision("A stale worker must never publish."),
                first.claim_id,
                operation,
            )
        self.assertEqual(commit_error.exception.code, "AGENT_TURN_CLAIM_LOST")

        with self.assertRaises(AgentPersistenceError) as abandon_error:
            await turns.abandon(event, first.claim_id, operation)
        self.assertEqual(abandon_error.exception.code, "AGENT_TURN_CLAIM_LOST")

        key = turns._key(event, operation)
        self.assertEqual(turns.claims[key][0], takeover.claim_id)
        receipt = await turns.commit(
            event,
            RoleRouter().route(event),
            make_agent_decision("Only the takeover worker may publish."),
            takeover.claim_id,
            operation,
        )
        self.assertTrue(receipt.created)

    async def test_started_and_rejected_trace_failures_become_durable_warnings(self) -> None:
        operation = make_operation()
        event = make_event("task_started")
        configs = StaticRoleConfigs(
            make_role_config(
                "world_agent",
                allowed_events=("task_started",),
                allowed_tools=("probe_tool",),
            )
        )
        success_handler = _EvidenceHandler()
        started_trace = _SelectiveFailureTraceSink("agent.tool.started")
        success_tools = ToolRegistry(started_trace)
        success_tools.register(
            AgentTool(
                "probe_tool",
                "Return a successful read-only probe.",
                _empty_schema,
                frozenset({"world_agent"}),
                success_handler,
            )
        )
        llm = SequenceLlm(
            [
                make_reply(tool_calls_output("probe_tool", {}, call_id="call_started_trace_0001")),
                make_reply(decision_output("world_agent", "The probe completed successfully.")),
            ]
        )
        turns = CommitStore()
        hub = AgentHub(
            router=RoleRouter(),
            contexts=_context_builder(RecordingReads(operation=operation), configs),
            runtime=_runtime(llm, configs, success_tools, started_trace),
            turns=turns,
        )

        result = await hub.handle(event, operation)

        self.assertTrue(result.persisted)
        self.assertIsNotNone(result.decision)
        assert result.decision is not None
        self.assertFalse(result.decision.degraded)
        self.assertIn(
            "TRACE_TOOL_STARTED_WRITE_FAILED",
            result.decision.runtime_warnings,
        )
        stored = next(iter(turns.records.values()))
        self.assertEqual(stored.decision, result.decision)
        self.assertIn(
            "TRACE_TOOL_STARTED_WRITE_FAILED",
            stored.decision.runtime_warnings,
        )
        self.assertEqual(success_handler.calls, 1)
        self.assertIn("agent.tool.started", started_trace.attempted_names)

        rejected_handler = _EvidenceHandler()
        rejected_trace = _SelectiveFailureTraceSink("agent.tool.rejected")
        rejected_tools = ToolRegistry(rejected_trace)
        rejected_tools.register(
            AgentTool(
                "probe_tool",
                "This handler must not run after authorization rejection.",
                _empty_schema,
                frozenset({"world_agent"}),
                rejected_handler,
            )
        )
        with self.assertRaises(AgentToolExecutionError) as rejected:
            await rejected_tools.execute(
                role="world_agent",
                allowed_names=(),
                model_call_id="call_rejected_trace_0001",
                ordinal=1,
                name="probe_tool",
                arguments={},
                turn_context=make_context(),
                operation_context=operation,
            )

        self.assertEqual(
            rejected.exception.code,
            "TOOL_NOT_ALLOWED_BY_ROLE_CONFIG",
        )
        self.assertIn(
            "TRACE_TOOL_REJECTED_WRITE_FAILED",
            rejected.exception.details["runtime_warnings"],
        )
        self.assertEqual(rejected_handler.calls, 0)
        self.assertIn("agent.tool.rejected", rejected_trace.attempted_names)

    async def test_two_hubs_single_flight_one_runtime_and_one_world_effect(self) -> None:
        operation = make_operation()
        event = make_event("run_skill_requested")
        state = make_world_state()
        skill = make_skill(operation)
        reads = RecordingReads(operation=operation, skill=skill)
        application = InMemoryWateringInvocations(operation, skill, state)
        configs = PackagedRoleConfigProvider.load()
        contexts = _context_builder(reads, configs, worlds=application)
        trace = TraceSink()
        llm = _GatedLlm(
            [
                make_reply(
                    tool_calls_output(
                        "invoke_skill",
                        {"skill_id": "bound_skill", "arguments": {"length": 8}},
                        call_id="call_concurrent_0001",
                    )
                ),
                make_reply(decision_output("xiaohutao", "Verified canonical success.")),
            ]
        )
        runtime = _CountingRuntime(
            _runtime(
                llm,
                configs,
                build_default_tool_registry(trace, application),
                trace,
            )
        )
        turns = CommitStore()
        first_hub = AgentHub(
            router=RoleRouter(),
            contexts=contexts,
            runtime=runtime,
            turns=turns,
            invocations=application,
        )
        second_hub = AgentHub(
            router=RoleRouter(),
            contexts=contexts,
            runtime=runtime,
            turns=turns,
            invocations=application,
        )

        first = asyncio.create_task(first_hub.handle(event, operation))
        await asyncio.wait_for(llm.first_request_started.wait(), timeout=1)
        try:
            with self.assertRaises(AgentPersistenceError) as raised:
                await second_hub.handle(event, operation)
            self.assertEqual(raised.exception.code, "AGENT_TURN_IN_PROGRESS")
            self.assertEqual(runtime.calls, 1)
            self.assertEqual(application.execution_count, 0)
        finally:
            llm.release_first_request.set()
        canonical = await first

        self.assertTrue(canonical.persisted)
        self.assertFalse(canonical.replayed)
        self.assertIsNotNone(canonical.decision)
        assert canonical.decision is not None
        self.assertFalse(canonical.decision.degraded)
        self.assertEqual(runtime.calls, 1)
        self.assertEqual(application.call_count, 1)
        self.assertEqual(application.execution_count, 1)
        self.assertEqual(application.revision, 6)
        self.assertEqual(len(turns.commits), 1)
        replay = await second_hub.handle(event, operation)
        self.assertTrue(replay.replayed)
        self.assertIs(replay.decision, canonical.decision)
        self.assertEqual(runtime.calls, 1)
        self.assertEqual(application.execution_count, 1)
        self.assertEqual(len(llm.requests), 2)

    async def test_cross_tenant_message_provenance_never_reaches_prompt(self) -> None:
        operation = make_operation()
        foreign_operation = replace(
            operation,
            actor=replace(operation.actor, tenant_id="tenant_other"),
        )
        event = make_event("hint_requested")
        secret = "CROSS_TENANT_MESSAGE_SECRET"
        message = MessageSnapshot(
            "message_recent_0001",
            event.session_id,
            "teaching_agent",
            secret,
            foreign_operation,
        )
        reads = _TeachingReads(operation=operation, messages=(message,))
        configs = StaticRoleConfigs(
            make_role_config(
                "teaching_agent",
                allowed_events=("hint_requested",),
                allowed_tools=(),
            )
        )
        prompts = _CapturingPrompts()
        llm = SequenceLlm([])
        trace = TraceSink()
        hub = AgentHub(
            router=RoleRouter(),
            contexts=_context_builder(reads, configs),
            runtime=_runtime(
                llm,
                configs,
                ToolRegistry(trace),
                trace,
                prompts=prompts,
            ),
            turns=CommitStore(),
        )

        with self.assertRaises(AgentContextError) as raised:
            await hub.handle(event, operation)

        self.assertEqual(raised.exception.code, "CONTEXT_ACTOR_MISMATCH")
        self.assertEqual(llm.requests, [])
        self.assertEqual(prompts.contents, [])
        self.assertNotIn(secret, "".join(prompts.contents))

    async def test_cross_content_run_provenance_never_reaches_prompt(self) -> None:
        operation = make_operation()
        foreign_operation = replace(
            operation,
            content_ref=replace(
                operation.content_ref,
                version="1.0.1",
                content_hash="c" * 64,
            ),
        )
        event = make_event("run_failed")
        secret = "CROSS_CONTENT_RUN_SECRET"
        run = _failed_run(
            event,
            foreign_operation,
            *event.evidence_refs,
            secret=secret,
        )
        reads = _TeachingReads(operation=operation, run_result=run)
        configs = StaticRoleConfigs(
            make_role_config(
                "teaching_agent",
                allowed_events=("run_failed",),
                allowed_tools=(),
            )
        )
        prompts = _CapturingPrompts()
        llm = SequenceLlm([])
        trace = TraceSink()
        hub = AgentHub(
            router=RoleRouter(),
            contexts=_context_builder(reads, configs),
            runtime=_runtime(
                llm,
                configs,
                ToolRegistry(trace),
                trace,
                prompts=prompts,
            ),
            turns=CommitStore(),
        )

        with self.assertRaises(AgentContextError) as raised:
            await hub.handle(event, operation)

        self.assertEqual(run.run_id, event.run_id)
        self.assertEqual(raised.exception.code, "CONTEXT_CONTENT_MISMATCH")
        self.assertEqual(llm.requests, [])
        self.assertEqual(prompts.contents, [])
        self.assertNotIn(secret, "".join(prompts.contents))

    async def test_invocation_result_must_echo_tenant_and_request_hash(self) -> None:
        operation = make_operation()
        context = make_context("xiaohutao")
        config = PackagedRoleConfigProvider.load().get("xiaohutao")
        cases = (
            ("tenant_id", "TOOL_INVOCATION_TENANT_MISMATCH"),
            ("request_sha256", "TOOL_INVOCATION_REQUEST_MISMATCH"),
        )

        for field, expected_code in cases:
            with self.subTest(field=field):
                application = InMemoryWateringInvocations(
                    operation,
                    make_skill(operation),
                    make_world_state(),
                )
                tampering = _TamperingInvocations(application, field)
                trace = TraceSink()
                tools = build_default_tool_registry(trace, tampering)

                with self.assertRaises(AgentToolExecutionError) as raised:
                    await tools.execute(
                        role="xiaohutao",
                        allowed_names=config.allowed_tools,
                        model_call_id=f"call_echo_{field}_0001",
                        ordinal=1,
                        name="invoke_skill",
                        arguments={
                            "skill_id": "bound_skill",
                            "arguments": {"length": 7},
                        },
                        turn_context=context,
                        operation_context=operation,
                    )

                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(tampering.calls, 1)
                self.assertEqual(application.execution_count, 1)
                self.assertEqual(application.revision, 5)
                self.assertEqual(trace.events[-1].name, "agent.tool.failed")

    def test_invoke_schema_omits_internal_certification_metadata(self) -> None:
        operation = make_operation()
        context = make_context("xiaohutao")
        if context.skill is None:
            self.fail("xiaohutao fixture must bind a Skill")
        parameter_schema = dict(context.skill.parameter_schema)
        parameter_schema["x-yaya-certification"] = {
            "semantic_version": "1.0.0",
            "capabilities": ["world.read"],
        }
        skill = replace(context.skill, parameter_schema=parameter_schema)
        context = replace(context, skill=skill, available_skills=(skill,))
        trace = TraceSink()
        tools = build_default_tool_registry(
            trace,
            InMemoryWateringInvocations(operation, skill, make_world_state()),
        )
        definitions = tools.model_definitions(
            "xiaohutao",
            PackagedRoleConfigProvider.load().get("xiaohutao").allowed_tools,
            context,
        )
        invoke = next(item for item in definitions if item["name"] == "invoke_skill")
        input_schema = invoke["input_schema"]
        self.assertIsInstance(input_schema, Mapping)
        assert isinstance(input_schema, Mapping)
        properties = input_schema["properties"]
        self.assertIsInstance(properties, Mapping)
        assert isinstance(properties, Mapping)
        arguments = properties["arguments"]
        self.assertIsInstance(arguments, Mapping)
        assert isinstance(arguments, Mapping)
        self.assertNotIn("x-yaya-certification", arguments)
        self.assertEqual(arguments["required"], ("length",))

    async def test_teaching_inference_uses_directive_evidence_and_keeps_extra_run_evidence(
        self,
    ) -> None:
        operation = make_operation()
        event = make_event("run_failed")
        event_evidence = event.evidence_refs[0]
        extra_evidence = make_evidence(
            "evidence_run_extra_0001",
            EvidenceType.TEST_REPORT,
        )
        run = _failed_run(event, operation, event_evidence, extra_evidence)
        reads = _TeachingReads(operation=operation, run_result=run)
        configs = StaticRoleConfigs(
            make_role_config(
                "teaching_agent",
                allowed_events=("run_failed",),
                allowed_tools=(),
            )
        )
        context = await _context_builder(reads, configs).build(
            event,
            "teaching_agent",
            operation,
        )
        trace = TraceSink()
        runtime = _runtime(
            SequenceLlm([make_reply(_teaching_inference_output())]),
            configs,
            ToolRegistry(trace),
            trace,
        )

        decision = await runtime.run("teaching_agent", context, operation)

        expected_ids = (event_evidence.evidence_id, extra_evidence.evidence_id)
        self.assertFalse(decision.degraded)
        self.assertEqual(
            tuple(evidence.evidence_id for evidence in decision.evidence_refs),
            expected_ids,
        )
        inference = decision.draft.learner_inference
        self.assertIsNotNone(inference)
        assert inference is not None
        self.assertEqual(inference.evidence_ids, (event_evidence.evidence_id,))
        self.assertTrue(
            set(inference.evidence_ids)
            <= {evidence.evidence_id for evidence in decision.evidence_refs}
        )

    async def test_decision_completion_covers_future_tool_evidence_clock_skew(self) -> None:
        future_evidence = replace(
            make_evidence(
                "evidence_future_database_clock_0001",
                EvidenceType.TEST_REPORT,
            ),
            created_at=NOW + timedelta(minutes=5),
        )
        handler = _EvidenceHandler((future_evidence,))
        trace = TraceSink()
        tools = ToolRegistry(trace)
        tools.register(
            AgentTool(
                "evidence_reader",
                "Return Evidence authored by an authoritative database clock.",
                _empty_schema,
                frozenset({"world_agent"}),
                handler,
            )
        )
        configs = StaticRoleConfigs(
            make_role_config(
                "world_agent",
                allowed_tools=("evidence_reader",),
                max_tool_calls=1,
            )
        )
        runtime = _runtime(
            SequenceLlm(
                [
                    make_reply(
                        tool_calls_output(
                            "evidence_reader",
                            {},
                            call_id="call_future_database_clock_0001",
                        )
                    ),
                    make_reply(decision_output("world_agent")),
                ]
            ),
            configs,
            tools,
            trace,
        )

        decision = await runtime.run("world_agent", make_context(), make_operation())

        self.assertEqual(decision.evidence_refs, (future_evidence,))
        self.assertEqual(decision.completed_at, future_evidence.created_at)

    async def test_read_evidence_budget_rejects_before_invoke_side_effect(self) -> None:
        evidence = tuple(
            make_evidence(
                f"evidence_budget_{index:04d}",
                EvidenceType.TEST_REPORT,
            )
            for index in range(64)
        )
        read_handler = _EvidenceHandler(evidence)
        side_effect_handler = _EvidenceHandler()
        trace = TraceSink()
        tools = ToolRegistry(trace)
        for name, handler in (
            ("evidence_reader", read_handler),
            ("invoke_skill", side_effect_handler),
        ):
            tools.register(
                AgentTool(
                    name,
                    f"{name} hardening fixture.",
                    _empty_schema,
                    frozenset({"xiaohutao"}),
                    handler,
                )
            )
        configs = StaticRoleConfigs(
            make_role_config(
                "xiaohutao",
                allowed_tools=("evidence_reader", "invoke_skill"),
                max_tool_calls=2,
            )
        )
        batch = _tool_batch(
            _call("evidence_reader", "call_evidence_0001"),
            _call("invoke_skill", "call_invoke_0001"),
        )
        llm = SequenceLlm([make_reply(batch)])
        runtime = _runtime(llm, configs, tools, trace)

        decision = await runtime.run("xiaohutao", make_context("xiaohutao"), make_operation())

        self.assertTrue(decision.degraded)
        self.assertEqual(decision.fallback_reason, "SIDE_EFFECT_EVIDENCE_BUDGET_UNSAFE")
        self.assertEqual(read_handler.calls, 1)
        self.assertEqual(side_effect_handler.calls, 0)
        self.assertEqual(len(decision.tool_calls), 1)
        self.assertEqual(decision.tool_calls[0].name, "evidence_reader")
        self.assertEqual(len(decision.evidence_refs), 64)
        self.assertEqual(len(llm.requests), 1)

    async def test_tool_failure_trace_failure_preserves_original_fallback_and_warning(self) -> None:
        trace = _FailureTraceSink()
        handler = _EvidenceHandler(error=TimeoutError("read dependency timed out"))
        tools = ToolRegistry(trace)
        tools.register(
            AgentTool(
                "probe_tool",
                "Fail while the failure trace sink also fails.",
                _empty_schema,
                frozenset({"world_agent"}),
                handler,
            )
        )
        configs = StaticRoleConfigs(
            make_role_config(
                "world_agent",
                allowed_tools=("probe_tool",),
                max_tool_calls=1,
            )
        )
        llm = SequenceLlm(
            [make_reply(tool_calls_output("probe_tool", {}, call_id="call_failure_0001"))]
        )
        runtime = _runtime(llm, configs, tools, trace)

        decision = await runtime.run("world_agent", make_context(), make_operation())

        self.assertTrue(decision.degraded)
        self.assertEqual(decision.fallback_reason, "TOOL_DEPENDENCY_FAILED")
        self.assertIn("TRACE_TOOL_FAILED_WRITE_FAILED", decision.runtime_warnings)
        self.assertEqual(handler.calls, 1)
        self.assertEqual(decision.tool_calls, ())
        self.assertEqual(len(llm.requests), 1)
        self.assertEqual(trace.events[-1].name, "agent.turn.finished")


if __name__ == "__main__":
    unittest.main()
