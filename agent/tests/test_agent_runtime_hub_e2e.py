from __future__ import annotations

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
    CommitStore,
    InMemoryWateringInvocations,
    RecordingReads,
    SequenceLlm,
    TraceSink,
    decision_output,
    make_agent_decision,
    make_context,
    make_event,
    make_operation,
    make_reply,
    make_skill,
    make_versions,
    make_world_snapshot,
    make_world_state,
    tool_calls_output,
)
from yaya_agent_runtime import (  # noqa: E402
    AgentHub,
    AgentPersistenceError,
    AgentTurnClaimReceipt,
    AgentTurnCommitReceipt,
    CommittedAgentTurn,
    ContextBuilder,
    PackagedRoleConfigProvider,
    PromptBuilder,
    RoleRouter,
    SharedAgentRuntime,
    build_default_tool_registry,
)


class _ExplodingDependency:
    def __init__(self) -> None:
        self.calls = 0

    async def build(self, *args):
        self.calls += 1
        raise AssertionError("no-action route must not build context")

    async def get_committed(self, *args):
        self.calls += 1
        return None

    async def run(self, *args):
        self.calls += 1
        raise AssertionError("no-action route must not run a model")

    async def commit(self, *args):
        self.calls += 1
        raise AssertionError("no-action route must not persist a decision")


class _StubContexts:
    def __init__(self) -> None:
        self.calls = 0

    async def build(self, event, role, context):
        del event, role, context
        self.calls += 1
        return make_context()


class _StubRuntime:
    def __init__(self) -> None:
        self.decision = make_agent_decision()
        self.calls = 0

    async def run(self, role, context, operation_context):
        del role, context, operation_context
        self.calls += 1
        return self.decision


class _SequenceRuntime:
    def __init__(self, *decisions) -> None:
        self._decisions = list(decisions)
        self.calls = 0

    async def run(self, role, context, operation_context):
        del role, context, operation_context
        self.calls += 1
        return self._decisions.pop(0)

    def execution_budget_ms(self, role):
        del role
        return 1_000


class _StubSkillContexts:
    async def build(self, event, role, context):
        del event, role, context
        return make_context("xiaohutao")


class _NoReceiptInvocations:
    def __init__(self) -> None:
        self.lookups = 0

    async def get_result(self, invocation_id, context):
        del invocation_id, context
        self.lookups += 1
        return None


class _MismatchingStore:
    async def get_committed(self, event, context):
        del event, context
        return None

    async def claim(self, event, context):
        del event, context
        return AgentTurnClaimReceipt(
            "claim_mismatch_0001",
            NOW + timedelta(seconds=30),
            None,
        )

    async def abandon(self, event, claim_id, context):
        del event, claim_id, context

    async def commit(self, event, route, decision, claim_id, context):
        del claim_id
        del decision
        return AgentTurnCommitReceipt(
            CommittedAgentTurn(
                event,
                context.actor,
                context.content_ref,
                route,
                make_agent_decision("A different canonical value."),
            ),
            True,
        )


class _FailingStore:
    async def get_committed(self, event, context):
        del event, context
        return None

    async def claim(self, event, context):
        del event, context
        return AgentTurnClaimReceipt(
            "claim_failure_000001",
            NOW + timedelta(seconds=30),
            None,
        )

    async def abandon(self, event, claim_id, context):
        del event, claim_id, context

    async def commit(self, event, route, decision, claim_id, context):
        del event, route, decision, claim_id, context
        raise RuntimeError("database connection detail")


def _make_context_builder(reads, configs, worlds=None) -> ContextBuilder:
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


class AgentHubAndRealTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_action_route_touches_no_runtime_or_persistence_dependency(self) -> None:
        dependency = _ExplodingDependency()
        hub = AgentHub(
            router=RoleRouter(),
            contexts=dependency,
            runtime=dependency,
            turns=dependency,
        )

        result = await hub.handle(make_event("compile_succeeded"), make_operation())

        self.assertFalse(result.route.should_run)
        self.assertIsNone(result.decision)
        self.assertFalse(result.persisted)
        self.assertEqual(dependency.calls, 1)

    async def test_hub_rejects_a_different_value_from_the_canonical_commit(self) -> None:
        contexts = _StubContexts()
        runtime = _StubRuntime()
        hub = AgentHub(
            router=RoleRouter(),
            contexts=contexts,
            runtime=runtime,
            turns=_MismatchingStore(),
        )

        with self.assertRaises(AgentPersistenceError) as raised:
            await hub.handle(make_event("task_started"), make_operation())

        self.assertEqual(raised.exception.code, "AGENT_TURN_COMMIT_MISMATCH")
        self.assertEqual(contexts.calls, 1)
        self.assertEqual(runtime.calls, 1)

    async def test_hub_maps_unexpected_commit_exception_without_claiming_success(self) -> None:
        hub = AgentHub(
            router=RoleRouter(),
            contexts=_StubContexts(),
            runtime=_StubRuntime(),
            turns=_FailingStore(),
        )

        with self.assertRaises(AgentPersistenceError) as raised:
            await hub.handle(make_event("task_started"), make_operation())

        self.assertEqual(raised.exception.code, "AGENT_TURN_COMMIT_FAILED")
        self.assertEqual(raised.exception.details["exception_type"], "RuntimeError")
        self.assertNotIn("database connection detail", str(raised.exception.details))

    async def test_known_side_effect_rollback_releases_claim_for_same_turn_retry(self) -> None:
        fallback_base = make_agent_decision("The first persistence unit rolled back.")
        rolled_back = replace(
            fallback_base,
            draft=replace(fallback_base.draft, role="xiaohutao"),
            message_key="agent.xiaohutao.message",
            source="provider_fallback",
            degraded=True,
            fallback_reason="TOOL_PERSISTENCE_ROLLED_BACK",
            runtime_warnings=("SIDE_EFFECT_ROLLED_BACK",),
            teaching_directive=None,
        )
        retry_base = make_agent_decision("The retry completed without a durable Run.")
        retry = replace(
            retry_base,
            draft=replace(retry_base.draft, role="xiaohutao"),
            message_key="agent.xiaohutao.message",
            teaching_directive=None,
        )
        runtime = _SequenceRuntime(rolled_back, retry)
        turns = CommitStore()
        invocations = _NoReceiptInvocations()
        hub = AgentHub(
            router=RoleRouter(),
            contexts=_StubSkillContexts(),
            runtime=runtime,
            turns=turns,
            invocations=invocations,
        )
        event = make_event("run_skill_requested")
        operation = make_operation()

        with self.assertRaises(AgentPersistenceError) as first:
            await hub.handle(event, operation)
        self.assertEqual(first.exception.code, "SIDE_EFFECT_ROLLED_BACK")
        self.assertEqual(turns.claims, {})
        self.assertEqual(turns.commits, [])
        self.assertEqual(invocations.lookups, 2)

        completed = await hub.handle(event, operation)
        self.assertTrue(completed.persisted)
        self.assertFalse(completed.replayed)
        self.assertEqual(runtime.calls, 2)
        self.assertEqual(len(turns.commits), 1)
        self.assertEqual(turns.claims, {})

    async def test_real_in_memory_skill_waters_all_eight_plots_once(self) -> None:
        operation = make_operation()
        event = make_event("run_skill_requested")
        state = make_world_state(plot_count=8, hydration=0)
        plots = state["plots"]
        self.assertIsInstance(plots, list)
        assert isinstance(plots, list)
        skill = make_skill()
        reads = RecordingReads(
            operation=operation,
            skill=skill,
            world=make_world_snapshot(operation, state=state),
        )
        configs = PackagedRoleConfigProvider.load()
        invocations = InMemoryWateringInvocations(operation, skill, state)
        contexts = _make_context_builder(reads, configs, worlds=invocations)
        trace = TraceSink()
        tools = build_default_tool_registry(trace, invocations)
        llm = SequenceLlm(
            [
                make_reply(
                    tool_calls_output(
                        "invoke_skill",
                        {
                            "skill_id": "bound_skill",
                            "arguments": {"length": 8},
                        },
                        call_id="call_invoke_0001",
                    )
                ),
                make_reply(
                    decision_output(
                        "xiaohutao",
                        "The verified run watered 8 of 8 plots.",
                    )
                ),
            ]
        )
        runtime = SharedAgentRuntime(
            llm=llm,
            role_configs=configs,
            tools=tools,
            prompts=PromptBuilder(),
            trace=trace,
            versions=make_versions(),
            clock=lambda: NOW,
        )
        turns = CommitStore()
        hub = AgentHub(
            router=RoleRouter(),
            contexts=contexts,
            runtime=runtime,
            turns=turns,
            invocations=invocations,
        )

        result = await hub.handle(event, operation)

        self.assertEqual(result.route.role, "xiaohutao")
        self.assertTrue(result.persisted)
        self.assertIsNotNone(result.decision)
        decision = result.decision
        assert decision is not None
        self.assertFalse(decision.degraded)
        self.assertEqual(decision.source, "provider")
        self.assertIsNone(decision.fallback_reason)
        self.assertEqual(invocations.call_count, 1)
        self.assertEqual(invocations.execution_count, 1)
        self.assertEqual(invocations.requests[0].arguments["length"], 8)
        self.assertEqual(sum(plot["hydration"] == 0 for plot in plots), 8)
        canonical_world = await invocations.get_snapshot(reads.session.world_id, operation)
        self.assertEqual(canonical_world.value.revision, 6)
        self.assertEqual(canonical_world.value.last_event_sequence, 48)
        canonical_plots = canonical_world.value.state["plots"]
        self.assertIsInstance(canonical_plots, tuple)
        assert isinstance(canonical_plots, tuple)
        self.assertEqual(sum(plot["hydration"] == 100 for plot in canonical_plots), 8)
        self.assertEqual(canonical_world.value.state_hash, invocations.state_sha256)
        self.assertEqual(len(decision.tool_calls), 1)
        self.assertEqual(decision.tool_calls[0].name, "invoke_skill")
        self.assertIs(decision, turns.commits[0][1])
        self.assertEqual(len(turns.commits), 1)
        self.assertEqual(decision.tool_calls[0].result_summary["task_success"], True)
        self.assertEqual(decision.tool_calls[0].result_summary["world_revision_after"], 6)
        self.assertEqual(
            [evidence.evidence_id for evidence in decision.evidence_refs],
            ["evidence_world_commit_0001"],
        )
        self.assertEqual(
            [event.name for event in trace.events].count("agent.tool.succeeded"),
            1,
        )

        replay = await hub.handle(event, operation)
        self.assertTrue(replay.replayed)
        self.assertIs(replay.decision, decision)
        self.assertEqual(invocations.call_count, 1)
        self.assertEqual(invocations.execution_count, 1)
        self.assertEqual(len(llm.requests), 2)
        self.assertEqual(len(turns.commits), 1)


if __name__ == "__main__":
    unittest.main()
