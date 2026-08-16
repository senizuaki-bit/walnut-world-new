from __future__ import annotations

import json
import sys
import unittest
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

import test_agent_backend_outcome_authority as outcome  # noqa: E402
import test_agent_backend_role_live_e2e as role_live  # noqa: E402
from agent_runtime_fixtures import make_reply  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from yaya_agent_backend.codec import decode_as, plain  # noqa: E402
from yaya_agent_backend.database import PostgresDatabase  # noqa: E402
from yaya_agent_backend.product_application import (  # noqa: E402
    ProductApplicationError,
)
from yaya_agent_contracts import (  # noqa: E402
    EvidenceRef,
    EvidenceType,
    LlmRequest,
    OperationContext,
)
from yaya_agent_runtime import GameEvent, RunResultSnapshot  # noqa: E402

type _Fingerprint = dict[str, tuple[int, str]]
type _DatabaseMutation = Callable[[PostgresDatabase, GameEvent], Awaitable[None]]
type _EventMutation = Callable[[GameEvent], GameEvent]
type _DecisionMutation = Callable[[dict[str, object]], None]


def _requested_role(request: LlmRequest) -> str:
    schema = cast(dict[str, object], plain(request.output_schema))
    raw_variants = schema.get("oneOf")
    variants = (
        cast(list[dict[str, object]], raw_variants) if isinstance(raw_variants, list) else [schema]
    )
    decision_variant = next(
        variant
        for variant in variants
        if cast(dict[str, object], variant["properties"])["kind"]
        == {"type": "string", "const": "decision"}
    )
    envelope_properties = cast(dict[str, object], decision_variant["properties"])
    decision_schema = cast(dict[str, object], envelope_properties["decision"])
    raw_decision_variants = decision_schema.get("oneOf")
    decision_variants = (
        cast(list[dict[str, object]], raw_decision_variants)
        if isinstance(raw_decision_variants, list)
        else [decision_schema]
    )
    properties = cast(dict[str, object], decision_variants[0]["properties"])
    return cast(str, cast(dict[str, object], properties["role"])["const"])


class _InvalidBookLlm(outcome._SchemaLlm):
    """Return one deliberately untrusted Book decision on both repair attempts."""

    def __init__(
        self,
        mutation: _DecisionMutation,
        snapshot: Callable[[], Awaitable[_Fingerprint]],
    ) -> None:
        super().__init__()
        self._mutation = mutation
        self._snapshot = snapshot
        self.roles: list[str] = []
        self.before_final: _Fingerprint | None = None

    async def generate(
        self,
        request: LlmRequest,
        context: OperationContext,
    ) -> object:
        role = _requested_role(request)
        self.roles.append(role)
        result = await super().generate(request, context)
        if role != "book_agent":
            return result
        if self.before_final is None:
            self.before_final = await self._snapshot()
        output = cast(dict[str, object], plain(cast(Any, result).value.output))
        decision = cast(dict[str, object], output["decision"])
        self._mutation(decision)
        return make_reply(output)


