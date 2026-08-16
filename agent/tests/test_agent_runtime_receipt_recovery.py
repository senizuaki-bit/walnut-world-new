from __future__ import annotations

import asyncio
import sys
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from agent_runtime_fixtures import (  # noqa: E402
    NOW,
    WORLD_ID,
    CommitStore,
    InMemoryWateringInvocations,
    RecordingReads,
    SequenceLlm,
    StaticRoleConfigs,
    TraceSink,
    decision_output,
    make_event,
    make_evidence,
    make_operation,
    make_reply,
    make_skill,
    make_versions,
    make_world_state,
    tool_calls_output,
)
from yaya_agent_contracts import EvidenceType  # noqa: E402
from yaya_agent_runtime import (  # noqa: E402
    AgentHub,
    AgentPersistenceError,
    ContextBuilder,
    PackagedRoleConfigProvider,
    PromptBuilder,
    RoleRouter,
    RunResultSnapshot,
    SharedAgentRuntime,
    SkillInvocationRequest,
    SkillInvocationResult,
    build_default_tool_registry,
    side_effect_execution_id,
    skill_invocation_request_sha256,
)


class _InvocationFaultPort:
    """Expose the atomic adapter while counting and optionally delaying delivery."""

    def __init__(
        self,
        application: InMemoryWateringInvocations,
        *,
        pause_after_commit: bool = False,
    ) -> None:
        self.application = application
        self.pause_after_commit = pause_after_commit
        self.invoke_calls = 0
        self.get_result_calls = 0
        self.snapshot_calls = 0
        self.lookup_ids: list[str] = []
        self.world_committed = asyncio.Event()
        self.release_response = asyncio.Event()

    async def get_snapshot(self, world_id, context):
        self.snapshot_calls += 1
        return await self.application.get_snapshot(world_id, context)

    async def get_result(self, invocation_id, context):
        self.get_result_calls += 1
        self.lookup_ids.append(invocation_id)
        return await self.application.get_result(invocation_id, context)

    async def invoke(self, request, context):
        self.invoke_calls += 1
        result = await self.application.invoke(request, context)
        self.world_committed.set()
        if self.pause_after_commit:
            await self.release_response.wait()
        return result


class _WrongReceiptPort(_InvocationFaultPort):
    def __init__(self, application, result: SkillInvocationResult) -> None:
        super().__init__(application)
        self.result = result

    async def get_result(self, invocation_id, context):
        del context
        self.get_result_calls += 1
        self.lookup_ids.append(invocation_id)
        return self.result


