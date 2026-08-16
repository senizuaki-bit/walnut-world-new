from __future__ import annotations

import sys
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

import test_agent_backend_outcome_authority as outcome  # noqa: E402
import test_agent_backend_role_live_e2e as role_live  # noqa: E402
from yaya_agent_backend.codec import decode_as  # noqa: E402
from yaya_agent_backend.composition import create_production_composition  # noqa: E402
from yaya_agent_contracts import (  # noqa: E402
    CommandRecord,
    OperationContext,
    SandboxRunRequest,
)
from yaya_agent_runtime import (  # noqa: E402
    AgentDecision,
    AgentTurnCommitReceipt,
    GameEvent,
    RoleRoute,
)
from yaya_agent_sandbox import DockerCppSandbox  # noqa: E402


class _CountingSandbox:
    """Count only calls that reach the real pinned Docker Sandbox adapter."""

    def __init__(
        self,
        delegate: DockerCppSandbox,
        counters: dict[str, int],
        run_ids: list[str],
    ) -> None:
        self._delegate = delegate
        self._counters = counters
        self._run_ids = run_ids

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def run(self, request: SandboxRunRequest, context: OperationContext) -> object:
        self._counters["sandbox"] += 1
        self._run_ids.append(request.run_id)
        return await self._delegate.run(request, context)


class _CountingWorldParticipant:
    """Count attempts entering the production same-transaction World participant."""

    def __init__(self, delegate: Any, counters: dict[str, int]) -> None:
        self._delegate = delegate
        self._counters = counters

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def commit_on(
        self,
        connection: Any,
        request: Any,
        context: OperationContext,
    ) -> object:
        self._counters["world"] += 1
        return await self._delegate.commit_on(connection, request, context)


class _LoseTurnCommitResponse:
    """Drop one response only after the real Turn repository committed it."""

    def __init__(self, delegate: Any, *, command_id: str, event_type: str) -> None:
        self._delegate = delegate
        self._command_id = command_id
        self._event_type = event_type
        self.dropped = 0
        self.created_receipt: AgentTurnCommitReceipt | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def commit(
        self,
        event: GameEvent,
        route: RoleRoute,
        decision: AgentDecision,
        claim_id: str,
        context: OperationContext,
    ) -> AgentTurnCommitReceipt:
        receipt = await self._delegate.commit(event, route, decision, claim_id, context)
        if (
            self.dropped == 0
            and event.command_id == self._command_id
            and event.event_type == self._event_type
        ):
            if not receipt.created:
                raise AssertionError("response-loss seam reached a replayed Turn")
            self.created_receipt = receipt
            self.dropped += 1
            raise ConnectionResetError("simulated response loss after durable AgentTurn commit")
        return receipt


class AgentBackendOutcomeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """Production Worker recovery across root and derived Turn commit loss."""

    _skill_snapshot = staticmethod(role_live.AgentBackendRoleLiveE2E._skill_snapshot)
    _certified = staticmethod(role_live.AgentBackendRoleLiveE2E._certified)
    _reset_database = outcome.AgentBackendOutcomeAuthorityTests._reset_database
    _initialize_composition = outcome.AgentBackendOutcomeAuthorityTests._initialize_composition
    _install_llm = outcome.AgentBackendOutcomeAuthorityTests._install_llm
    _accept = outcome.AgentBackendOutcomeAuthorityTests._accept
    _await_terminal = outcome.AgentBackendOutcomeAuthorityTests._await_terminal
    _command_job_diagnostics = outcome.AgentBackendOutcomeAuthorityTests._command_job_diagnostics
    _turns = outcome.AgentBackendOutcomeAuthorityTests._turns
    _publication_counts = outcome.AgentBackendOutcomeAuthorityTests._publication_counts
    _side_effect_fingerprint = outcome.AgentBackendOutcomeAuthorityTests._side_effect_fingerprint
    _job_state = outcome.AgentBackendOutcomeAuthorityTests._job_state

    @classmethod
    def setUpClass(cls) -> None:
        outcome.AgentBackendOutcomeAuthorityTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls) -> None:
        outcome.AgentBackendOutcomeAuthorityTests.tearDownClass.__func__(cls)

    async def asyncSetUp(self) -> None:
        await self._reset_database()
        await self._initialize_composition()
        self.execution_attempts = {"sandbox": 0, "world": 0}
        self.sandbox_run_ids: list[str] = []
        self._install_execution_counters()

    def _install_execution_counters(self) -> None:
        invocations = cast(Any, self.composition.invocations)
        world_uow = cast(Any, self.composition.world_uow)
        self.assertIsInstance(self.composition.sandbox, DockerCppSandbox)
        self.assertIs(invocations._sandbox, self.composition.sandbox)
        self.assertIs(invocations._world_uow, self.composition.world_uow)
        invocations._sandbox = _CountingSandbox(
            self.composition.sandbox,
            self.execution_attempts,
            self.sandbox_run_ids,
        )
        world_uow._participant = _CountingWorldParticipant(
            world_uow.participant,
            self.execution_attempts,
        )

    async def _job_command_state(self, command_id: str) -> dict[str, object]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT j.state,j.attempt,j.worker_id,j.lease_id,j.last_error_code,
                       c.status,c.revision,c.record_json
                FROM yaya_command_jobs j JOIN yaya_commands c
                  ON c.tenant_id=j.tenant_id AND c.command_id=j.command_id
                WHERE j.command_id=%s
                """,
                (command_id,),
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            raise AssertionError("outcome recovery Command/Job disappeared")
        record = decode_as(row["record_json"], CommandRecord)
        return {
            "state": row["state"],
            "attempt": row["attempt"],
            "worker_id": row["worker_id"],
            "lease_id": row["lease_id"],
            "last_error_code": row["last_error_code"],
            "status": row["status"],
            "revision": row["revision"],
            "record_status": record.status.value,
            "record_revision": record.revision,
            "terminal": record.terminal,
        }

    async def _make_retry_immediately_available(self, command_id: str) -> None:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                UPDATE yaya_command_jobs SET available_at=clock_timestamp()
                WHERE command_id=%s AND state='READY'
                """,
                (command_id,),
            )
            self.assertEqual(cursor.rowcount, 1)
        finally:
            await connection.close()

    async def _rebuild_composition(self, llm: outcome._SchemaLlm, suffix: str) -> None:
        settings = replace(
            self.composition.settings,
            worker_id=f"outcome_recovery_{suffix}_0002",
        )
        self.composition = await create_production_composition(settings, migrate=False)
        self._install_llm(llm)
        self._install_execution_counters()

    async def _run_authority_fingerprint(self) -> tuple[int, str]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT count(*)::int AS count,
                       md5(COALESCE(string_agg(snapshot_json::text,'' ORDER BY run_id),'')) AS hash
                FROM yaya_runs
                """
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            raise AssertionError("Run authority fingerprint query returned no row")
        return cast(int, row["count"]), cast(str, row["hash"])

    async def _product_snapshot(self) -> tuple[object, ...]:
        page = await self.composition.product_application.list_interactions(
            self.actor,
            "session_watering_0001",
            after_sequence=0,
            limit=100,
        )
        interactions = cast(list[Mapping[str, object]], page.payload["interactions"])
        gets: list[tuple[Mapping[str, object], Mapping[str, str]]] = []
        for item in interactions:
            restored = await self.composition.product_application.get_interaction(
                self.actor,
                "session_watering_0001",
                cast(str, item["interaction_id"]),
            )
            gets.append((restored.payload, restored.headers))
        return page.payload, page.headers, tuple(gets)

    @staticmethod
    def _expected_counts(role: str) -> dict[str, int]:
        if role == "bug_agent":
            return {
                "commands": 3,
                "jobs": 3,
                "runs": 3,
                "evidence": 3,
                "invocations": 3,
                "turns": 6,
                "messages": 3,
                "interactions": 3,
                "events": 6,
                "projection_outbox": 9,
                "worker_outbox": 0,
                "learner_models": 0,
                "learner_jobs": 3,
                "learner_receipts": 0,
                "learner_failures": 0,
                "model_requests": 9,
                "world_revision": 5,
                "world_sequence": 0,
            }
        return {
            "commands": 2,
            "jobs": 2,
            "runs": 2,
            "evidence": 3,
            "invocations": 2,
            "turns": 4,
            "messages": 2,
            "interactions": 2,
            "events": 5,
            "projection_outbox": 7,
            "worker_outbox": 0,
            "learner_models": 0,
            "learner_jobs": 2,
            "learner_receipts": 0,
            "learner_failures": 0,
            "model_requests": 6,
            "world_revision": 6,
            "world_sequence": 1,
        }

    @staticmethod
    def _expected_execution_attempts(role: str) -> dict[str, int]:
        return {
            "sandbox": 3 if role == "bug_agent" else 2,
            "world": 0 if role == "bug_agent" else 1,
        }

    async def _prepare_target(
        self,
        role: str,
    ) -> tuple[Any, Any, int, str]:
        if role == "bug_agent":
            for sequence in (1, 2):
                accepted = cast(Any, await self._accept(self.failure_skill, sequence))
                self.assertTrue(await self.composition.worker.run_once())
                terminal = await self._await_terminal(accepted.command.command_id)
                self.assertEqual(terminal["status"], "REJECTED")
            sequence = 3
            skill = self.failure_skill
            derived_event_type = "run_failed"
        else:
            accepted = cast(Any, await self._accept(self.failure_skill, 1))
            self.assertTrue(await self.composition.worker.run_once())
            terminal = await self._await_terminal(accepted.command.command_id)
            self.assertEqual(terminal["status"], "REJECTED")
            await role_live.AgentBackendRoleLiveE2E._activate_second_version(
                self,
                self.composition,
                self.success_skill,
                self.success_certified,
            )
            sequence = 2
            skill = self.success_skill
            derived_event_type = "task_completed"
        target = cast(Any, await self._accept(skill, sequence))
        return target, skill, sequence, derived_event_type

    async def _assert_commit_response_loss_recovers(self, role: str, phase: str) -> None:
        llm = outcome._SchemaLlm()
        self._install_llm(llm)
        accepted, skill, sequence, derived_event_type = await self._prepare_target(role)
        target_event_type = "run_skill_requested" if phase == "root" else derived_event_type
        lost = _LoseTurnCommitResponse(
            self.composition.turns,
            command_id=accepted.command.command_id,
            event_type=target_event_type,
        )
        cast(Any, self.composition.hub)._turns = lost

        self.assertTrue(await self.composition.worker.run_once())
        self.assertEqual(lost.dropped, 1)
        self.assertIsNotNone(lost.created_receipt)
        after_loss_state = await self._job_command_state(accepted.command.command_id)
        self.assertEqual(
            after_loss_state,
            {
                "state": "READY",
                "attempt": 1,
                "worker_id": None,
                "lease_id": None,
                "last_error_code": "AGENT_TURN_COMMIT_FAILED",
                "status": "VALIDATING",
                "revision": 2,
                "record_status": "VALIDATING",
                "record_revision": 2,
                "terminal": False,
            },
        )
        after_loss_turns = await self._turns(accepted.command.command_id)
        self.assertEqual(len(after_loss_turns), 1 if phase == "root" else 2)
        self.assertEqual(after_loss_turns[0].event.event_type, "run_skill_requested")
        if phase == "derived":
            self.assertEqual(
                {item.event.event_type for item in after_loss_turns},
                {"run_skill_requested", derived_event_type},
            )

        expected = self._expected_counts(role)
        expected_after_loss = dict(expected)
        if phase == "root":
            expected_after_loss["turns"] -= 1
            expected_after_loss["messages"] -= 1
            expected_after_loss["interactions"] -= 1
            expected_after_loss["events"] -= 2
            expected_after_loss["projection_outbox"] -= 3
            expected_after_loss["learner_jobs"] -= 1
            expected_after_loss["model_requests"] -= 1
        self.assertEqual(
            await role_live.AgentBackendRoleLiveE2E._durable_counts(self.composition),
            expected_after_loss,
        )
        expected_attempts = self._expected_execution_attempts(role)
        self.assertEqual(self.execution_attempts, expected_attempts)
        self.assertEqual(len(set(self.sandbox_run_ids)), expected_attempts["sandbox"])
        loss_fingerprint = await self._side_effect_fingerprint()
        loss_run_authority = await self._run_authority_fingerprint()

        await self._make_retry_immediately_available(accepted.command.command_id)
        await self._rebuild_composition(llm, f"{role}_{phase}_first")
        self.assertTrue(await self.composition.worker.run_once())
        terminal = await self._await_terminal(accepted.command.command_id)
        self.assertEqual(terminal["status"], "REJECTED" if role == "bug_agent" else "APPLIED")
        recovered_state = await self._job_command_state(accepted.command.command_id)
        self.assertEqual(recovered_state["state"], "DONE")
        self.assertEqual(recovered_state["attempt"], 2)
        self.assertIsNone(recovered_state["last_error_code"])
        self.assertIs(recovered_state["terminal"], True)
        recovered_turns = await self._turns(accepted.command.command_id)
        self.assertEqual(len(recovered_turns), 2)
        final = [item for item in recovered_turns if item.event.event_type == derived_event_type]
        self.assertEqual(len(final), 1)
        self.assertEqual(final[0].route.role, role)
        self.assertEqual(len(llm.requests), expected["model_requests"])
        self.assertEqual(self.execution_attempts, expected_attempts)
        self.assertEqual(len(set(self.sandbox_run_ids)), expected_attempts["sandbox"])
        recovered_counts = await role_live.AgentBackendRoleLiveE2E._durable_counts(self.composition)
        self.assertEqual(recovered_counts, expected)
        recovered_fingerprint = await self._side_effect_fingerprint()
        if phase == "derived":
            self.assertEqual(recovered_fingerprint, loss_fingerprint)
        else:
            self.assertEqual(await self._run_authority_fingerprint(), loss_run_authority)
            for table in ("yaya_evidence", "yaya_skill_invocations", "yaya_worlds"):
                self.assertEqual(recovered_fingerprint[table], loss_fingerprint[table])

        model_requests, finished = await role_live.AgentBackendRoleLiveE2E._model_trace_graph(
            self.composition
        )
        turn_id = f"turn_outcome_authority_{sequence:04d}"
        self.assertEqual(model_requests[(turn_id, "xiaohutao")], 2)
        self.assertEqual(model_requests[(turn_id, role)], 1)
        self.assertIn((turn_id, "xiaohutao"), finished)
        self.assertIn((turn_id, role), finished)

        product = await self._product_snapshot()
        stable_counts = await role_live.AgentBackendRoleLiveE2E._durable_counts(self.composition)
        stable_fingerprint = await role_live.AgentBackendRoleLiveE2E._business_fingerprint(
            self.composition
        )
        replay = cast(Any, await self._accept(skill, sequence))
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.receipt, accepted.receipt)
        self.assertEqual(replay.command.command_id, accepted.command.command_id)
        self.assertIs(replay.command.terminal, True)
        self.assertFalse(await self.composition.worker.run_once())

        await self._rebuild_composition(llm, f"{role}_{phase}_second")
        self.assertFalse(await self.composition.worker.run_once())
        self.assertEqual(await self._product_snapshot(), product)
        self.assertEqual(
            await role_live.AgentBackendRoleLiveE2E._durable_counts(self.composition),
            stable_counts,
        )
        self.assertEqual(
            await role_live.AgentBackendRoleLiveE2E._business_fingerprint(self.composition),
            stable_fingerprint,
        )
        self.assertEqual(len(llm.requests), expected["model_requests"])
        self.assertEqual(self.execution_attempts, expected_attempts)
        self.assertEqual(len(set(self.sandbox_run_ids)), expected_attempts["sandbox"])

    async def test_bug_root_commit_response_loss_recovers_after_composition_restart_without_duplicates(
        self,
    ) -> None:
        await self._assert_commit_response_loss_recovers("bug_agent", "root")

    async def test_bug_derived_commit_response_loss_recovers_after_composition_restart_without_duplicates(
        self,
    ) -> None:
        await self._assert_commit_response_loss_recovers("bug_agent", "derived")

    async def test_book_root_commit_response_loss_recovers_after_composition_restart_without_duplicates(
        self,
    ) -> None:
        await self._assert_commit_response_loss_recovers("book_agent", "root")

    async def test_book_derived_commit_response_loss_recovers_after_composition_restart_without_duplicates(
        self,
    ) -> None:
        await self._assert_commit_response_loss_recovers("book_agent", "derived")


if __name__ == "__main__":
    unittest.main()