class AgentBackendBookOutcomeMatrixTests(unittest.IsolatedAsyncioTestCase):
    """Provider-independent Book trust matrix on the production Worker chain."""

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
    _run_worker = outcome.AgentBackendOutcomeAuthorityTests._run_worker
    _stop_worker = staticmethod(outcome.AgentBackendOutcomeAuthorityTests._stop_worker)

    @classmethod
    def setUpClass(cls) -> None:
        outcome.AgentBackendOutcomeAuthorityTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls) -> None:
        outcome.AgentBackendOutcomeAuthorityTests.tearDownClass.__func__(cls)

    async def asyncSetUp(self) -> None:
        await self._reset_database()
        await self._initialize_composition()

    async def _assert_current_absent_from_product(self, command_id: str) -> None:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT count(*)::int AS count FROM yaya_agent_interactions
                WHERE command_id=%s
                """,
                (command_id,),
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["count"], 0, "untrusted Book data reached Product storage")

        try:
            page = await self.composition.product_application.list_interactions(
                self.actor,
                "session_watering_0001",
                after_sequence=0,
                limit=100,
            )
        except ProductApplicationError as error:
            # A test-induced historical authority corruption may make the entire
            # Product projection unreadable.  That is the required fail-closed
            # result; returning the untrusted current turn would not be.
            self.assertEqual(error.code, "INVARIANT_VIOLATION")
            return
        interactions = cast(list[Mapping[str, object]], page.payload["interactions"])
        self.assertNotIn("book_agent", {item.get("role") for item in interactions})

    async def _assert_common_failed_closure(
        self,
        *,
        command_id: str,
        before_final: _Fingerprint | None,
        terminal: Mapping[str, object],
        expected_cause: str | None = None,
    ) -> None:
        self.assertEqual(terminal["status"], "FAILED")
        error = cast(Mapping[str, object], terminal["error"])
        details = cast(Mapping[str, object], error["details"])
        cause = details.get("cause_code")
        self.assertIsInstance(cause, str)
        if expected_cause is not None:
            self.assertEqual(cause, expected_cause)
        self.assertIsNotNone(before_final)
        self.assertEqual(await self._side_effect_fingerprint(), before_final)
        turns = await self._turns(command_id)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].event.event_type, "run_skill_requested")
        self.assertEqual(await self._job_state(command_id), "DONE")
        await self._assert_current_absent_from_product(command_id)

    async def _one_failure_then_activate(
        self,
        llm: outcome._SchemaLlm,
    ) -> tuple[Any, Any]:
        self._install_llm(llm)
        stop, worker_task = await self._run_worker()
        try:
            accepted = cast(Any, await self._accept(self.failure_skill, 1))
            terminal = await self._await_terminal(accepted.command.command_id)
            if terminal["status"] != "REJECTED":
                self.fail(
                    {
                        "terminal": terminal,
                        "llm_roles": getattr(llm, "roles", None),
                        "diagnostics": await self._command_job_diagnostics(),
                    }
                )
            self.assertEqual(await self._job_state(accepted.command.command_id), "DONE")
            await role_live.AgentBackendRoleLiveE2E._activate_second_version(
                self,
                self.composition,
                self.success_skill,
                self.success_certified,
            )
        except BaseException:
            await self._stop_worker(stop, worker_task)
            raise
        return stop, worker_task

    async def _assert_success_database_mutation_fails(
        self,
        mutation: _DatabaseMutation,
    ) -> None:
        llm = outcome._SchemaLlm()
        stop, worker_task = await self._one_failure_then_activate(llm)
        worker = cast(Any, self.composition.worker)
        wrapper = outcome._MutatingOutcomeAuthority(
            worker._outcome_authority,
            self.database,
            mutation,
            self._side_effect_fingerprint,
        )
        worker._outcome_authority = wrapper
        try:
            accepted = cast(Any, await self._accept(self.success_skill, 2))
            terminal = await self._await_terminal(accepted.command.command_id)
        finally:
            await self._stop_worker(stop, worker_task)
        self.assertEqual(wrapper.calls, 1)
        self.assertEqual(len(llm.requests), 5, "corrupt success reached Book Provider")
        await self._assert_common_failed_closure(
            command_id=accepted.command.command_id,
            before_final=wrapper.before_final,
            terminal=terminal,
        )

    async def _assert_success_event_mutation_fails(
        self,
        mutation: _EventMutation,
    ) -> None:
        llm = outcome._SchemaLlm()
        stop, worker_task = await self._one_failure_then_activate(llm)
        worker = cast(Any, self.composition.worker)
        wrapper = outcome._EventMutatingOutcomeAuthority(
            worker._outcome_authority,
            mutation,
            self._side_effect_fingerprint,
        )
        worker._outcome_authority = wrapper
        try:
            accepted = cast(Any, await self._accept(self.success_skill, 2))
            terminal = await self._await_terminal(accepted.command.command_id)
        finally:
            await self._stop_worker(stop, worker_task)
        self.assertEqual(wrapper.calls, 1)
        self.assertEqual(len(llm.requests), 5, "corrupt success reached Book Provider")
        await self._assert_common_failed_closure(
            command_id=accepted.command.command_id,
            before_final=wrapper.before_final,
            terminal=terminal,
        )

    async def _assert_failure_event_mutation_fails(
        self,
        mutation: _EventMutation,
    ) -> None:
        llm = outcome._SchemaLlm()
        self._install_llm(llm)
        worker = cast(Any, self.composition.worker)
        wrapper = outcome._EventMutatingOutcomeAuthority(
            worker._outcome_authority,
            mutation,
            self._side_effect_fingerprint,
        )
        worker._outcome_authority = wrapper
        stop, worker_task = await self._run_worker()
        try:
            accepted = cast(Any, await self._accept(self.failure_skill, 1))
            terminal = await self._await_terminal(accepted.command.command_id)
        finally:
            await self._stop_worker(stop, worker_task)
        self.assertEqual(wrapper.calls, 1)
        self.assertEqual(len(llm.requests), 2, "failed Run reached a final Provider")
        await self._assert_common_failed_closure(
            command_id=accepted.command.command_id,
            before_final=wrapper.before_final,
            terminal=terminal,
        )

    async def _assert_invalid_book_provider_fails(
        self,
        mutation: _DecisionMutation,
    ) -> None:
        llm = _InvalidBookLlm(mutation, self._side_effect_fingerprint)
        stop, worker_task = await self._one_failure_then_activate(llm)
        try:
            accepted = cast(Any, await self._accept(self.success_skill, 2))
            terminal = await self._await_terminal(accepted.command.command_id)
        finally:
            await self._stop_worker(stop, worker_task)
        expected_roles = [
            "xiaohutao",
            "xiaohutao",
            "teaching_agent",
            "xiaohutao",
            "xiaohutao",
            "book_agent",
            "book_agent",
        ]
        if llm.roles != expected_roles:
            self.fail(
                {
                    "actual_roles": llm.roles,
                    "expected_roles": expected_roles,
                    "sandbox_wall_ms": self.composition.settings.sandbox_wall_ms,
                    "terminal": terminal,
                    "commands": await self._command_job_diagnostics(),
                    "runs_and_invocations": await self._run_and_invocation_diagnostics(),
                    "sandbox_receipts": self._sandbox_receipt_diagnostics(),
                }
            )
        await self._assert_common_failed_closure(
            command_id=accepted.command.command_id,
            before_final=llm.before_final,
            terminal=terminal,
            expected_cause="DIRECTIVE_PROVIDER_OUTPUT_UNTRUSTED",
        )

    async def _run_and_invocation_diagnostics(self) -> dict[str, object]:
        connection = await self.database.connect(autocommit=True)
        try:
            run_cursor = await connection.execute(
                """
                SELECT run_id,command_id,skill_version_id,failure_key,task_success,
                       snapshot_json
                FROM yaya_runs ORDER BY created_at,run_id
                """
            )
            invocation_cursor = await connection.execute(
                """
                SELECT invocation_id,run_id,request_sha256,result_json
                FROM yaya_skill_invocations ORDER BY committed_at,invocation_id
                """
            )
            evidence_cursor = await connection.execute(
                """
                SELECT evidence_id,evidence_type,evidence_json
                FROM yaya_evidence ORDER BY recorded_at,evidence_id
                """
            )
            runs = list(await run_cursor.fetchall())
            invocations = list(await invocation_cursor.fetchall())
            evidence = list(await evidence_cursor.fetchall())
        finally:
            await connection.close()
        return {
            "runs": [dict(row) for row in runs],
            "invocations": [dict(row) for row in invocations],
            "evidence": [dict(row) for row in evidence],
        }

    def _sandbox_receipt_diagnostics(self) -> list[dict[str, object]]:
        result_root = self.artifact_root / ".sandbox-results"
        receipts: list[dict[str, object]] = []
        for path in sorted(result_root.rglob("*.json")):
            if path.name.endswith(".launch.json"):
                continue
            receipts.append(
                {
                    "path": path.relative_to(result_root).as_posix(),
                    "envelope": json.loads(path.read_text(encoding="utf-8")),
                }
            )
        return receipts

    async def _corrupt_prior_run_snapshot(
        self,
        database: PostgresDatabase,
        event: GameEvent,
        *,
        path: tuple[str, ...],
        replacement: object,
    ) -> None:
        connection = await database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                UPDATE yaya_runs SET snapshot_json=jsonb_set(
                  snapshot_json,%s::text[],%s::jsonb,false
                )
                WHERE command_id=(
                  SELECT command_id FROM yaya_runs
                  WHERE command_id<>%s AND task_success=FALSE
                  ORDER BY created_at,run_id LIMIT 1
                )
                """,
                (list(path), Jsonb(replacement), event.command_id),
            )
            self.assertEqual(cursor.rowcount, 1)
        finally:
            await connection.close()

    async def _failed_sandbox_evidence(self) -> EvidenceRef:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT snapshot_json FROM yaya_runs
                WHERE task_success=FALSE ORDER BY created_at,run_id LIMIT 1
                """
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            raise AssertionError("failed Run disappeared before Book evidence mutation")
        run = decode_as(row["snapshot_json"], RunResultSnapshot)
        return next(
            item for item in run.evidence_refs if item.evidence_type is EvidenceType.SANDBOX_LOG
        )

    async def test_task_completed_referencing_failed_run_fails_before_book_provider(
        self,
    ) -> None:
        await self._assert_failure_event_mutation_fails(
            lambda event: replace(
                event,
                event_type="task_completed",
                failure_count=0,
                failure_key=None,
            )
        )

    async def test_success_run_without_canonical_world_commit_fails_before_book_provider(
        self,
    ) -> None:
        async def remove_receipt(database: PostgresDatabase, event: GameEvent) -> None:
            connection = await database.connect(autocommit=True)
            try:
                cursor = await connection.execute(
                    """
                    UPDATE yaya_runs SET snapshot_json=jsonb_set(
                      snapshot_json,ARRAY['$fields','world_commit'],'null'::jsonb,false
                    ) WHERE command_id=%s
                    """,
                    (event.command_id,),
                )
                self.assertEqual(cursor.rowcount, 1)
            finally:
                await connection.close()

        await self._assert_success_database_mutation_fails(remove_receipt)

    async def test_world_receipt_noncontiguous_revision_fails_before_book_provider(
        self,
    ) -> None:
        async def skip_revision(database: PostgresDatabase, event: GameEvent) -> None:
            connection = await database.connect(autocommit=True)
            try:
                cursor = await connection.execute(
                    """
                    SELECT snapshot_json #>>
                      ARRAY['$fields','world_commit','$fields','previous_revision'] AS previous
                    FROM yaya_runs WHERE command_id=%s
                    """,
                    (event.command_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise AssertionError("successful Run disappeared")
                bad_revision = int(cast(str, row["previous"])) + 2
                updated = await connection.execute(
                    """
                    UPDATE yaya_runs SET snapshot_json=jsonb_set(
                      snapshot_json,
                      ARRAY['$fields','world_commit','$fields','world_revision'],
                      %s::jsonb,false
                    ) WHERE command_id=%s
                    """,
                    (Jsonb(bad_revision), event.command_id),
                )
                self.assertEqual(updated.rowcount, 1)
            finally:
                await connection.close()

        await self._assert_success_database_mutation_fails(skip_revision)

    async def test_session_run_history_cross_session_fails_before_book_provider(self) -> None:
        async def mutate(database: PostgresDatabase, event: GameEvent) -> None:
            await self._corrupt_prior_run_snapshot(
                database,
                event,
                path=("$fields", "session_id"),
                replacement="session_cross_scope_0001",
            )

        await self._assert_success_database_mutation_fails(mutate)

    async def test_session_run_history_cross_actor_fails_before_book_provider(self) -> None:
        async def mutate(database: PostgresDatabase, event: GameEvent) -> None:
            await self._corrupt_prior_run_snapshot(
                database,
                event,
                path=(
                    "$fields",
                    "request_context",
                    "$fields",
                    "actor",
                    "$fields",
                    "actor_id",
                ),
                replacement="student_cross_scope_0001",
            )

        await self._assert_success_database_mutation_fails(mutate)

    async def test_session_run_history_cross_content_fails_before_book_provider(self) -> None:
        async def mutate(database: PostgresDatabase, event: GameEvent) -> None:
            await self._corrupt_prior_run_snapshot(
                database,
                event,
                path=(
                    "$fields",
                    "request_context",
                    "$fields",
                    "content_ref",
                    "$fields",
                    "content_hash",
                ),
                replacement="f" * 64,
            )

        await self._assert_success_database_mutation_fails(mutate)

    async def test_session_run_history_cross_world_fails_before_book_provider(self) -> None:
        async def mutate(database: PostgresDatabase, event: GameEvent) -> None:
            await self._corrupt_prior_run_snapshot(
                database,
                event,
                path=("$fields", "world_id"),
                replacement="world_cross_scope_0001",
            )

        await self._assert_success_database_mutation_fails(mutate)

    async def test_skill_history_missing_current_skill_fails_before_book_provider(self) -> None:
        async def hide_current_skill(database: PostgresDatabase, event: GameEvent) -> None:
            connection = await database.connect(autocommit=True)
            try:
                cursor = await connection.execute(
                    """
                    UPDATE yaya_skills s SET skill_id='skill_missing_current_0001'
                    FROM yaya_runs r
                    WHERE r.command_id=%s
                      AND s.tenant_id=r.tenant_id
                      AND s.skill_version_id=r.skill_version_id
                    """,
                    (event.command_id,),
                )
                self.assertEqual(cursor.rowcount, 1)
            finally:
                await connection.close()

        await self._assert_success_database_mutation_fails(hide_current_skill)

    async def test_task_completed_evidence_not_owned_by_success_run_fails_before_provider(
        self,
    ) -> None:
        llm = outcome._SchemaLlm()
        self._install_llm(llm)
        stop, worker_task = await self._run_worker()
        try:
            accepted_failure = cast(Any, await self._accept(self.failure_skill, 1))
            failure_terminal = await self._await_terminal(accepted_failure.command.command_id)
            self.assertEqual(failure_terminal["status"], "REJECTED")
            wrong_evidence = await self._failed_sandbox_evidence()
            await role_live.AgentBackendRoleLiveE2E._activate_second_version(
                self,
                self.composition,
                self.success_skill,
                self.success_certified,
            )
            worker = cast(Any, self.composition.worker)
            wrapper = outcome._EventMutatingOutcomeAuthority(
                worker._outcome_authority,
                lambda event: replace(event, evidence_refs=(wrong_evidence,)),
                self._side_effect_fingerprint,
            )
            worker._outcome_authority = wrapper
            accepted = cast(Any, await self._accept(self.success_skill, 2))
            terminal = await self._await_terminal(accepted.command.command_id)
        finally:
            await self._stop_worker(stop, worker_task)
        self.assertEqual(wrapper.calls, 1)
        self.assertEqual(len(llm.requests), 5, "foreign Evidence reached Book Provider")
        await self._assert_common_failed_closure(
            command_id=accepted.command.command_id,
            before_final=wrapper.before_final,
            terminal=terminal,
        )

    async def test_success_run_disguised_as_run_failed_fails_before_final_provider(
        self,
    ) -> None:
        await self._assert_success_event_mutation_fails(
            lambda event: replace(
                event,
                event_type="run_failed",
                failure_count=1,
                failure_key="watering_loop_short",
            )
        )

    async def test_book_provider_wrong_role_exhausts_repair_without_publication(self) -> None:
        await self._assert_invalid_book_provider_fails(
            lambda decision: decision.__setitem__("role", "bug_agent")
        )

    async def test_book_provider_wrong_phase_exhausts_repair_without_publication(self) -> None:
        # Phase is deterministic policy state and is intentionally absent from
        # the closed model envelope.  A provider attempting to override it with
        # RECTIFICATION must therefore fail structural validation.
        await self._assert_invalid_book_provider_fails(
            lambda decision: decision.__setitem__("phase", "RECTIFICATION")
        )

    async def test_book_provider_wrong_response_type_exhausts_without_publication(
        self,
    ) -> None:
        def wrong_response(decision: dict[str, object]) -> None:
            decision["response_type"] = "question"
            decision["question"] = "Which answer should replace the canonical summary?"

        await self._assert_invalid_book_provider_fails(wrong_response)

    async def test_book_provider_permanent_mastery_message_is_never_published(self) -> None:
        await self._assert_invalid_book_provider_fails(
            lambda decision: decision.__setitem__(
                "message",
                "You have mastered forever and will never fail again.",
            )
        )

    async def test_book_provider_permanent_mastery_learner_reason_is_never_published(
        self,
    ) -> None:
        def permanent_reason(decision: dict[str, object]) -> None:
            inference = cast(dict[str, object], decision["learner_inference"])
            inference["reason"] = "You will never make another mistake."

        await self._assert_invalid_book_provider_fails(permanent_reason)


if __name__ == "__main__":
    unittest.main()