class _DelayedCommitPort(_InvocationFaultPort):
    def __init__(self, application, *, delay_seconds: float) -> None:
        super().__init__(application)
        self.delay_seconds = delay_seconds
        self.cancelled_calls = 0
        self.background_tasks: list[asyncio.Task[object]] = []

    async def invoke(self, request, context):
        self.invoke_calls += 1

        async def commit_later():
            await asyncio.sleep(self.delay_seconds)
            result = await self.application.invoke(request, context)
            self.world_committed.set()
            return result

        task = asyncio.create_task(commit_later())
        self.background_tasks.append(task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self.cancelled_calls += 1
            raise


class _NeverReceiptPort(_InvocationFaultPort):
    def __init__(self, application) -> None:
        super().__init__(application)
        self.cancelled_calls = 0
        self.never_finishes = asyncio.Event()

    async def invoke(self, request, context):
        del request, context
        self.invoke_calls += 1
        try:
            await self.never_finishes.wait()
        except asyncio.CancelledError:
            self.cancelled_calls += 1
            raise
        raise AssertionError("unreachable fixture state")


class _CountingRuntime:
    def __init__(self, runtime: SharedAgentRuntime) -> None:
        self.runtime = runtime
        self.run_calls = 0
        self.recovery_calls = 0

    def execution_budget_ms(self, role):
        return self.runtime.execution_budget_ms(role)

    async def run(self, role, context, operation_context):
        self.run_calls += 1
        return await self.runtime.run(role, context, operation_context)

    async def recover_skill_invocation(
        self,
        scope,
        result,
        operation_context,
        **kwargs,
    ):
        self.recovery_calls += 1
        return await self.runtime.recover_skill_invocation(
            scope,
            result,
            operation_context,
            **kwargs,
        )


def _context_builder(reads, configs, invocations) -> ContextBuilder:
    return ContextBuilder(
        tasks=reads,
        sessions=reads,
        skills=reads,
        runs=reads,
        counterexamples=reads,
        learners=reads,
        messages=reads,
        worlds=invocations,
        role_configs=configs,
    )


def _runtime(llm, configs, invocations, trace) -> _CountingRuntime:
    return _CountingRuntime(
        SharedAgentRuntime(
            llm=llm,
            role_configs=configs,
            tools=build_default_tool_registry(trace, invocations),
            prompts=PromptBuilder(),
            trace=trace,
            versions=make_versions(),
            clock=lambda: NOW,
        )
    )


def _fast_xiaohutao_configs(*, timeout_ms: int = 20) -> StaticRoleConfigs:
    packaged = PackagedRoleConfigProvider.load().get("xiaohutao")
    return StaticRoleConfigs(replace(packaged, timeout_ms=timeout_ms))


def _invoke_all_plots_reply(call_id: str):
    return make_reply(
        tool_calls_output(
            "invoke_skill",
            {"skill_id": "bound_skill", "arguments": {"length": 8}},
            call_id=call_id,
        )
    )


def _invocation_request(event, operation, *, invocation_id: str | None = None):
    resolved_id = invocation_id or side_effect_execution_id(event.command_id, event.turn_id)
    arguments = {"length": 8}
    request_sha256 = skill_invocation_request_sha256(
        tenant_id=operation.actor.tenant_id,
        invocation_id=resolved_id,
        session_id=event.session_id,
        turn_id=event.turn_id,
        command_id=event.command_id,
        world_id=WORLD_ID,
        expected_world_revision=event.expected_world_revision,
        skill_ref=event.skill_ref,
        arguments=arguments,
    )
    return SkillInvocationRequest(
        resolved_id,
        operation.actor.tenant_id,
        event.session_id,
        event.turn_id,
        event.command_id,
        WORLD_ID,
        event.expected_world_revision,
        event.skill_ref,
        arguments,
        request_sha256,
    )


def _oversize_run(*, world_difference, failed_actions) -> RunResultSnapshot:
    operation = make_operation()
    event = make_event("run_skill_requested")
    return RunResultSnapshot(
        run_id="run_oversize_fixture_0001",
        session_id=event.session_id,
        turn_id=event.turn_id,
        command_id=event.command_id,
        world_id=WORLD_ID,
        skill_ref=event.skill_ref,
        task_success=False,
        world_revision_before=event.expected_world_revision,
        world_revision_after=event.expected_world_revision,
        world_difference=world_difference,
        failed_actions=failed_actions,
        failure_key="oversize_fixture",
        evidence_refs=(make_evidence("evidence_oversize_fixture_0001"),),
        world_commit=None,
        request_context=operation,
    )


class AgentRuntimeReceiptRecoveryE2ETests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_invoke_response_recovers_atomic_receipt_before_fallback_commit(
        self,
    ) -> None:
        operation = make_operation()
        event = make_event("run_skill_requested")
        state = make_world_state(plot_count=8, hydration=0)
        skill = make_skill(operation)
        application = InMemoryWateringInvocations(operation, skill, state)
        application.fail_after_next_commit = True
        invocations = _InvocationFaultPort(application)
        reads = RecordingReads(operation=operation, skill=skill)
        configs = PackagedRoleConfigProvider.load()
        trace = TraceSink()
        llm = SequenceLlm([_invoke_all_plots_reply("call_lost_receipt_0001")])
        runtime = _runtime(llm, configs, invocations, trace)
        turns = CommitStore()
        hub = AgentHub(
            router=RoleRouter(),
            contexts=_context_builder(reads, configs, invocations),
            runtime=runtime,
            turns=turns,
            invocations=invocations,
        )

        result = await hub.handle(event, operation)

        self.assertTrue(result.persisted)
        self.assertFalse(result.replayed)
        self.assertIsNotNone(result.decision)
        decision = result.decision
        assert decision is not None
        self.assertTrue(decision.degraded)
        self.assertEqual(decision.fallback_reason, "SIDE_EFFECT_RECEIPT_RECOVERED")
        self.assertEqual(decision.message_key, "agent.skill.recovery")
        self.assertEqual(runtime.run_calls, 1)
        self.assertEqual(runtime.recovery_calls, 1)
        self.assertEqual(len(llm.requests), 1)
        self.assertEqual(invocations.invoke_calls, 1)
        self.assertFalse(application.fail_after_next_commit)
        self.assertEqual(invocations.get_result_calls, 2)
        self.assertEqual(len(set(invocations.lookup_ids)), 1)
        self.assertEqual(application.call_count, 1)
        self.assertEqual(application.execution_count, 1)
        self.assertEqual(application.revision, 6)
        self.assertEqual(len(application._receipts), 1)
        receipt = next(iter(application._receipts.values()))[1]
        self.assertIsNotNone(receipt.run.world_commit)
        self.assertEqual(decision.evidence_refs, receipt.run.evidence_refs)
        self.assertEqual(
            [item.evidence_type for item in decision.evidence_refs],
            [EvidenceType.WORLD_COMMIT],
        )
        self.assertEqual(decision.tool_calls[0].execution_id, receipt.invocation_id)
        self.assertEqual(
            decision.tool_calls[0].result_summary["world_difference"],
            receipt.run.world_difference,
        )
        plots = application.state["plots"]
        self.assertIsInstance(plots, list)
        assert isinstance(plots, list)
        self.assertEqual(sum(plot["hydration"] == 100 for plot in plots), 8)
        self.assertEqual(len(turns.commits), 1)
        self.assertIs(turns.commits[0][1], decision)

        replay = await hub.handle(event, operation)

        self.assertTrue(replay.replayed)
        self.assertIs(replay.decision, decision)
        self.assertEqual(invocations.invoke_calls, 1)
        self.assertEqual(invocations.get_result_calls, 2)
        self.assertEqual(application.execution_count, 1)
        self.assertEqual(application.revision, 6)
        self.assertEqual(len(turns.commits), 1)

    async def test_tool_timeout_recovers_receipt_from_shielded_delayed_commit(self) -> None:
        operation = make_operation()
        event = make_event("run_skill_requested")
        skill = make_skill(operation)
        application = InMemoryWateringInvocations(
            operation,
            skill,
            make_world_state(plot_count=8, hydration=0),
        )
        invocations = _DelayedCommitPort(application, delay_seconds=0.08)
        reads = RecordingReads(operation=operation, skill=skill)
        configs = _fast_xiaohutao_configs(timeout_ms=20)
        trace = TraceSink()
        llm = SequenceLlm([_invoke_all_plots_reply("call_delayed_commit_0001")])
        runtime = _runtime(llm, configs, invocations, trace)
        turns = CommitStore()
        hub = AgentHub(
            router=RoleRouter(),
            contexts=_context_builder(reads, configs, invocations),
            runtime=runtime,
            turns=turns,
            invocations=invocations,
        )

        result = await hub.handle(event, operation)

        self.assertTrue(result.persisted)
        self.assertFalse(result.replayed)
        self.assertIsNotNone(result.decision)
        decision = result.decision
        assert decision is not None
        self.assertTrue(decision.degraded)
        self.assertEqual(decision.fallback_reason, "SIDE_EFFECT_RECEIPT_RECOVERED")
        self.assertIn("SIDE_EFFECT_COMMIT_UNKNOWN", decision.runtime_warnings)
        self.assertEqual(runtime.run_calls, 1)
        self.assertEqual(runtime.recovery_calls, 1)
        self.assertEqual(len(llm.requests), 1)
        self.assertEqual(invocations.invoke_calls, 1)
        self.assertEqual(invocations.cancelled_calls, 1)
        self.assertGreaterEqual(invocations.get_result_calls, 3)
        self.assertEqual(len(set(invocations.lookup_ids)), 1)
        self.assertEqual(len(invocations.background_tasks), 1)
        self.assertTrue(invocations.background_tasks[0].done())
        self.assertIsNone(invocations.background_tasks[0].exception())
        self.assertEqual(application.call_count, 1)
        self.assertEqual(application.execution_count, 1)
        self.assertEqual(application.revision, 6)
        self.assertEqual(len(application._receipts), 1)
        self.assertEqual(len(turns.commits), 1)
        self.assertIs(turns.commits[0][1], decision)
        receipt = next(iter(application._receipts.values()))[1]
        self.assertEqual(decision.evidence_refs, receipt.run.evidence_refs)
        self.assertEqual(
            [item.evidence_type for item in decision.evidence_refs],
            [EvidenceType.WORLD_COMMIT],
        )

        replay = await hub.handle(event, operation)

        self.assertTrue(replay.replayed)
        self.assertIs(replay.decision, decision)
        self.assertEqual(invocations.invoke_calls, 1)
        self.assertEqual(application.execution_count, 1)
        self.assertEqual(len(turns.commits), 1)

    async def test_tool_timeout_without_receipt_is_explicit_unknown_and_never_commits(
        self,
    ) -> None:
        operation = make_operation()
        event = make_event("run_skill_requested")
        skill = make_skill(operation)
        application = InMemoryWateringInvocations(
            operation,
            skill,
            make_world_state(plot_count=8, hydration=0),
        )
        invocations = _NeverReceiptPort(application)
        reads = RecordingReads(operation=operation, skill=skill)
        configs = _fast_xiaohutao_configs(timeout_ms=10)
        trace = TraceSink()
        llm = SequenceLlm([_invoke_all_plots_reply("call_unknown_commit_0001")])
        runtime = _runtime(llm, configs, invocations, trace)
        turns = CommitStore()
        hub = AgentHub(
            router=RoleRouter(),
            contexts=_context_builder(reads, configs, invocations),
            runtime=runtime,
            turns=turns,
            invocations=invocations,
        )

        with self.assertRaises(AgentPersistenceError) as raised:
            await hub.handle(event, operation)

        self.assertEqual(raised.exception.code, "UNKNOWN_COMMIT_STATE")
        self.assertEqual(
            raised.exception.details["invocation_id"],
            side_effect_execution_id(event.command_id, event.turn_id),
        )
        self.assertEqual(runtime.run_calls, 1)
        self.assertEqual(runtime.recovery_calls, 0)
        self.assertEqual(len(llm.requests), 1)
        self.assertEqual(invocations.invoke_calls, 1)
        self.assertEqual(invocations.cancelled_calls, 1)
        self.assertEqual(invocations.get_result_calls, 6)
        self.assertEqual(len(set(invocations.lookup_ids)), 1)
        self.assertEqual(application.call_count, 0)
        self.assertEqual(application.execution_count, 0)
        self.assertEqual(application.revision, 5)
        self.assertEqual(application._receipts, {})
        self.assertEqual(turns.commits, [])
        self.assertEqual(turns.records, {})
        self.assertEqual(len(turns.claims), 1)

    async def test_takeover_recovers_receipt_without_stale_world_read_and_fences_worker_a(
        self,
    ) -> None:
        operation = make_operation()
        event = make_event("run_skill_requested")
        state = make_world_state(plot_count=8, hydration=0)
        skill = make_skill(operation)
        application = InMemoryWateringInvocations(operation, skill, state)
        invocations = _InvocationFaultPort(application, pause_after_commit=True)
        reads = RecordingReads(operation=operation, skill=skill)
        configs = PackagedRoleConfigProvider.load()
        trace = TraceSink()
        llm = SequenceLlm(
            [
                _invoke_all_plots_reply("call_worker_a_invoke_0001"),
                make_reply(
                    decision_output(
                        "xiaohutao",
                        "Worker A received the original invocation response.",
                    )
                ),
            ]
        )
        runtime = _runtime(llm, configs, invocations, trace)
        current_time = [NOW]
        turns = CommitStore(clock=lambda: current_time[0], lease_seconds=5)
        contexts = _context_builder(reads, configs, invocations)
        worker_a_hub = AgentHub(
            router=RoleRouter(),
            contexts=contexts,
            runtime=runtime,
            turns=turns,
            invocations=invocations,
        )
        worker_b_hub = AgentHub(
            router=RoleRouter(),
            contexts=contexts,
            runtime=runtime,
            turns=turns,
            invocations=invocations,
        )

        worker_a = asyncio.create_task(worker_a_hub.handle(event, operation))
        await asyncio.wait_for(invocations.world_committed.wait(), timeout=1)
        self.assertEqual(application.revision, 6)
        self.assertEqual(application.execution_count, 1)
        self.assertEqual(invocations.snapshot_calls, 1)
        current_time[0] = NOW + timedelta(milliseconds=runtime.execution_budget_ms("xiaohutao") + 1)

        try:
            winner = await worker_b_hub.handle(event, operation)
        finally:
            invocations.release_response.set()
            worker_a_outcome = (await asyncio.gather(worker_a, return_exceptions=True))[0]

        if isinstance(worker_a_outcome, BaseException):
            raise worker_a_outcome
        worker_a_result = worker_a_outcome
        self.assertTrue(winner.persisted)
        self.assertFalse(winner.replayed)
        self.assertIsNotNone(winner.decision)
        winner_decision = winner.decision
        assert winner_decision is not None
        self.assertTrue(winner_decision.degraded)
        self.assertEqual(
            winner_decision.fallback_reason,
            "SIDE_EFFECT_RECEIPT_RECOVERED",
        )
        self.assertEqual(invocations.snapshot_calls, 1)
        self.assertEqual(reads.calls.count("list_active_skills"), 1)
        self.assertEqual(runtime.run_calls, 1)
        self.assertEqual(runtime.recovery_calls, 1)
        self.assertEqual(invocations.invoke_calls, 1)
        self.assertEqual(application.call_count, 1)
        self.assertEqual(application.execution_count, 1)
        self.assertEqual(application.revision, 6)
        self.assertEqual(len(application._receipts), 1)
        self.assertEqual(len(turns.commits), 1)
        self.assertIs(turns.commits[0][1], winner_decision)

        self.assertTrue(worker_a_result.persisted)
        self.assertTrue(worker_a_result.replayed)
        self.assertIs(worker_a_result.decision, winner_decision)
        assert worker_a_result.decision is not None
        self.assertEqual(
            worker_a_result.decision.fallback_reason,
            "SIDE_EFFECT_RECEIPT_RECOVERED",
        )

        replay = await worker_a_hub.handle(event, operation)

        self.assertTrue(replay.replayed)
        self.assertIs(replay.decision, winner_decision)
        self.assertEqual(invocations.get_result_calls, 2)
        self.assertEqual(invocations.snapshot_calls, 1)
        self.assertEqual(invocations.invoke_calls, 1)
        self.assertEqual(application.execution_count, 1)
        self.assertEqual(len(turns.commits), 1)

    async def test_lookup_rejects_self_consistent_receipt_for_another_invocation_id(
        self,
    ) -> None:
        operation = make_operation()
        event = make_event("run_skill_requested")
        skill = make_skill(operation)
        wrong_id = "toolexec_wrong_receipt_0001"
        arguments = {"length": 8}
        run = RunResultSnapshot(
            run_id="run_wrong_receipt_0001",
            session_id=event.session_id,
            turn_id=event.turn_id,
            command_id=event.command_id,
            world_id=WORLD_ID,
            skill_ref=skill.ref,
            task_success=False,
            world_revision_before=event.expected_world_revision,
            world_revision_after=event.expected_world_revision,
            world_difference={"watered_plots": 0},
            failed_actions=({"reason": "wrong_invocation_fixture"},),
            failure_key="wrong_invocation_fixture",
            evidence_refs=(make_evidence("evidence_wrong_receipt_0001"),),
            world_commit=None,
            request_context=operation,
        )
        wrong_sha256 = skill_invocation_request_sha256(
            tenant_id=operation.actor.tenant_id,
            invocation_id=wrong_id,
            session_id=event.session_id,
            turn_id=event.turn_id,
            command_id=event.command_id,
            world_id=WORLD_ID,
            expected_world_revision=event.expected_world_revision,
            skill_ref=skill.ref,
            arguments=arguments,
        )
        wrong_receipt = SkillInvocationResult(
            wrong_id,
            operation.actor.tenant_id,
            wrong_sha256,
            arguments,
            run,
        )
        application = InMemoryWateringInvocations(
            operation,
            skill,
            make_world_state(),
        )
        invocations = _WrongReceiptPort(application, wrong_receipt)
        reads = RecordingReads(operation=operation, skill=skill)
        configs = PackagedRoleConfigProvider.load()
        trace = TraceSink()
        llm = SequenceLlm([])
        runtime = _runtime(llm, configs, invocations, trace)
        turns = CommitStore()
        hub = AgentHub(
            router=RoleRouter(),
            contexts=_context_builder(reads, configs, invocations),
            runtime=runtime,
            turns=turns,
            invocations=invocations,
        )

        with self.assertRaises(AgentPersistenceError) as raised:
            await hub.handle(event, operation)

        self.assertEqual(
            raised.exception.code,
            "AGENT_SKILL_RECEIPT_IDENTITY_MISMATCH",
        )
        self.assertEqual(invocations.get_result_calls, 1)
        self.assertEqual(invocations.snapshot_calls, 0)
        self.assertEqual(invocations.invoke_calls, 0)
        self.assertEqual(reads.calls, [])
        self.assertEqual(runtime.run_calls, 0)
        self.assertEqual(runtime.recovery_calls, 0)
        self.assertEqual(llm.requests, [])
        self.assertEqual(turns.commits, [])
        self.assertEqual(turns.records, {})

    def test_run_snapshot_rejects_oversized_utf8_diagnostics(self) -> None:
        oversized_text = "界" * 9_000

        with self.assertRaisesRegex(
            ValueError,
            "world_difference exceeds its 24576-byte canonical bound",
        ):
            _oversize_run(
                world_difference={"diagnostic": oversized_text},
                failed_actions=({"reason": "fixture"},),
            )

        with self.assertRaisesRegex(
            ValueError,
            "failed_actions exceeds its 24576-byte canonical bound",
        ):
            _oversize_run(
                world_difference={"diagnostic": "bounded"},
                failed_actions=({"reason": "fixture", "diagnostic": oversized_text},),
            )

    async def test_invocation_stages_complete_result_before_publishing_world(self) -> None:
        operation = make_operation()
        event = make_event("run_skill_requested")
        skill = make_skill(operation)
        application = InMemoryWateringInvocations(
            operation,
            skill,
            make_world_state(plot_count=8, hydration=0),
        )
        request = _invocation_request(event, operation)
        original_state = deepcopy(application.state)
        original_hash = application.state_sha256
        original_revision = application.revision
        original_sequence = application.last_event_sequence

        with patch(
            "agent_runtime_fixtures.world_commit_receipt_sha256",
            side_effect=ValueError("injected Run construction failure"),
        ):
            with self.assertRaisesRegex(ValueError, "injected Run construction failure"):
                await application.invoke(request, operation)

        self.assertEqual(application.state, original_state)
        self.assertEqual(application.state_sha256, original_hash)
        self.assertEqual(application.revision, original_revision)
        self.assertEqual(application.last_event_sequence, original_sequence)
        self.assertEqual(application._receipts, {})
        self.assertIsNone(await application.get_result(request.invocation_id, operation))


if __name__ == "__main__":
    unittest.main()
