from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from agent_runtime_fixtures import (  # noqa: E402
    COMMAND_ID,
    SESSION_ID,
    STUDENT_ID,
    TURN_ID,
    WORLD_ID,
    InMemoryWateringInvocations,
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
    make_session,
    make_skill,
    make_skill_ref,
    make_task,
    make_versions,
    make_world_state,
    tool_calls_output,
)
from yaya_agent_contracts import EvidenceType  # noqa: E402
from yaya_agent_runtime import (  # noqa: E402
    PEDAGOGY_POLICY_VERSION,
    AgentHub,
    AgentPersistenceError,
    AgentTool,
    AgentToolExecutionError,
    CommittedAgentTurn,
    ContextBuilder,
    LearnerProfileSnapshot,
    PackagedRoleConfigProvider,
    PromptBuilder,
    RoleRoute,
    RunResultSnapshot,
    SharedAgentRuntime,
    SkillInvocationRequest,
    TeachingDirective,
    TeachingPhase,
    ToolRegistry,
    ToolResult,
    TurnContext,
    build_default_tool_registry,
    skill_invocation_request_sha256,
)
from yaya_agent_runtime.adapters import (  # noqa: E402
    OpenAICompatibleConfig,
    OpenAICompatibleLlmAdapter,
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


def _call(name: str, arguments: dict[str, object], call_id: str) -> dict[str, object]:
    return {"call_id": call_id, "name": name, "arguments": arguments}


class _CountingHandler:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def __call__(self, arguments, turn_context, execution, operation_context):
        del arguments, turn_context, execution, operation_context
        self.calls += 1
        if self.fail:
            raise TimeoutError("fixture timeout")
        return ToolResult({"ok": True}, {"ok": True})


class _SucceededTraceFailure(TraceSink):
    async def record(self, event, context):
        if event.name == "agent.tool.succeeded":
            raise OSError("trace sink unavailable")
        await super().record(event, context)


class _DriftedRouter:
    """A current policy that would suppress the historical handled route."""

    def __init__(self) -> None:
        self.calls = 0

    def route(self, event):
        self.calls += 1
        return RoleRoute(event.event_type, None, "current policy suppresses this historical turn")


class _DriftedRoleConfigs:
    """Current config deliberately rejects the historical task_started event."""

    def __init__(self) -> None:
        self.calls = 0
        self.config = make_role_config(
            "world_agent",
            allowed_events=("compile_failed",),
            allowed_tools=("probe_tool",),
        )

    def get(self, role):
        del role
        self.calls += 1
        return self.config


class _NeverReadPorts:
    def __init__(self) -> None:
        self.calls = 0

    def __getattr__(self, name):
        async def unexpected(*args, **kwargs):
            del args, kwargs
            self.calls += 1
            raise AssertionError(f"replay unexpectedly read context through {name}")

        return unexpected


class _ReplayOnlyStore:
    def __init__(self, record: CommittedAgentTurn) -> None:
        self.record = record
        self.lookup_calls = 0
        self.commit_calls = 0

    async def get_committed(self, event, context):
        del event, context
        self.lookup_calls += 1
        return self.record

    async def commit(self, event, route, decision, context):
        del event, route, decision, context
        self.commit_calls += 1
        raise AssertionError("committed replay must never attempt another commit")


class _FailingProviderTransport:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def post_json(self, url, headers, body, timeout_ms):
        del url, headers, body, timeout_ms
        self.calls += 1
        raise self.error


def _runtime(llm, configs, tools, trace) -> SharedAgentRuntime:
    from agent_runtime_fixtures import NOW

    return SharedAgentRuntime(
        llm=llm,
        role_configs=configs,
        tools=tools,
        prompts=PromptBuilder(),
        trace=trace,
        versions=make_versions(),
        clock=lambda: NOW,
    )


def _skill_patch_output() -> dict[str, object]:
    return {
        "kind": "decision",
        "decision": {
            "role": "teaching_agent",
            "response_type": "skill_patch",
            "message": "Replace the loop body with this patch.",
            "question": None,
            "hint_level": 4,
            "learner_inference": None,
            "skill_patch": {
                "path": "main.py",
                "old_text": "water(index)",
                "new_text": "water(index + 1)",
                "explanation": "An intentionally forbidden legacy patch proposal.",
            },
            "requires_student_confirmation": True,
        },
        "tool_calls": [],
    }


def _invocation_request(
    operation,
    *,
    length: int,
    invocation_id: str = "toolexec_idempotency_0001",
) -> SkillInvocationRequest:
    skill_ref = make_skill_ref()
    arguments = {"length": length}
    identity = {
        "tenant_id": operation.actor.tenant_id,
        "invocation_id": invocation_id,
        "session_id": SESSION_ID,
        "turn_id": TURN_ID,
        "command_id": COMMAND_ID,
        "world_id": WORLD_ID,
        "expected_world_revision": 5,
        "skill_ref": skill_ref,
        "arguments": arguments,
    }
    return SkillInvocationRequest(
        **identity,
        request_sha256=skill_invocation_request_sha256(**identity),
    )


class AgentRuntimeRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_committed_replay_bypasses_drifted_router_config_context_and_runtime(
        self,
    ) -> None:
        operation = make_operation()
        event = make_event("task_started")
        historical_route = RoleRoute(
            event.event_type,
            "world_agent",
            "historical policy handled this turn",
        )
        historical_decision = make_agent_decision("Historical canonical response.")
        record = CommittedAgentTurn(
            event,
            operation.actor,
            operation.content_ref,
            historical_route,
            historical_decision,
        )
        store = _ReplayOnlyStore(record)
        router = _DriftedRouter()
        current_configs = _DriftedRoleConfigs()
        reads = _NeverReadPorts()
        contexts = ContextBuilder(
            tasks=reads,
            sessions=reads,
            skills=reads,
            runs=reads,
            counterexamples=reads,
            learners=reads,
            messages=reads,
            worlds=reads,
            role_configs=current_configs,
        )
        trace = TraceSink()
        tool_handler = _CountingHandler()
        tools = ToolRegistry(trace)
        tools.register(
            AgentTool(
                "probe_tool",
                "Current-policy tool that a replay must not call.",
                _empty_schema,
                frozenset({"world_agent"}),
                tool_handler,
            )
        )
        llm = SequenceLlm([])
        runtime = _runtime(llm, current_configs, tools, trace)
        hub = AgentHub(router=router, contexts=contexts, runtime=runtime, turns=store)

        result = await hub.handle(event, operation)

        self.assertTrue(result.persisted)
        self.assertTrue(result.replayed)
        self.assertIs(result.route, historical_route)
        self.assertIs(result.decision, historical_decision)
        self.assertEqual(store.lookup_calls, 1)
        self.assertEqual(store.commit_calls, 0)
        self.assertEqual(router.calls, 0)
        self.assertEqual(current_configs.calls, 0)
        self.assertEqual(reads.calls, 0)
        self.assertEqual(len(llm.requests), 0)
        self.assertEqual(tool_handler.calls, 0)
        self.assertEqual(trace.events, [])

    async def test_committed_replay_rejects_cross_tenant_and_content_authority(self) -> None:
        original_operation = make_operation()
        event = make_event("task_started")
        historical_route = RoleRoute(
            event.event_type,
            "world_agent",
            "historical canonical route",
        )
        record = CommittedAgentTurn(
            event,
            original_operation.actor,
            original_operation.content_ref,
            historical_route,
            make_agent_decision("Tenant- and content-scoped response."),
        )
        cross_tenant_operation = replace(
            original_operation,
            actor=replace(original_operation.actor, tenant_id="tenant_other"),
        )
        different_content_operation = replace(
            original_operation,
            content_ref=replace(
                original_operation.content_ref,
                version="1.0.1",
                content_hash="c" * 64,
            ),
        )
        cases = (
            (
                cross_tenant_operation,
                "AGENT_TURN_REPLAY_AUTHORITY_MISMATCH",
            ),
            (
                different_content_operation,
                "AGENT_TURN_REPLAY_CONTENT_MISMATCH",
            ),
        )

        for current_operation, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                store = _ReplayOnlyStore(record)
                router = _DriftedRouter()
                side_effects = _NeverReadPorts()
                hub = AgentHub(
                    router=router,
                    contexts=side_effects,
                    runtime=side_effects,
                    turns=store,
                )

                with self.assertRaises(AgentPersistenceError) as raised:
                    await hub.handle(event, current_operation)

                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(store.lookup_calls, 1)
                self.assertEqual(store.commit_calls, 0)
                self.assertEqual(router.calls, 0)
                self.assertEqual(side_effects.calls, 0)

    async def test_invocation_port_replays_equal_hash_and_rejects_hash_conflict(self) -> None:
        operation = make_operation()
        skill = make_skill(operation)
        application = InMemoryWateringInvocations(operation, skill, make_world_state())
        original_request = _invocation_request(operation, length=8)

        original = await application.invoke(original_request, operation)
        replay = await application.invoke(original_request, operation)
        conflicting_request = _invocation_request(operation, length=7)
        self.assertNotEqual(
            original_request.request_sha256,
            conflicting_request.request_sha256,
        )

        with self.assertRaises(AgentToolExecutionError) as raised:
            await application.invoke(conflicting_request, operation)

        self.assertEqual(raised.exception.code, "TOOL_IDEMPOTENCY_KEY_REUSED")
        self.assertIs(replay, original)
        self.assertEqual(application.call_count, 3)
        self.assertEqual(application.execution_count, 1)
        self.assertEqual(application.revision, 6)
        canonical = await application.get_snapshot(WORLD_ID, operation)
        self.assertEqual(canonical.value.revision, 6)
        plots = canonical.value.state["plots"]
        self.assertIsInstance(plots, tuple)
        assert isinstance(plots, tuple)
        self.assertEqual(sum(plot["hydration"] == 100 for plot in plots), 8)

    async def test_teaching_legacy_patch_is_repaired_once_then_falls_back(self) -> None:
        operation = make_operation()
        skill = make_skill(operation)
        event = make_event("hint_requested", failure_count=0)
        context = TurnContext(
            role="teaching_agent",
            event=event,
            task=make_task(operation),
            session=make_session(operation=operation),
            hint_level=1,
            skill=skill,
            learner_profile=LearnerProfileSnapshot(STUDENT_ID, 0, {}, operation),
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
                teaching_spec_version="teaching-1",
            ),
        )
        configs = PackagedRoleConfigProvider.load()
        teaching_config = configs.get("teaching_agent")
        self.assertTrue(teaching_config.limits.allow_skill_patch)
        self.assertTrue(teaching_config.limits.require_confirmation_for_patch)
        self.assertNotIn("propose_skill_patch", teaching_config.allowed_tools)
        trace = TraceSink()
        application = InMemoryWateringInvocations(operation, skill, make_world_state())
        tools = build_default_tool_registry(trace, application)
        invalid_patch = make_reply(_skill_patch_output())
        llm = SequenceLlm([invalid_patch, invalid_patch])
        runtime = _runtime(llm, configs, tools, trace)

        decision = await runtime.run("teaching_agent", context, operation)

        self.assertTrue(decision.degraded)
        self.assertEqual(decision.source, "provider_fallback")
        self.assertEqual(decision.fallback_reason, "MODEL_OUTPUT_INVALID")
        self.assertEqual(decision.response_type, "question")
        self.assertIsNone(decision.draft.skill_patch)
        self.assertEqual(decision.tool_calls, ())
        self.assertEqual(application.call_count, 0)
        self.assertEqual(len(llm.requests), 2)
        invalid_events = [event for event in trace.events if event.name == "agent.output.invalid"]
        self.assertEqual(len(invalid_events), 1)
        self.assertEqual(
            invalid_events[0].fields["error_code"],
            "RESPONSE_TYPE_DIRECTIVE_MISMATCH",
        )

    async def test_provider_unavailable_and_timeout_become_explicit_runtime_fallbacks(self) -> None:
        for provider_error in (
            ConnectionError("provider unavailable"),
            TimeoutError("provider deadline exceeded"),
        ):
            with self.subTest(provider_error=type(provider_error).__name__):
                transport = _FailingProviderTransport(provider_error)
                adapter = OpenAICompatibleLlmAdapter(
                    OpenAICompatibleConfig(
                        endpoint="https://provider.example/v1/chat/completions",
                        api_key="runtime-test-key",
                        model="fixture-model",
                        provider="fixture-provider",
                        response_format="json_schema",
                    ),
                    transport,
                )
                trace = TraceSink()
                runtime = _runtime(
                    adapter,
                    StaticRoleConfigs(make_role_config("world_agent")),
                    ToolRegistry(trace),
                    trace,
                )

                decision = await runtime.run("world_agent", make_context(), make_operation())

                self.assertTrue(decision.degraded)
                self.assertEqual(decision.source, "provider_fallback")
                self.assertEqual(decision.fallback_reason, "DEPENDENCY_UNAVAILABLE")
                self.assertEqual(decision.provider, "runtime")
                self.assertEqual(transport.calls, 1)
                self.assertEqual(
                    [event.name for event in trace.events],
                    ["agent.turn.started", "agent.model.requested", "agent.turn.finished"],
                )
                self.assertTrue(trace.events[-1].fields["fallback"])

    async def test_failed_run_replaces_english_false_success_with_canonical_receipt(self) -> None:
        operation = make_operation()
        skill = make_skill(operation)
        application = InMemoryWateringInvocations(
            operation,
            skill,
            make_world_state(plot_count=8, hydration=0),
        )
        trace = TraceSink()
        configs = PackagedRoleConfigProvider.load()
        runtime = _runtime(
            SequenceLlm(
                [
                    make_reply(
                        tool_calls_output(
                            "invoke_skill",
                            {"skill_id": "bound_skill", "arguments": {"length": 7}},
                            call_id="call_failed_0001",
                        )
                    ),
                    make_reply(
                        decision_output(
                            "xiaohutao",
                            "The verified run watered 8 of 8 plots and completed successfully.",
                        )
                    ),
                ]
            ),
            configs,
            build_default_tool_registry(trace, application),
            trace,
        )

        decision = await runtime.run("xiaohutao", make_context("xiaohutao"), operation)

        self.assertFalse(decision.degraded)
        self.assertIn("任务尚未完成", decision.message)
        self.assertNotIn("completed successfully", decision.message)
        self.assertEqual(application.revision, 5)
        self.assertEqual(application.execution_count, 1)
        self.assertEqual(decision.tool_calls[0].result_summary["task_success"], False)

    async def test_entire_batch_is_preflighted_before_any_side_effect(self) -> None:
        trace = TraceSink()
        read_handler = _CountingHandler()
        side_effect_handler = _CountingHandler()
        tools = ToolRegistry(trace)
        tools.register(
            AgentTool(
                "probe_tool",
                "Read-only fixture.",
                _empty_schema,
                frozenset({"xiaohutao"}),
                read_handler,
            )
        )
        tools.register(
            AgentTool(
                "invoke_skill",
                "Side-effect fixture.",
                _empty_schema,
                frozenset({"xiaohutao"}),
                side_effect_handler,
            )
        )
        configs = StaticRoleConfigs(
            make_role_config(
                "xiaohutao",
                allowed_tools=("probe_tool", "invoke_skill"),
                max_tool_calls=2,
            )
        )
        invalid_batch = _tool_batch(
            _call("probe_tool", {"extra": True}, "call_probe_0001"),
            _call("invoke_skill", {}, "call_invoke_0001"),
        )
        runtime = _runtime(
            SequenceLlm([make_reply(invalid_batch), make_reply(invalid_batch)]),
            configs,
            tools,
            trace,
        )

        decision = await runtime.run("xiaohutao", make_context("xiaohutao"), make_operation())

        self.assertTrue(decision.degraded)
        self.assertEqual(read_handler.calls, 0)
        self.assertEqual(side_effect_handler.calls, 0)

    async def test_read_failure_occurs_before_last_side_effect(self) -> None:
        trace = TraceSink()
        read_handler = _CountingHandler(fail=True)
        side_effect_handler = _CountingHandler()
        tools = ToolRegistry(trace)
        for name, handler in (
            ("probe_tool", read_handler),
            ("invoke_skill", side_effect_handler),
        ):
            tools.register(
                AgentTool(
                    name,
                    f"{name} fixture.",
                    _empty_schema,
                    frozenset({"xiaohutao"}),
                    handler,
                )
            )
        configs = StaticRoleConfigs(
            make_role_config(
                "xiaohutao",
                allowed_tools=("probe_tool", "invoke_skill"),
                max_tool_calls=2,
            )
        )
        batch = _tool_batch(
            _call("probe_tool", {}, "call_probe_0001"),
            _call("invoke_skill", {}, "call_invoke_0001"),
        )
        runtime = _runtime(SequenceLlm([make_reply(batch)]), configs, tools, trace)

        decision = await runtime.run("xiaohutao", make_context("xiaohutao"), make_operation())

        self.assertTrue(decision.degraded)
        self.assertEqual(decision.fallback_reason, "TOOL_DEPENDENCY_FAILED")
        self.assertEqual(read_handler.calls, 1)
        self.assertEqual(side_effect_handler.calls, 0)

    async def test_side_effect_identity_does_not_depend_on_tool_ordinal(self) -> None:
        operation = make_operation()
        skill = make_skill(operation)
        application = InMemoryWateringInvocations(operation, skill, make_world_state())
        trace = TraceSink()
        tools = build_default_tool_registry(trace, application)
        config = PackagedRoleConfigProvider.load().get("xiaohutao")
        context = make_context("xiaohutao")
        arguments = {"skill_id": "bound_skill", "arguments": {"length": 8}}

        first, _, _ = await tools.execute(
            role="xiaohutao",
            allowed_names=config.allowed_tools,
            model_call_id="call_order_0001",
            ordinal=1,
            name="invoke_skill",
            arguments=arguments,
            turn_context=context,
            operation_context=operation,
        )
        second, _, _ = await tools.execute(
            role="xiaohutao",
            allowed_names=config.allowed_tools,
            model_call_id="call_order_0002",
            ordinal=2,
            name="invoke_skill",
            arguments=arguments,
            turn_context=context,
            operation_context=operation,
        )

        self.assertEqual(first.execution_id, second.execution_id)
        self.assertEqual(application.call_count, 2)
        self.assertEqual(application.execution_count, 1)
        self.assertEqual(application.revision, 6)

    async def test_success_requires_exact_world_commit_receipt(self) -> None:
        with self.assertRaisesRegex(ValueError, "successful world task"):
            RunResultSnapshot(
                run_id="run_fake_success_0001",
                session_id=SESSION_ID,
                turn_id=TURN_ID,
                command_id=COMMAND_ID,
                world_id=WORLD_ID,
                skill_ref=make_skill_ref(),
                task_success=True,
                world_revision_before=5,
                world_revision_after=5,
                world_difference={"watered_plots": 8},
                failed_actions=(),
                failure_key=None,
                evidence_refs=(make_evidence(evidence_type=EvidenceType.ACTION_LOG),),
                world_commit=None,
                request_context=make_operation(),
            )

    async def test_trace_failure_after_world_commit_is_durable_warning(self) -> None:
        operation = make_operation()
        skill = make_skill(operation)
        application = InMemoryWateringInvocations(operation, skill, make_world_state())
        trace = _SucceededTraceFailure()
        configs = PackagedRoleConfigProvider.load()
        runtime = _runtime(
            SequenceLlm(
                [
                    make_reply(
                        tool_calls_output(
                            "invoke_skill",
                            {"skill_id": "bound_skill", "arguments": {"length": 8}},
                            call_id="call_trace_0001",
                        )
                    ),
                    make_reply(decision_output("xiaohutao", "Provider prose is replaced.")),
                ]
            ),
            configs,
            build_default_tool_registry(trace, application),
            trace,
        )

        decision = await runtime.run("xiaohutao", make_context("xiaohutao"), operation)

        self.assertFalse(decision.degraded)
        self.assertIn("TRACE_TOOL_SUCCEEDED_WRITE_FAILED", decision.runtime_warnings)
        self.assertEqual(application.revision, 6)
        self.assertEqual(application.execution_count, 1)

    async def test_provider_prompt_omits_canonical_resource_and_student_ids(self) -> None:
        context = make_context("xiaohutao")
        trace = TraceSink()
        config = PackagedRoleConfigProvider.load().get("xiaohutao")
        tools = build_default_tool_registry(
            trace,
            InMemoryWateringInvocations(make_operation(), make_skill(), make_world_state()),
        )
        definitions = tools.model_definitions("xiaohutao", config.allowed_tools, context)
        messages = PromptBuilder().initial_messages(config, context, definitions)
        serialized = "\n".join(message.content for message in messages)

        for secret_identity in (
            STUDENT_ID,
            SESSION_ID,
            TURN_ID,
            COMMAND_ID,
            WORLD_ID,
            make_skill_ref().skill_id,
            context.event.event_id,
        ):
            self.assertNotIn(secret_identity, serialized)
        self.assertIn("bound_skill", serialized)


if __name__ == "__main__":
    unittest.main()
