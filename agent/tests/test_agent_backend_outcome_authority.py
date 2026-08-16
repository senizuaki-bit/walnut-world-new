from __future__ import annotations

import asyncio
import json
import stat
import sys
import tempfile
import unittest
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

import test_agent_backend_role_live_e2e as role_live  # noqa: E402
from agent_runtime_fixtures import make_operation, make_reply  # noqa: E402
from postgres_test_support import (  # noqa: E402
    postgres_test_server,
    reset_sandbox_recovery_results,
)
from psycopg.types.json import Jsonb  # noqa: E402
from test_agent_backend_docker_cpp_sandbox import (  # noqa: E402
    compile_linux,
    install_artifact,
)
from yaya_agent_backend.application import HttpAttempt  # noqa: E402
from yaya_agent_backend.codec import decode_as, encode, plain  # noqa: E402
from yaya_agent_backend.composition import create_production_composition  # noqa: E402
from yaya_agent_backend.database import PostgresDatabase  # noqa: E402
from yaya_agent_contracts import (  # noqa: E402
    CommandRecord,
    EvidenceRef,
    EvidenceType,
    LlmRequest,
    OperationContext,
    canonical_json_sha256,
)
from yaya_agent_runtime import (  # noqa: E402
    AgentTraceEvent,
    CommittedAgentTurn,
    GameEvent,
    RunResultSnapshot,
    SkillSnapshot,
)


class _SchemaLlm:
    """Provider-shaped deterministic LLM for provider-independent Worker tests."""

    def __init__(self, *, tool_lengths: tuple[int, ...] | None = None) -> None:
        self.requests: list[LlmRequest] = []
        self._tool_call_sequence = 0
        self._tool_lengths = tool_lengths

    async def generate(
        self,
        request: LlmRequest,
        context: OperationContext,
    ) -> object:
        del context
        self.requests.append(request)
        schema = cast(dict[str, object], plain(request.output_schema))
        raw_variants = schema.get("oneOf")
        variants = (
            cast(list[dict[str, object]], raw_variants)
            if isinstance(raw_variants, list)
            else [schema]
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
        first_properties = cast(dict[str, object], decision_variants[0]["properties"])
        role = cast(str, cast(dict[str, object], first_properties["role"])["const"])
        has_tool_variant = any(
            cast(dict[str, object], variant["properties"])["kind"]
            == {"type": "string", "const": "tool_calls"}
            for variant in variants
        )
        tool_result_seen = any(
            '"runtime_tool_result":true' in message.content for message in request.messages
        )
        if role == "xiaohutao" and has_tool_variant and not tool_result_seen:
            self._tool_call_sequence += 1
            length = 8
            if self._tool_lengths is not None:
                index = self._tool_call_sequence - 1
                if index >= len(self._tool_lengths):
                    raise AssertionError("provider-independent tool length sequence is exhausted")
                length = self._tool_lengths[index]
            return make_reply(
                {
                    "kind": "tool_calls",
                    "decision": None,
                    "tool_calls": [
                        {
                            "call_id": f"call_outcome_{self._tool_call_sequence:04d}",
                            "name": "invoke_skill",
                            "arguments": {
                                "skill_id": "bound_skill",
                                "arguments": {"length": length},
                            },
                        }
                    ],
                }
            )

        preferred_response = {
            "xiaohutao": "message",
            "teaching_agent": "question",
            "bug_agent": "question",
            "book_agent": "growth_summary",
        }[role]
        properties = next(
            (
                cast(dict[str, object], variant["properties"])
                for variant in decision_variants
                if (
                    cast(
                        dict[str, object],
                        cast(dict[str, object], variant["properties"])["response_type"],
                    ).get("const")
                    == preferred_response
                    or preferred_response
                    in cast(
                        list[str],
                        cast(
                            dict[str, object],
                            cast(dict[str, object], variant["properties"])["response_type"],
                        ).get("enum", []),
                    )
                )
            ),
            None,
        )
        if properties is None:
            raise AssertionError(
                f"role {role} schema rejects its production response {preferred_response}"
            )
        response_type = preferred_response
        learner_schema = cast(dict[str, object], properties["learner_inference"])
        inference: dict[str, object] | None = None
        if learner_schema.get("type") == "object":
            learner_properties = cast(dict[str, object], learner_schema["properties"])
            evidence_schema = cast(dict[str, object], learner_properties["evidence_ids"])
            prefix = cast(list[dict[str, object]], evidence_schema.get("prefixItems", []))
            inference = {
                "concept": cast(dict[str, object], learner_properties["concept"])["const"],
                "score_delta": 0.1 if role == "book_agent" else -0.1,
                "confidence": 0.8,
                "reason": "The cited Run evidence supports this bounded inference.",
                "evidence_ids": [item["const"] for item in prefix],
            }
        return make_reply(
            {
                "kind": "decision",
                "decision": {
                    "role": role,
                    "response_type": response_type,
                    "message": "This response is limited to the cited Run evidence.",
                    "question": (
                        "Which loop boundary does the evidence identify?"
                        if response_type == "question"
                        else None
                    ),
                    "hint_level": None,
                    "learner_inference": inference,
                    "skill_patch": None,
                    "requires_student_confirmation": False,
                },
                "tool_calls": [],
            }
        )


type _Mutation = Callable[[PostgresDatabase, GameEvent], Awaitable[None]]
type _EventMutation = Callable[[GameEvent], GameEvent]


class _MutatingOutcomeAuthority:
    def __init__(
        self,
        delegate: Any,
        database: PostgresDatabase,
        mutation: _Mutation,
        snapshot: Callable[[], Awaitable[dict[str, tuple[int, str]]]] | None = None,
    ) -> None:
        self._delegate = delegate
        self._database = database
        self._mutation = mutation
        self._snapshot = snapshot
        self.calls = 0
        self.error: BaseException | None = None
        self.before_final: dict[str, tuple[int, str]] | None = None

    async def derive(
        self,
        *,
        worker_id: str,
        lease_id: str,
        root_event: GameEvent,
        context: OperationContext,
    ) -> GameEvent:
        self.calls += 1
        await self._mutation(self._database, root_event)
        if self._snapshot is not None:
            self.before_final = await self._snapshot()
        try:
            return await self._delegate.derive(
                worker_id=worker_id,
                lease_id=lease_id,
                root_event=root_event,
                context=context,
            )
        except BaseException as error:
            self.error = error
            raise


class _EventMutatingOutcomeAuthority:
    def __init__(
        self,
        delegate: Any,
        mutation: _EventMutation,
        snapshot: Callable[[], Awaitable[dict[str, tuple[int, str]]]],
    ) -> None:
        self._delegate = delegate
        self._mutation = mutation
        self._snapshot = snapshot
        self.calls = 0
        self.before_final: dict[str, tuple[int, str]] | None = None
        self.canonical_event: GameEvent | None = None

    async def derive(
        self,
        *,
        worker_id: str,
        lease_id: str,
        root_event: GameEvent,
        context: OperationContext,
    ) -> GameEvent:
        self.calls += 1
        event = await self._delegate.derive(
            worker_id=worker_id,
            lease_id=lease_id,
            root_event=root_event,
            context=context,
        )
        self.canonical_event = event
        self.before_final = await self._snapshot()
        return self._mutation(event)


class AgentBackendOutcomeAuthorityTests(unittest.IsolatedAsyncioTestCase):
    _skill_snapshot = staticmethod(role_live.AgentBackendRoleLiveE2E._skill_snapshot)
    _certified = staticmethod(role_live.AgentBackendRoleLiveE2E._certified)

    @classmethod
    def setUpClass(cls) -> None:
        cls._artifact_context = tempfile.TemporaryDirectory(prefix="yaya-outcome-authority-")
        cls._server_context = postgres_test_server()
        cls._artifact_targets: list[Path] = []
        try:
            cls.root = Path(cls._artifact_context.__enter__()).resolve()
            build_root = cls.root / "build"
            cls.artifact_root = cls.root / "artifacts"
            build_root.mkdir()
            cls.artifact_root.mkdir()
            failure_executable = compile_linux(
                role_live._FAILURE_CPP,
                build_root,
                "watering_outcome_failure",
            )
            success_executable = compile_linux(
                role_live._SUCCESS_CPP,
                build_root,
                "watering_outcome_success",
            )
            raw_failure, failure_target = install_artifact(
                failure_executable,
                cls.artifact_root,
            )
            raw_success, success_target = install_artifact(
                success_executable,
                cls.artifact_root,
            )
            cls._artifact_targets.extend((failure_target, success_target))
            cls.failure_ref = role_live._versioned_ref(raw_failure, 1)
            cls.success_ref = role_live._versioned_ref(raw_success, 2)
            cls.server = cls._server_context.__enter__()
            cls.database = PostgresDatabase(cls.server.dsn)
            asyncio.run(cls.database.migrate())
        except BaseException:
            cls._server_context.__exit__(*sys.exc_info())
            for target in cls._artifact_targets:
                target.chmod(stat.S_IWRITE | stat.S_IREAD)
            cls._artifact_context.__exit__(*sys.exc_info())
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server_context.__exit__(None, None, None)
        for target in cls._artifact_targets:
            target.chmod(stat.S_IWRITE | stat.S_IREAD)
        cls._artifact_context.__exit__(None, None, None)

    async def asyncSetUp(self) -> None:
        await self._reset_database()
        await self._initialize_composition()

    async def _initialize_composition(self) -> None:
        settings = role_live.AgentBackendRoleLiveE2E._settings(
            self.server.dsn,
            self.artifact_root,
            endpoint="https://provider.invalid/v1/chat/completions",
            api_key="provider-independent-test-key",
            model="provider-independent-test-model",
            provider="provider-independent-test",
            thinking_mode=None,
        )
        self.composition = await create_production_composition(settings, migrate=False)
        (
            self.failure_skill,
            self.success_skill,
            self.success_certified,
        ) = await role_live.AgentBackendRoleLiveE2E._seed_initial_authority(
            self,
            self.composition,
            failed_ref=self.failure_ref,
            success_ref=self.success_ref,
        )
        self.actor = make_operation().actor

    async def _reset_database(self) -> None:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT tablename FROM pg_tables
                WHERE schemaname='public' AND tablename LIKE 'yaya_%'
                  AND tablename<>'yaya_schema_migrations'
                ORDER BY tablename
                """
            )
            tables = [cast(str, row["tablename"]) for row in await cursor.fetchall()]
            if not tables:
                self.fail("migrated PostgreSQL has no yaya business tables")
            quoted = ",".join(f'"{table}"' for table in tables)
            await connection.execute(f"TRUNCATE {quoted} CASCADE")
        finally:
            await connection.close()
        reset_sandbox_recovery_results(
            self.artifact_root / ".sandbox-results",
            owner_root=self.artifact_root,
        )

    def _install_llm(self, llm: _SchemaLlm) -> None:
        worker = cast(Any, self.composition.worker)
        worker._hub._runtime._llm = llm

    async def _accept(self, skill: SkillSnapshot, sequence: int) -> object:
        turn_id = f"turn_outcome_authority_{sequence:04d}"
        raw = role_live._turn_body(
            skill,
            turn_id=turn_id,
            client_turn_sequence=sequence,
        )
        body = cast(dict[str, object], json.loads(raw))
        return await self.composition.application.accept(
            actor=self.actor,
            attempt=HttpAttempt(
                request_id=f"req_outcome_{sequence:04d}",
                trace_id=f"trace_outcome_{sequence:04d}",
                correlation_id=f"corr_outcome_{sequence:04d}",
                requested_at=datetime.now(UTC),
            ),
            session_id="session_watering_0001",
            idempotency_key=f"agent-turn:outcome-authority:{sequence:04d}",
            raw_body=raw,
            body=body,
        )

    async def _await_terminal(self, command_id: str) -> Mapping[str, object]:
        deadline = asyncio.get_running_loop().time() + 45
        while asyncio.get_running_loop().time() < deadline:
            result = await self.composition.application.get_command(command_id, self.actor)
            if result.payload.get("terminal") is True:
                return result.payload
            await asyncio.sleep(0.05)
        worker_task = getattr(self, "_active_worker_task", None)
        worker_done = isinstance(worker_task, asyncio.Task) and worker_task.done()
        worker_exception = (
            repr(worker_task.exception())
            if isinstance(worker_task, asyncio.Task) and worker_done and not worker_task.cancelled()
            else None
        )
        self.fail(
            {
                "command_id": command_id,
                "worker_done": worker_done,
                "worker_exception": worker_exception,
                "commands": await self._command_job_diagnostics(),
            }
        )

    async def _command_job_diagnostics(self) -> list[dict[str, object]]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT c.command_id,c.client_turn_sequence,c.status AS column_status,
                       c.revision AS column_revision,c.record_json,
                       j.state AS job_state,j.worker_id,j.lease_id,j.last_error_code
                FROM yaya_commands c JOIN yaya_command_jobs j
                  ON j.tenant_id=c.tenant_id AND j.command_id=c.command_id
                ORDER BY c.client_turn_sequence,c.command_id
                """
            )
            rows = list(await cursor.fetchall())
        finally:
            await connection.close()
        result: list[dict[str, object]] = []
        for row in rows:
            record = decode_as(row["record_json"], CommandRecord)
            result.append(
                {
                    "command_id": row["command_id"],
                    "client_turn_sequence": row["client_turn_sequence"],
                    "column_status": row["column_status"],
                    "column_revision": row["column_revision"],
                    "record_status": record.status.value,
                    "record_revision": record.revision,
                    "record_terminal": record.terminal,
                    "job_state": row["job_state"],
                    "worker_id": row["worker_id"],
                    "lease_id": row["lease_id"],
                    "last_error_code": row["last_error_code"],
                }
            )
        return result

    async def _turns(self, command_id: str) -> list[CommittedAgentTurn]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT record_json FROM yaya_agent_turns
                WHERE record_json #>> '{$fields,event,$fields,command_id}'=%s
                ORDER BY event_id
                """,
                (command_id,),
            )
            rows = list(await cursor.fetchall())
        finally:
            await connection.close()
        return [decode_as(row["record_json"], CommittedAgentTurn) for row in rows]

    async def _publication_counts(self) -> dict[str, int]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT
                  (SELECT count(*)::int FROM yaya_agent_messages) AS messages,
                  (SELECT count(*)::int FROM yaya_agent_interactions) AS interactions,
                  (SELECT count(*)::int FROM yaya_events) AS events,
                  (SELECT count(*)::int FROM yaya_projection_outbox) AS projection_outbox,
                  (SELECT count(*)::int FROM yaya_outbox) AS worker_outbox,
                  (SELECT count(*)::int FROM yaya_learner_projection_jobs) AS learner_jobs
                """
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            raise AssertionError("PostgreSQL publication count query returned no row")
        return {key: cast(int, value) for key, value in row.items()}

    async def _side_effect_fingerprint(self) -> dict[str, tuple[int, str]]:
        tables = (
            "yaya_runs",
            "yaya_evidence",
            "yaya_skill_invocations",
            "yaya_worlds",
            "yaya_agent_messages",
            "yaya_agent_interactions",
            "yaya_events",
            "yaya_projection_outbox",
            "yaya_outbox",
            "yaya_learner_models",
            "yaya_learner_projection_jobs",
            "yaya_learner_projection_receipts",
            "yaya_learner_projection_failures",
        )
        connection = await self.database.connect(autocommit=True)
        try:
            result: dict[str, tuple[int, str]] = {}
            for table in tables:
                cursor = await connection.execute(
                    f"""
                    SELECT count(*)::int AS count,
                           md5(COALESCE(string_agg(value::text,'' ORDER BY value::text),'')) AS hash
                    FROM (SELECT to_jsonb(t) AS value FROM {table} t) rows
                    """
                )
                row = await cursor.fetchone()
                if row is None:
                    raise AssertionError(f"fingerprint query returned no row for {table}")
                result[table] = (cast(int, row["count"]), cast(str, row["hash"]))
            return result
        finally:
            await connection.close()

    async def _projection_destinations(self) -> dict[str, int]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT destination,count(*)::int AS count
                FROM yaya_projection_outbox GROUP BY destination ORDER BY destination
                """
            )
            rows = list(await cursor.fetchall())
        finally:
            await connection.close()
        return {cast(str, row["destination"]): cast(int, row["count"]) for row in rows}

    async def _job_state(self, command_id: str) -> str:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                "SELECT state FROM yaya_command_jobs WHERE command_id=%s",
                (command_id,),
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            raise AssertionError("Command Job disappeared")
        return cast(str, row["state"])

    async def _trace_diagnostics(self, turn_id: str) -> list[tuple[str, object]]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT trace_json FROM yaya_agent_traces
                WHERE turn_id=%s ORDER BY trace_record_id
                """,
                (turn_id,),
            )
            rows = list(await cursor.fetchall())
        finally:
            await connection.close()
        events = [decode_as(row["trace_json"], AgentTraceEvent) for row in rows]
        return [(event.name, plain(event.fields)) for event in events]

    async def _success_authority_diagnostics(self, command_id: str) -> dict[str, object]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT r.snapshot_json,r.wire_json,w.state_hash,w.state_json
                FROM yaya_runs r
                JOIN yaya_worlds w
                  ON w.tenant_id=r.tenant_id AND w.world_id=r.world_id
                WHERE r.command_id=%s
                """,
                (command_id,),
            )
            rows = list(await cursor.fetchall())
            if len(rows) != 1:
                raise AssertionError("successful Command has no unique durable Run diagnostics")
            run = decode_as(rows[0]["snapshot_json"], RunResultSnapshot)
            evidence_cursor = await connection.execute(
                """
                SELECT evidence_type,payload_sha256,evidence_json
                FROM yaya_evidence WHERE evidence_id=ANY(%s)
                ORDER BY evidence_type,evidence_id
                """,
                ([item.evidence_id for item in run.evidence_refs],),
            )
            evidence_rows = list(await evidence_cursor.fetchall())
        finally:
            await connection.close()
        wire = cast(Mapping[str, object], rows[0]["wire_json"])
        world_application = cast(Mapping[str, object], wire["world_application"])
        return {
            "typed_receipt": plain(run.world_commit),
            "wire_receipt": world_application["receipt"],
            "world_row_state_hash": rows[0]["state_hash"],
            "canonical_world_state_hash": canonical_json_sha256(
                cast(Mapping[str, object], rows[0]["state_json"])
            ),
            "evidence": [
                {
                    "evidence_type": row["evidence_type"],
                    "payload_sha256": row["payload_sha256"],
                    "document": row["evidence_json"],
                }
                for row in evidence_rows
            ],
        }

    @staticmethod
    def _exception_chain(error: BaseException | None) -> list[dict[str, object]]:
        chain: list[dict[str, object]] = []
        current = error
        while current is not None:
            chain.append(
                {
                    "type": type(current).__name__,
                    "message": str(current),
                    "code": getattr(current, "code", None),
                    "details": plain(getattr(current, "details", {})),
                }
            )
            current = current.__cause__
        return chain

    async def _run_worker(self) -> tuple[asyncio.Event, asyncio.Task[None]]:
        stop = asyncio.Event()
        task = asyncio.create_task(self.composition.worker.run_forever(stop))
        self._active_worker_task = task
        return stop, task

    @staticmethod
    async def _stop_worker(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    async def test_worker_derives_exact_one_two_three_failure_streak_then_success(self) -> None:
        llm = _SchemaLlm()
        self._install_llm(llm)
        stop, worker_task = await self._run_worker()
        try:
            expected_roles = ("teaching_agent", "teaching_agent", "bug_agent")
            for sequence, expected_role in enumerate(expected_roles, start=1):
                accepted = cast(Any, await self._accept(self.failure_skill, sequence))
                terminal = await self._await_terminal(accepted.command.command_id)
                self.assertEqual(terminal["status"], "REJECTED")
                turns = await self._turns(accepted.command.command_id)
                self.assertEqual(len(turns), 2)
                root = [turn for turn in turns if turn.event.event_type == "run_skill_requested"]
                final = [turn for turn in turns if turn.event.event_type == "run_failed"]
                self.assertEqual(len(root), 1)
                self.assertEqual(len(final), 1)
                self.assertEqual(root[0].route.role, "xiaohutao")
                self.assertEqual(final[0].event.failure_count, sequence)
                self.assertEqual(final[0].event.failure_key, "watering_loop_short")
                self.assertEqual(final[0].route.role, expected_role)
                self.assertEqual(
                    final[0].decision.source,
                    "provider",
                    await self._trace_diagnostics(final[0].event.turn_id),
                )
                self.assertFalse(final[0].decision.degraded)
                self.assertEqual(await self._job_state(accepted.command.command_id), "DONE")

            await role_live.AgentBackendRoleLiveE2E._activate_second_version(
                self,
                self.composition,
                self.success_skill,
                self.success_certified,
            )

            async def no_mutation(database: PostgresDatabase, event: GameEvent) -> None:
                del database, event

            worker = cast(Any, self.composition.worker)
            success_authority = _MutatingOutcomeAuthority(
                worker._outcome_authority,
                self.database,
                no_mutation,
            )
            worker._outcome_authority = success_authority
            accepted = cast(Any, await self._accept(self.success_skill, 4))
            terminal = await self._await_terminal(accepted.command.command_id)
            if terminal["status"] != "APPLIED":
                self.fail(
                    {
                        "terminal": terminal,
                        "authority_error_chain": self._exception_chain(success_authority.error),
                        "run_world_evidence": await self._success_authority_diagnostics(
                            accepted.command.command_id
                        ),
                    }
                )
            turns = await self._turns(accepted.command.command_id)
            self.assertEqual(len(turns), 2)
            final = [turn for turn in turns if turn.event.event_type == "task_completed"]
            self.assertEqual(len(final), 1)
            self.assertEqual(final[0].route.role, "book_agent")
            self.assertEqual(final[0].decision.source, "provider")
            self.assertFalse(final[0].decision.degraded)
            self.assertEqual(await self._job_state(accepted.command.command_id), "DONE")
        finally:
            await self._stop_worker(stop, worker_task)
        self.assertEqual(len(llm.requests), 12)
        counts = await self._publication_counts()
        self.assertEqual(
            counts,
            {
                "messages": 4,
                "interactions": 4,
                "events": 9,
                "projection_outbox": 13,
                "worker_outbox": 0,
                "learner_jobs": 4,
            },
        )
        # Every public final emits feedback, Product, and Learner projection
        # messages; the one committed World event is the thirteenth message.
        self.assertEqual(
            await self._projection_destinations(),
            {
                "agent_feedback_events": 4,
                "learner_projection_events": 4,
                "product_agent_interactions": 4,
                "world_events": 1,
            },
        )

    async def _assert_current_tamper_fails_before_final(
        self,
        mutation: _Mutation,
    ) -> None:
        llm = _SchemaLlm()
        self._install_llm(llm)
        worker = cast(Any, self.composition.worker)
        wrapper = _MutatingOutcomeAuthority(
            worker._outcome_authority,
            self.database,
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
        self.assertEqual(terminal["status"], "FAILED")
        error = cast(Mapping[str, object], terminal["error"])
        details = cast(Mapping[str, object], error["details"])
        self.assertEqual(details["cause_code"], "AGENT_OUTCOME_INVARIANT_VIOLATION")
        self.assertEqual(wrapper.calls, 1)
        self.assertIsNotNone(wrapper.before_final)
        self.assertEqual(len(llm.requests), 2, "final role Provider must not be called")
        self.assertEqual(await self._side_effect_fingerprint(), wrapper.before_final)
        turns = await self._turns(accepted.command.command_id)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].event.event_type, "run_skill_requested")
        self.assertEqual(await self._job_state(accepted.command.command_id), "DONE")
        self.assertEqual(
            await self._publication_counts(),
            {
                "messages": 0,
                "interactions": 0,
                "events": 0,
                "projection_outbox": 0,
                "worker_outbox": 0,
                "learner_jobs": 0,
            },
        )

    async def _assert_derived_event_tamper_fails_before_final(
        self,
        mutation: _EventMutation,
    ) -> None:
        llm = _SchemaLlm()
        self._install_llm(llm)
        worker = cast(Any, self.composition.worker)
        wrapper = _EventMutatingOutcomeAuthority(
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
        self.assertEqual(terminal["status"], "FAILED")
        self.assertEqual(wrapper.calls, 1)
        self.assertIsNotNone(wrapper.before_final)
        self.assertEqual(len(llm.requests), 2, "corrupt derived fact reached final Provider")
        self.assertEqual(await self._side_effect_fingerprint(), wrapper.before_final)
        turns = await self._turns(accepted.command.command_id)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].event.event_type, "run_skill_requested")
        self.assertEqual(await self._job_state(accepted.command.command_id), "DONE")

    async def test_derived_failure_fact_corruption_matrix_fails_before_final_provider(
        self,
    ) -> None:
        wrong_evidence = EvidenceRef(
            "evidence_wrong_run_0001",
            EvidenceType.SANDBOX_LOG,
            datetime.now(UTC),
            sha256="f" * 64,
        )
        cases: tuple[tuple[str, _EventMutation], ...] = (
            (
                "claimed_three_with_only_one_canonical_failure",
                lambda event: replace(event, failure_count=3),
            ),
            (
                "failure_key_differs_from_current_run",
                lambda event: replace(event, failure_key="different_failure_key"),
            ),
            (
                "evidence_belongs_to_no_canonical_run",
                lambda event: replace(event, evidence_refs=(wrong_evidence,)),
            ),
            (
                "failed_run_disguised_as_task_completed",
                lambda event: replace(
                    event,
                    event_type="task_completed",
                    failure_count=0,
                    failure_key=None,
                ),
            ),
            (
                "cross_session_derived_event",
                lambda event: replace(event, session_id="session_cross_scope_0001"),
            ),
            (
                "cross_skill_derived_event",
                lambda event: replace(event, skill_ref=self.success_ref),
            ),
            (
                "cross_actor_derived_event",
                lambda event: replace(event, student_id="student_cross_scope_0001"),
            ),
            (
                "cross_world_revision_derived_event",
                lambda event: replace(
                    event,
                    expected_world_revision=event.expected_world_revision + 1,
                ),
            ),
        )
        for index, (name, mutation) in enumerate(cases):
            with self.subTest(name=name):
                if index:
                    await self._reset_database()
                    await self._initialize_composition()
                await self._assert_derived_event_tamper_fails_before_final(mutation)

    async def test_missing_current_invocation_fails_before_final_provider(self) -> None:
        async def delete_invocation(database: PostgresDatabase, event: GameEvent) -> None:
            connection = await database.connect(autocommit=True)
            try:
                await connection.execute(
                    """
                    DELETE FROM yaya_skill_invocations
                    WHERE run_id=(SELECT run_id FROM yaya_runs WHERE command_id=%s)
                    """,
                    (event.command_id,),
                )
            finally:
                await connection.close()

        await self._assert_current_tamper_fails_before_final(delete_invocation)

    async def test_current_world_authority_drift_fails_before_final_provider(self) -> None:
        async def corrupt_world_hash(database: PostgresDatabase, event: GameEvent) -> None:
            connection = await database.connect(autocommit=True)
            try:
                await connection.execute(
                    """
                    UPDATE yaya_worlds SET state_hash=%s
                    WHERE world_id=(SELECT world_id FROM yaya_runs WHERE command_id=%s)
                    """,
                    ("f" * 64, event.command_id),
                )
            finally:
                await connection.close()

        await self._assert_current_tamper_fails_before_final(corrupt_world_hash)

    async def test_third_failure_with_broken_prior_evidence_never_calls_bug_provider(self) -> None:
        llm = _SchemaLlm()
        self._install_llm(llm)
        stop, worker_task = await self._run_worker()
        try:
            prior_command_ids: list[str] = []
            for sequence in (1, 2):
                accepted = cast(Any, await self._accept(self.failure_skill, sequence))
                terminal = await self._await_terminal(accepted.command.command_id)
                self.assertEqual(terminal["status"], "REJECTED")
                prior_command_ids.append(accepted.command.command_id)
            baseline = await self._publication_counts()

            async def delete_first_evidence(
                database: PostgresDatabase,
                event: GameEvent,
            ) -> None:
                del event
                connection = await database.connect(autocommit=True)
                try:
                    await connection.execute(
                        """
                        UPDATE yaya_evidence
                        SET evidence_json=jsonb_set(
                          evidence_json,'{payload,run_id}',
                          '"run_tampered_prior_0001"'::jsonb,false
                        )
                        WHERE evidence_json #>> '{source,command_id}'=%s
                        """,
                        (prior_command_ids[0],),
                    )
                finally:
                    await connection.close()

            worker = cast(Any, self.composition.worker)
            wrapper = _MutatingOutcomeAuthority(
                worker._outcome_authority,
                self.database,
                delete_first_evidence,
                self._side_effect_fingerprint,
            )
            worker._outcome_authority = wrapper
            accepted = cast(Any, await self._accept(self.failure_skill, 3))
            terminal = await self._await_terminal(accepted.command.command_id)
        finally:
            await self._stop_worker(stop, worker_task)

        self.assertEqual(terminal["status"], "FAILED")
        self.assertEqual(wrapper.calls, 1)
        self.assertIsNotNone(wrapper.before_final)
        self.assertEqual(len(llm.requests), 8, "third final Bug Provider must not be called")
        turns = await self._turns(accepted.command.command_id)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].event.event_type, "run_skill_requested")
        self.assertEqual(await self._side_effect_fingerprint(), wrapper.before_final)
        self.assertEqual(await self._publication_counts(), baseline)

    async def _assert_third_failure_history_snapshot_tamper_fails(
        self,
        *,
        path: tuple[str, ...],
        duplicate_field: str | None = None,
        replacement: object | None = None,
    ) -> None:
        llm = _SchemaLlm()
        self._install_llm(llm)
        stop, worker_task = await self._run_worker()
        try:
            prior_command_ids: list[str] = []
            for sequence in (1, 2):
                accepted = cast(Any, await self._accept(self.failure_skill, sequence))
                terminal = await self._await_terminal(accepted.command.command_id)
                self.assertEqual(terminal["status"], "REJECTED")
                prior_command_ids.append(accepted.command.command_id)
            baseline = await self._publication_counts()

            async def corrupt_first_history_snapshot(
                database: PostgresDatabase,
                event: GameEvent,
            ) -> None:
                del event
                connection = await database.connect(autocommit=True)
                try:
                    duplicate_value = replacement
                    if duplicate_field is not None:
                        cursor = await connection.execute(
                            """
                            SELECT snapshot_json FROM yaya_runs WHERE command_id=%s
                            """,
                            (prior_command_ids[1],),
                        )
                        row = await cursor.fetchone()
                        if row is None:
                            raise AssertionError("second canonical Run disappeared")
                        duplicate_value = getattr(
                            decode_as(row["snapshot_json"], RunResultSnapshot),
                            duplicate_field,
                        )
                    cursor = await connection.execute(
                        """
                        UPDATE yaya_runs
                        SET snapshot_json=jsonb_set(
                          snapshot_json,%s::text[],%s::jsonb,false
                        )
                        WHERE command_id=%s
                        """,
                        (
                            list(path),
                            Jsonb(duplicate_value),
                            prior_command_ids[0],
                        ),
                    )
                    self.assertEqual(cursor.rowcount, 1)
                finally:
                    await connection.close()

            worker = cast(Any, self.composition.worker)
            wrapper = _MutatingOutcomeAuthority(
                worker._outcome_authority,
                self.database,
                corrupt_first_history_snapshot,
                self._side_effect_fingerprint,
            )
            worker._outcome_authority = wrapper
            accepted = cast(Any, await self._accept(self.failure_skill, 3))
            terminal = await self._await_terminal(accepted.command.command_id)
        finally:
            await self._stop_worker(stop, worker_task)

        self.assertEqual(terminal["status"], "FAILED")
        error = cast(Mapping[str, object], terminal["error"])
        details = cast(Mapping[str, object], error["details"])
        self.assertEqual(details["cause_code"], "AGENT_OUTCOME_INVARIANT_VIOLATION")
        self.assertEqual(wrapper.calls, 1)
        self.assertIsNotNone(wrapper.before_final)
        self.assertEqual(len(llm.requests), 8, "corrupt history reached Bug Provider")
        turns = await self._turns(accepted.command.command_id)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].event.event_type, "run_skill_requested")
        self.assertEqual(await self._job_state(accepted.command.command_id), "DONE")
        self.assertEqual(await self._side_effect_fingerprint(), wrapper.before_final)
        self.assertEqual(await self._publication_counts(), baseline)

    async def test_third_failure_history_rejects_cross_scope_and_duplicate_identity(
        self,
    ) -> None:
        cases: tuple[tuple[str, tuple[str, ...], str | None, object | None], ...] = (
            (
                "prior_run_crosses_session",
                ("$fields", "session_id"),
                None,
                "session_cross_scope_0001",
            ),
            (
                "prior_run_crosses_skill",
                ("$fields", "skill_ref", "$fields", "skill_version_id"),
                None,
                "skill_version_cross_scope_0001",
            ),
            (
                "prior_run_crosses_actor",
                (
                    "$fields",
                    "request_context",
                    "$fields",
                    "actor",
                    "$fields",
                    "actor_id",
                ),
                None,
                "student_cross_scope_0001",
            ),
            (
                "prior_run_crosses_pinned_content_hash",
                (
                    "$fields",
                    "request_context",
                    "$fields",
                    "content_ref",
                    "$fields",
                    "content_hash",
                ),
                None,
                "b" * 64,
            ),
            (
                "prior_run_crosses_world",
                ("$fields", "world_id"),
                None,
                "world_cross_scope_0001",
            ),
            (
                "failure_history_duplicates_run_identity",
                ("$fields", "run_id"),
                "run_id",
                None,
            ),
            (
                "failure_history_duplicates_turn_identity",
                ("$fields", "turn_id"),
                "turn_id",
                None,
            ),
            (
                "failure_history_duplicates_command_identity",
                ("$fields", "command_id"),
                "command_id",
                None,
            ),
        )
        for index, (name, path, duplicate_field, replacement) in enumerate(cases):
            with self.subTest(name=name):
                if index:
                    await self._reset_database()
                    await self._initialize_composition()
                await self._assert_third_failure_history_snapshot_tamper_fails(
                    path=path,
                    duplicate_field=duplicate_field,
                    replacement=replacement,
                )

    async def test_claimed_third_failure_with_mixed_canonical_failure_keys_fails_before_bug_provider(
        self,
    ) -> None:
        llm = _SchemaLlm(tool_lengths=(8, 0, 8))
        self._install_llm(llm)
        relaxed_failure_skill = replace(
            self.failure_skill,
            parameter_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["length"],
                "properties": {"length": {"type": "integer", "enum": [0, 8]}},
            },
        )
        self.assertEqual(relaxed_failure_skill.ref, self.failure_skill.ref)
        self.assertEqual(relaxed_failure_skill.source_code, self.failure_skill.source_code)
        self.assertEqual(relaxed_failure_skill.source_sha256, self.failure_skill.source_sha256)
        self.assertEqual(relaxed_failure_skill.entrypoint, self.failure_skill.entrypoint)
        connection = await self.database.connect(autocommit=True)
        try:
            updated = await connection.execute(
                """
                UPDATE yaya_skills SET snapshot_json=%s
                WHERE tenant_id=%s AND actor_id=%s AND content_hash=%s
                  AND skill_id=%s AND skill_version_id=%s AND active=TRUE
                """,
                (
                    Jsonb(encode(relaxed_failure_skill)),
                    self.actor.tenant_id,
                    self.actor.actor_id,
                    relaxed_failure_skill.request_context.content_ref.content_hash,
                    relaxed_failure_skill.ref.skill_id,
                    relaxed_failure_skill.ref.skill_version_id,
                ),
            )
            self.assertEqual(updated.rowcount, 1)
        finally:
            await connection.close()

        stop, worker_task = await self._run_worker()
        try:
            first = cast(Any, await self._accept(relaxed_failure_skill, 1))
            self.assertEqual(
                (await self._await_terminal(first.command.command_id))["status"],
                "REJECTED",
            )
            second = cast(Any, await self._accept(relaxed_failure_skill, 2))
            self.assertEqual(
                (await self._await_terminal(second.command.command_id))["status"],
                "REJECTED",
            )

            baseline = await self._publication_counts()
            self.assertEqual(
                baseline,
                {
                    "messages": 2,
                    "interactions": 2,
                    "events": 4,
                    "projection_outbox": 6,
                    "worker_outbox": 0,
                    "learner_jobs": 2,
                },
            )
            worker = cast(Any, self.composition.worker)
            wrapper = _EventMutatingOutcomeAuthority(
                worker._outcome_authority,
                lambda event: replace(event, failure_count=3),
                self._side_effect_fingerprint,
            )
            worker._outcome_authority = wrapper
            third = cast(Any, await self._accept(relaxed_failure_skill, 3))
            terminal = await self._await_terminal(third.command.command_id)
        finally:
            await self._stop_worker(stop, worker_task)

        self.assertEqual(terminal["status"], "FAILED")
        error = cast(Mapping[str, object], terminal["error"])
        details = cast(Mapping[str, object], error["details"])
        self.assertEqual(details["cause_code"], "CONTEXT_FAILURE_HISTORY_COUNT_MISMATCH")
        self.assertEqual(wrapper.calls, 1)
        self.assertIsNotNone(wrapper.canonical_event)
        assert wrapper.canonical_event is not None
        self.assertEqual(wrapper.canonical_event.failure_count, 1)
        self.assertIsNotNone(wrapper.before_final)
        self.assertEqual(len(llm.requests), 8, "mixed failure keys reached Bug Provider")
        turns = await self._turns(third.command.command_id)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].event.event_type, "run_skill_requested")
        self.assertEqual(await self._job_state(third.command.command_id), "DONE")
        self.assertEqual(await self._side_effect_fingerprint(), wrapper.before_final)
        self.assertEqual(await self._publication_counts(), baseline)

        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT r.failure_key,r.snapshot_json,r.wire_json
                FROM yaya_runs r JOIN yaya_commands c
                  ON c.tenant_id=r.tenant_id AND c.command_id=r.command_id
                ORDER BY c.client_turn_sequence
                """
            )
            rows = list(await cursor.fetchall())
        finally:
            await connection.close()
        snapshots = [decode_as(row["snapshot_json"], RunResultSnapshot) for row in rows]
        expected_keys = (
            "watering_loop_short",
            "sandbox_execution_failed",
            "watering_loop_short",
        )
        self.assertEqual(tuple(row["failure_key"] for row in rows), expected_keys)
        self.assertEqual(tuple(run.failure_key for run in snapshots), expected_keys)
        self.assertEqual(
            tuple(run.skill_ref for run in snapshots),
            (relaxed_failure_skill.ref,) * 3,
        )
        first_run = snapshots[0]
        self.assertEqual(
            tuple(run.session_id for run in snapshots),
            (first_run.session_id,) * 3,
        )
        self.assertEqual(
            tuple(run.world_id for run in snapshots),
            (first_run.world_id,) * 3,
        )
        self.assertEqual(
            tuple(run.request_context.actor for run in snapshots),
            (first_run.request_context.actor,) * 3,
        )
        self.assertEqual(
            tuple(run.request_context.content_ref for run in snapshots),
            (first_run.request_context.content_ref,) * 3,
        )
        wire_reasons = tuple(
            cast(
                Mapping[str, object],
                cast(Mapping[str, object], row["wire_json"])["world_application"],
            )["failure"]
            for row in rows
        )
        self.assertEqual(
            tuple(
                cast(Mapping[str, object], cast(Mapping[str, object], item)["details"])["reason"]
                for item in wire_reasons
            ),
            ("watering_loop_short", "EMPTY_ACTION_TRACE", "watering_loop_short"),
        )


if __name__ == "__main__":
    unittest.main()
