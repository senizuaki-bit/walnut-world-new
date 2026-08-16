from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import sys
import threading
import unittest
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, LiteralString, cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
CONTRACTS_ROOT = Path(__file__).resolve().parents[1] / "contracts"
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

import psycopg  # noqa: E402
from agent_runtime_fixtures import (  # noqa: E402
    SESSION_ID,
    TASK_ID,
    WORLD_ID,
    make_agent_decision,
    make_event,
    make_operation,
    make_session,
    make_skill,
    make_task,
    make_versions,
    make_world_state,
)
from postgres_test_support import postgres_test_server  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from yaya_agent_backend.auth import JwtAuthenticator  # noqa: E402
from yaya_agent_backend.codec import encode, plain  # noqa: E402
from yaya_agent_backend.database import PostgresDatabase  # noqa: E402
from yaya_agent_backend.http_api import serve_http  # noqa: E402
from yaya_agent_backend.product_application import (  # noqa: E402
    ProductInteractionReadApplication,
)
from yaya_agent_backend.product_http_api import ProductHttpApi  # noqa: E402
from yaya_agent_backend.product_repositories import (  # noqa: E402
    PostgresProductInteractionReadRepository,
    ProductReadDependencyError,
    ProductReadInvariantError,
    ProductReadNotFoundError,
)
from yaya_agent_backend.repositories import (  # noqa: E402
    PostgresAgentTurnRepository,
    RepositoryAuthorityError,
)
from yaya_agent_backend.wire import ContractSchemaValidator  # noqa: E402
from yaya_agent_contracts import (  # noqa: E402
    ActorType,
    CommandRecord,
    CommandStatus,
    ContractError,
    ErrorCategory,
    EvidenceRef,
    EvidenceType,
    NewCommand,
    OperationContext,
    RequestContext,
    RuntimeEvent,
    RuntimeEventType,
    WorldCommitReceipt,
    canonical_json_sha256,
)
from yaya_agent_runtime import (  # noqa: E402
    AgentDecision,
    GameEvent,
    RoleRoute,
    RunResultSnapshot,
    TeachingPhase,
    world_commit_receipt_sha256,
)
from yaya_agent_runtime.errors import AgentPersistenceError  # noqa: E402


def _request_context(context: OperationContext) -> RequestContext:
    return RequestContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        trace_id=context.trace_id,
        requested_at=context.requested_at,
        actor=context.actor,
        content_ref=context.content_ref,
    )


@dataclass(frozen=True, slots=True)
class _PreparedTurn:
    index: int
    context: OperationContext
    event: GameEvent
    command: CommandRecord
    claim_id: str


class _HighWatermarkGateConnection:
    """Pause a Product read after PostgreSQL has fixed and queried its snapshot tip."""

    def __init__(
        self,
        connection: psycopg.AsyncConnection[dict[str, object]],
        high_watermark_read: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self._connection = connection
        self._high_watermark_read = high_watermark_read
        self._release = release
        self._paused = False

    async def execute(self, query: Any, params: Any = None) -> Any:
        cursor = await self._connection.execute(query, params)
        if not self._paused and "COUNT(*)::bigint AS row_count" in str(query):
            self._paused = True
            self._high_watermark_read.set()
            await self._release.wait()
        return cursor


class _HighWatermarkGateDatabase(PostgresDatabase):
    def __init__(
        self,
        dsn: str,
        high_watermark_read: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        super().__init__(dsn)
        self._high_watermark_read = high_watermark_read
        self._release = release

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[Any]:
        async with super().transaction() as connection:
            yield _HighWatermarkGateConnection(
                connection,
                self._high_watermark_read,
                self._release,
            )


class ProductInteractionPostgresRepositoryTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._server_context = postgres_test_server()
        try:
            cls.server = cls._server_context.__enter__()
            cls.database = PostgresDatabase(cls.server.dsn)
            asyncio.run(cls.database.migrate())
        except BaseException:
            cls._server_context.__exit__(*sys.exc_info())
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server_context.__exit__(None, None, None)

    async def asyncSetUp(self) -> None:
        await self._reset_database()
        self.turns = PostgresAgentTurnRepository(self.database, CONTRACTS_ROOT)
        self.validator = ContractSchemaValidator(CONTRACTS_ROOT)
        self.product = PostgresProductInteractionReadRepository(
            self.database,
            self.validator,
        )
        self.base_context = make_operation(command_id="cmd_product_0000")
        await self._seed_authority(self.base_context)

    async def _reset_database(self) -> None:
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                DROP TRIGGER IF EXISTS yaya_test_fail_product_sequence
                ON yaya_projection_outbox
                """
            )
            await connection.execute("DROP FUNCTION IF EXISTS yaya_test_fail_product_sequence()")
            await connection.execute(
                """
                TRUNCATE yaya_agent_turns,yaya_agent_interactions,
                  yaya_projection_outbox,yaya_agent_messages,yaya_events,
                  yaya_learner_projection_failures,
                  yaya_learner_projection_receipts,
                  yaya_learner_projection_job_evidence,
                  yaya_learner_projection_jobs,yaya_learner_models,
                  yaya_evidence,yaya_runs,yaya_command_jobs,yaya_commands,
                  yaya_registry_active,yaya_registry_certifications,yaya_skills,
                  yaya_agent_sessions,yaya_worlds,yaya_tasks CASCADE
                """
            )
        finally:
            await connection.close()

    async def _seed_authority(self, context: OperationContext) -> None:
        task = make_task(context)
        session = make_session(operation=context)
        skill = make_skill(context)
        state = make_world_state()
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                INSERT INTO yaya_tasks(tenant_id,task_id,actor_id,content_hash,snapshot_json)
                VALUES (%s,%s,%s,%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    task.task_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    Jsonb(encode(task)),
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_worlds(
                  tenant_id,world_id,actor_id,content_hash,stream_id,revision,
                  last_event_sequence,state_hash,world_rules_version,state_json,
                  request_context_json
                ) VALUES (%s,%s,%s,%s,%s,5,40,%s,'farm-rules-1',%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    WORLD_ID,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    f"world:{WORLD_ID}",
                    canonical_json_sha256(state),
                    Jsonb(state),
                    Jsonb(encode(_request_context(context))),
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_agent_sessions(
                  tenant_id,session_id,actor_id,task_id,world_id,content_hash,snapshot_json
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    SESSION_ID,
                    context.actor.actor_id,
                    TASK_ID,
                    WORLD_ID,
                    context.content_ref.content_hash,
                    Jsonb(encode(session)),
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_skills(
                  tenant_id,skill_id,skill_version_id,certification_id,actor_id,
                  session_id,content_hash,artifact_sha256,snapshot_json,active
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE)
                """,
                (
                    context.actor.tenant_id,
                    skill.ref.skill_id,
                    skill.ref.skill_version_id,
                    skill.ref.certification_id,
                    context.actor.actor_id,
                    SESSION_ID,
                    context.content_ref.content_hash,
                    skill.ref.artifact_sha256,
                    Jsonb(encode(skill)),
                ),
            )
        finally:
            await connection.close()

    def _turn_identity(self, index: int) -> tuple[OperationContext, GameEvent]:
        suffix = f"{index:04d}"
        command_id = f"cmd_product_{suffix}"
        context = replace(
            make_operation(command_id=command_id),
            request_id=f"req_product_{suffix}",
            correlation_id=f"corr_product_{suffix}",
            trace_id=f"trace_product_{suffix}",
        )
        event = replace(
            make_event(
                "task_started",
                turn_id=f"turn_product_{suffix}",
                command_id=command_id,
            ),
            event_id=f"event_task_started_product_{suffix}",
            occurred_at=context.requested_at + timedelta(milliseconds=index),
        )
        return context, event

    async def _prepare_turn(
        self,
        index: int,
        *,
        event_type: str = "task_started",
        failure_count: int | None = None,
        evidence_refs: tuple[EvidenceRef, ...] | None = None,
    ) -> _PreparedTurn:
        context, event = self._turn_identity(index)
        if event_type != event.event_type:
            event = replace(
                make_event(
                    event_type,
                    failure_count=failure_count,
                    turn_id=event.turn_id,
                    command_id=context.command_id,
                ),
                event_id=f"event_{event_type}_product_{index:04d}",
                occurred_at=event.occurred_at,
            )
        if evidence_refs is not None:
            event = replace(event, evidence_refs=evidence_refs)
        request_body = (
            f'{{"command_id":"{context.command_id}","turn_id":"{event.turn_id}"}}'
        ).encode()
        new_command = NewCommand(
            command_type="EXECUTE_AGENT_TURN",
            idempotency_key=f"agent-turn:product:{index:04d}",
            request_sha256=hashlib.sha256(request_body).hexdigest(),
            versions=make_versions(),
        )
        command = new_command.initial_record(context, context.requested_at)
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                INSERT INTO yaya_commands(
                  tenant_id,actor_id,operation,idempotency_key,command_id,
                  session_id,turn_id,client_turn_sequence,request_sha256,content_hash,
                  revision,status,updated_at,record_json
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    context.actor.actor_id,
                    new_command.operation,
                    new_command.idempotency_key,
                    context.command_id,
                    event.session_id,
                    event.turn_id,
                    index,
                    new_command.request_sha256,
                    context.content_ref.content_hash,
                    command.revision,
                    command.status.value,
                    command.updated_at,
                    Jsonb(encode(command)),
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_command_jobs(
                  tenant_id,command_id,job_id,actor_id,content_hash,session_id,
                  turn_id,client_turn_sequence,event_json,operation_context_json,
                  request_body,accepted_receipt_json
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    context.command_id,
                    f"job_product_{index:04d}",
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    event.session_id,
                    event.turn_id,
                    index,
                    Jsonb(encode(event)),
                    Jsonb(encode(context)),
                    request_body,
                    Jsonb({"accepted": True}),
                ),
            )
        finally:
            await connection.close()
        claim = await self.turns.claim(event, context)
        self.assertIsNotNone(claim.claim_id)
        return _PreparedTurn(index, context, event, command, claim.claim_id or "")

    async def _commit_turn(self, turn: _PreparedTurn) -> object:
        receipt = await self.turns.commit(
            turn.event,
            RoleRoute("task_started", "world_agent", "handled"),
            make_agent_decision(f"Product response {turn.index}"),
            turn.claim_id,
            turn.context,
        )
        self.assertTrue(receipt.created)
        await self._terminalize_run_free_command(turn)
        return receipt

    async def _terminalize_run_free_command(self, turn: _PreparedTurn) -> None:
        terminal = replace(
            turn.command,
            status=CommandStatus.APPLIED,
            stage="COMPLETE",
            terminal=True,
            result={
                "result_type": "NO_EFFECT",
                "reason_code": "MODEL_FALLBACK_NO_RUN",
            },
            error=None,
            evidence_refs=(),
            revision=turn.command.revision + 1,
            updated_at=turn.command.updated_at + timedelta(milliseconds=1),
        )
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                UPDATE yaya_commands
                SET revision=%s,status=%s,updated_at=%s,record_json=%s
                WHERE tenant_id=%s AND command_id=%s AND revision=%s
                """,
                (
                    terminal.revision,
                    terminal.status.value,
                    terminal.updated_at,
                    Jsonb(encode(terminal)),
                    turn.context.actor.tenant_id,
                    turn.context.command_id,
                    turn.command.revision,
                ),
            )
            self.assertEqual(cursor.rowcount, 1)
            await connection.execute(
                """
                UPDATE yaya_command_jobs SET state='DONE'
                WHERE tenant_id=%s AND command_id=%s
                """,
                (turn.context.actor.tenant_id, turn.context.command_id),
            )
        finally:
            await connection.close()

    async def _terminalize_failed_command(
        self,
        turn: _PreparedTurn,
        evidence: EvidenceRef,
    ) -> None:
        if turn.event.run_id is None:
            self.fail("failed Command fixture has no canonical Run")
        terminal = replace(
            turn.command,
            status=CommandStatus.FAILED,
            stage="SANDBOX",
            terminal=True,
            result=None,
            error=ContractError(
                code="SANDBOX_RUNTIME_ERROR",
                category=ErrorCategory.SANDBOX,
                retryable=False,
                user_message_key="sandbox.runtime_error",
                stage="SANDBOX",
                message="The run-backed Product fixture failed in the Sandbox.",
            ),
            evidence_refs=(evidence,),
            links={
                "self": f"/v1/commands/{turn.context.command_id}",
                "run": f"/v1/runs/{turn.event.run_id}",
            },
            revision=turn.command.revision + 1,
            updated_at=turn.command.updated_at + timedelta(milliseconds=1),
        )
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                UPDATE yaya_commands
                SET revision=%s,status=%s,updated_at=%s,record_json=%s
                WHERE tenant_id=%s AND command_id=%s AND revision=%s
                """,
                (
                    terminal.revision,
                    terminal.status.value,
                    terminal.updated_at,
                    Jsonb(encode(terminal)),
                    turn.context.actor.tenant_id,
                    turn.context.command_id,
                    turn.command.revision,
                ),
            )
            self.assertEqual(cursor.rowcount, 1)
            await connection.execute(
                """
                UPDATE yaya_command_jobs SET state='DONE'
                WHERE tenant_id=%s AND command_id=%s
                """,
                (turn.context.actor.tenant_id, turn.context.command_id),
            )
        finally:
            await connection.close()

    async def _terminalize_committed_command(
        self,
        turn: _PreparedTurn,
        evidence: EvidenceRef,
        receipt: WorldCommitReceipt,
    ) -> None:
        if turn.event.run_id is None:
            self.fail("committed Command fixture has no canonical Run")
        terminal = replace(
            turn.command,
            status=CommandStatus.APPLIED,
            stage="COMPLETE",
            terminal=True,
            result={
                "result_type": "WORLD_COMMIT",
                "world_id": receipt.world_id,
                "previous_revision": receipt.previous_revision,
                "world_revision": receipt.world_revision,
                "first_event_sequence": receipt.first_event_sequence,
                "last_event_sequence": receipt.last_event_sequence,
            },
            error=None,
            evidence_refs=(evidence,),
            links={
                "self": f"/v1/commands/{turn.context.command_id}",
                "run": f"/v1/runs/{turn.event.run_id}",
                "world_snapshot": f"/v1/worlds/{receipt.world_id}/snapshot",
            },
            revision=turn.command.revision + 1,
            updated_at=turn.command.updated_at + timedelta(milliseconds=1),
        )
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                UPDATE yaya_commands
                SET revision=%s,status=%s,updated_at=%s,record_json=%s
                WHERE tenant_id=%s AND command_id=%s AND revision=%s
                """,
                (
                    terminal.revision,
                    terminal.status.value,
                    terminal.updated_at,
                    Jsonb(encode(terminal)),
                    turn.context.actor.tenant_id,
                    turn.context.command_id,
                    turn.command.revision,
                ),
            )
            self.assertEqual(cursor.rowcount, 1)
            await connection.execute(
                """
                UPDATE yaya_command_jobs SET state='DONE'
                WHERE tenant_id=%s AND command_id=%s
                """,
                (turn.context.actor.tenant_id, turn.context.command_id),
            )
        finally:
            await connection.close()

    async def _seed_failed_run_authority(
        self,
        turn: _PreparedTurn,
        evidence: EvidenceRef,
        payload: dict[str, object],
    ) -> None:
        event = turn.event
        if event.run_id is None or event.skill_ref is None or event.failure_key is None:
            self.fail("run-backed Product fixture is not identity-complete")
        now = turn.context.requested_at
        now_wire = now.isoformat().replace("+00:00", "Z")
        context_wire = cast(dict[str, object], plain(_request_context(turn.context)))
        versions = {
            key: value
            for key, value in cast(dict[str, object], plain(make_versions())).items()
            if value is not None
        }
        evidence_wire: dict[str, object] = {
            "evidence_id": evidence.evidence_id,
            "evidence_type": evidence.evidence_type.value,
            "created_at": evidence.created_at.isoformat().replace("+00:00", "Z"),
            "sha256": evidence.sha256,
        }
        failure = ContractError(
            code="SANDBOX_RUNTIME_ERROR",
            category=ErrorCategory.SANDBOX,
            retryable=False,
            user_message_key="sandbox.runtime_error",
            stage="SANDBOX",
            message="The run-backed Product fixture failed in the Sandbox.",
        )
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
            failed_actions=({"reason": event.failure_key},),
            failure_key=event.failure_key,
            evidence_refs=(evidence,),
            world_commit=None,
            request_context=_request_context(turn.context),
        )
        run_wire: dict[str, object] = {
            "request_context": context_wire,
            "run_id": event.run_id,
            "session_id": event.session_id,
            "turn_id": event.turn_id,
            "command_id": event.command_id,
            "status": "FAILED",
            "terminal": True,
            "skill": cast(dict[str, object], plain(event.skill_ref)),
            "sandbox": {
                "invocation_id": "invoke_product_run_0001",
                "status": "FAILED",
                "started_at": now_wire,
                "finished_at": now_wire,
                "limits": {
                    "cpu_ms": 500,
                    "wall_ms": 2_000,
                    "memory_bytes": 67_108_864,
                    "max_intents": 32,
                },
                "usage": None,
                "action_intents": [],
                "failure": plain(failure),
            },
            "world_application": {
                "status": "NOT_ATTEMPTED",
                "receipt": None,
                "failure": None,
            },
            "agent_feedback": None,
            "created_at": now_wire,
            "updated_at": now_wire,
            "evidence_refs": [evidence_wire],
            "versions": versions,
        }
        evidence_document: dict[str, object] = {
            "request_context": context_wire,
            "evidence_ref": evidence_wire,
            "subject": {"learner_id": turn.context.actor.actor_id},
            "source": {
                "source_type": "SKILL_RUN",
                "source_id": event.run_id,
                "command_id": event.command_id,
                "world_id": WORLD_ID,
            },
            "occurred_at": now_wire,
            "recorded_at": now_wire,
            "integrity": {
                "payload_sha256": evidence.sha256,
                "previous_evidence_sha256": None,
            },
            "payload": payload,
            "related_evidence": [],
            "versions": versions,
        }
        self.validator.validate("schemas/game/run.schema.json", run_wire)
        self.validator.validate("schemas/game/evidence.schema.json", evidence_document)
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO yaya_evidence(
                  tenant_id,evidence_id,actor_id,content_hash,evidence_type,
                  payload_sha256,evidence_json,recorded_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    turn.context.actor.tenant_id,
                    evidence.evidence_id,
                    turn.context.actor.actor_id,
                    turn.context.content_ref.content_hash,
                    evidence.evidence_type.value,
                    evidence.sha256,
                    Jsonb(evidence_document),
                    now,
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_runs(
                  tenant_id,run_id,actor_id,content_hash,session_id,turn_id,
                  command_id,world_id,skill_version_id,failure_key,task_success,
                  snapshot_json,wire_json,created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE,%s,%s,%s)
                """,
                (
                    turn.context.actor.tenant_id,
                    event.run_id,
                    turn.context.actor.actor_id,
                    turn.context.content_ref.content_hash,
                    event.session_id,
                    event.turn_id,
                    event.command_id,
                    WORLD_ID,
                    event.skill_ref.skill_version_id,
                    event.failure_key,
                    Jsonb(encode(run)),
                    Jsonb(run_wire),
                    now,
                ),
            )

    def _committed_run_fixture(
        self,
        index: int,
    ) -> tuple[WorldCommitReceipt, EvidenceRef, dict[str, object], list[dict[str, object]]]:
        context, _ = self._turn_identity(index)
        state = make_world_state()
        plots = cast(list[dict[str, object]], state["plots"])
        intents: list[dict[str, object]] = []
        for ordinal, plot in enumerate(plots, start=1):
            plot["hydration"] = 100
            plot["last_updated_event_sequence"] = 41
            intents.append(
                {
                    "intent_id": f"intent_product_{ordinal:04d}",
                    "action_type": "WATER",
                    "actor_entity_id": "avatar_0001",
                    "expected_world_revision": 5,
                    "plot_id": f"plot_{ordinal:04d}",
                    "amount_ml": 100,
                }
            )
        receipt = WorldCommitReceipt(
            world_id=WORLD_ID,
            previous_revision=5,
            world_revision=6,
            first_event_sequence=41,
            last_event_sequence=41,
            committed_at=context.requested_at,
            state_hash=canonical_json_sha256(state),
        )
        evidence = EvidenceRef(
            f"evidence_product_world_{index:04d}",
            EvidenceType.WORLD_COMMIT,
            context.requested_at,
            sha256=world_commit_receipt_sha256(receipt),
        )
        return receipt, evidence, state, intents

    async def _seed_committed_run_authority(
        self,
        turn: _PreparedTurn,
        receipt: WorldCommitReceipt,
        evidence: EvidenceRef,
        state: dict[str, object],
        intents: list[dict[str, object]],
    ) -> RuntimeEvent:
        event = turn.event
        if event.run_id is None or event.skill_ref is None:
            self.fail("committed Run fixture is not identity-complete")
        now = receipt.committed_at
        now_wire = now.isoformat().replace("+00:00", "Z")
        context_wire = cast(dict[str, object], plain(_request_context(turn.context)))
        versions = {
            key: value
            for key, value in cast(dict[str, object], plain(make_versions())).items()
            if value is not None
        }
        evidence_wire: dict[str, object] = {
            "evidence_id": evidence.evidence_id,
            "evidence_type": evidence.evidence_type.value,
            "created_at": now_wire,
            "sha256": evidence.sha256,
        }
        receipt_wire = cast(dict[str, object], plain(receipt))
        world_event = RuntimeEvent(
            event_id=f"evt_world_product_{turn.index:04d}",
            event_type=RuntimeEventType.WORLD_COMMITTED,
            event_version=1,
            stream_id=f"world:{WORLD_ID}",
            sequence=receipt.first_event_sequence,
            occurred_at=now,
            producer="world_engine",
            trace_id=turn.context.trace_id,
            command_id=event.command_id,
            correlation_id=turn.context.correlation_id,
            causation_id=event.command_id,
            content_ref=turn.context.content_ref,
            payload={
                "commit_id": f"commit_product_world_{turn.index:04d}",
                "run_id": event.run_id,
                "world_id": WORLD_ID,
                "previous_world_revision": receipt.previous_revision,
                "world_revision": receipt.world_revision,
                "state_hash": receipt.state_hash,
                "applied_intent_ids": tuple(cast(str, intent["intent_id"]) for intent in intents),
                "committed_at": now_wire,
                "evidence_refs": (evidence_wire,),
            },
        )
        run = RunResultSnapshot(
            run_id=event.run_id,
            session_id=event.session_id,
            turn_id=event.turn_id,
            command_id=event.command_id,
            world_id=WORLD_ID,
            skill_ref=event.skill_ref,
            task_success=True,
            world_revision_before=receipt.previous_revision,
            world_revision_after=receipt.world_revision,
            world_difference={
                "watered_plots": 8,
                "total_plots": 8,
                "intent_count": len(intents),
            },
            failed_actions=(),
            failure_key=None,
            evidence_refs=(evidence,),
            world_commit=receipt,
            request_context=_request_context(turn.context),
        )
        run_wire: dict[str, object] = {
            "request_context": context_wire,
            "run_id": event.run_id,
            "session_id": event.session_id,
            "turn_id": event.turn_id,
            "command_id": event.command_id,
            "status": "SUCCEEDED",
            "terminal": True,
            "skill": cast(dict[str, object], plain(event.skill_ref)),
            "sandbox": {
                "invocation_id": f"invoke_product_world_{turn.index:04d}",
                "status": "SUCCEEDED",
                "started_at": now_wire,
                "finished_at": now_wire,
                "limits": {
                    "cpu_ms": 500,
                    "wall_ms": 2_000,
                    "memory_bytes": 67_108_864,
                    "max_intents": 32,
                },
                "usage": {
                    "cpu_ms": 1,
                    "wall_ms": 1,
                    "peak_memory_bytes": 1_024,
                },
                "action_intents": intents,
                "failure": None,
            },
            "world_application": {
                "status": "COMMITTED",
                "receipt": receipt_wire,
                "failure": None,
            },
            "agent_feedback": None,
            "created_at": now_wire,
            "updated_at": now_wire,
            "evidence_refs": [evidence_wire],
            "versions": versions,
        }
        evidence_payload: dict[str, object] = {
            "evidence_kind": "WORLD_COMMIT",
            "world_id": receipt.world_id,
            "previous_revision": receipt.previous_revision,
            "world_revision": receipt.world_revision,
            "first_event_sequence": receipt.first_event_sequence,
            "last_event_sequence": receipt.last_event_sequence,
            "state_hash": receipt.state_hash,
        }
        evidence_document: dict[str, object] = {
            "request_context": context_wire,
            "evidence_ref": evidence_wire,
            "subject": {"learner_id": turn.context.actor.actor_id},
            "source": {
                "source_type": "WORLD",
                "source_id": WORLD_ID,
                "command_id": event.command_id,
                "world_id": WORLD_ID,
            },
            "occurred_at": now_wire,
            "recorded_at": now_wire,
            "integrity": {
                "payload_sha256": evidence.sha256,
                "previous_evidence_sha256": None,
            },
            "payload": evidence_payload,
            "related_evidence": [],
            "versions": versions,
        }
        self.validator.validate("schemas/game/run.schema.json", run_wire)
        self.validator.validate("schemas/game/evidence.schema.json", evidence_document)
        async with self.database.transaction() as connection:
            updated = await connection.execute(
                """
                UPDATE yaya_worlds
                SET revision=%s,last_event_sequence=%s,state_hash=%s,
                    state_json=%s,updated_at=%s
                WHERE tenant_id=%s AND world_id=%s AND revision=%s
                  AND last_event_sequence=%s
                """,
                (
                    receipt.world_revision,
                    receipt.last_event_sequence,
                    receipt.state_hash,
                    Jsonb(state),
                    now,
                    turn.context.actor.tenant_id,
                    WORLD_ID,
                    receipt.previous_revision,
                    receipt.first_event_sequence - 1,
                ),
            )
            self.assertEqual(updated.rowcount, 1)
            await connection.execute(
                """
                INSERT INTO yaya_events(
                  tenant_id,event_id,stream_id,sequence,event_type,event_json,occurred_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    turn.context.actor.tenant_id,
                    world_event.event_id,
                    world_event.stream_id,
                    world_event.sequence,
                    world_event.event_type,
                    Jsonb(plain(world_event)),
                    world_event.occurred_at,
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_evidence(
                  tenant_id,evidence_id,actor_id,content_hash,evidence_type,
                  payload_sha256,evidence_json,recorded_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    turn.context.actor.tenant_id,
                    evidence.evidence_id,
                    turn.context.actor.actor_id,
                    turn.context.content_ref.content_hash,
                    evidence.evidence_type.value,
                    evidence.sha256,
                    Jsonb(evidence_document),
                    now,
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_runs(
                  tenant_id,run_id,actor_id,content_hash,session_id,turn_id,
                  command_id,world_id,skill_version_id,failure_key,task_success,
                  snapshot_json,wire_json,created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,TRUE,%s,%s,%s)
                """,
                (
                    turn.context.actor.tenant_id,
                    event.run_id,
                    turn.context.actor.actor_id,
                    turn.context.content_ref.content_hash,
                    event.session_id,
                    event.turn_id,
                    event.command_id,
                    WORLD_ID,
                    event.skill_ref.skill_version_id,
                    Jsonb(encode(run)),
                    Jsonb(run_wire),
                    now,
                ),
            )
        return world_event

    def _committed_run_decision(self, evidence: EvidenceRef) -> AgentDecision:
        decision = make_agent_decision("The World task completed successfully.")
        directive = decision.teaching_directive
        if directive is None:
            self.fail("committed Run fixture has no TeachingDirective")
        return replace(
            decision,
            draft=replace(
                decision.draft,
                role="book_agent",
                response_type="growth_summary",
            ),
            message_key="agent.book_agent.growth_summary",
            evidence_refs=(evidence,),
            teaching_directive=replace(
                directive,
                phase=TeachingPhase.SUMMARIZATION,
                allowed_response_types=("growth_summary",),
                required_evidence_ids=(evidence.evidence_id,),
            ),
        )

    async def _interaction_rows(self) -> list[dict[str, object]]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT sequence,turn_id,interaction_id,run_id
                FROM yaya_agent_interactions
                WHERE tenant_id=%s AND session_id=%s
                ORDER BY sequence
                """,
                (self.base_context.actor.tenant_id, SESSION_ID),
            )
            return list(await cursor.fetchall())
        finally:
            await connection.close()

    async def _fetch_one(
        self,
        query: LiteralString,
        params: tuple[object, ...],
    ) -> dict[str, object]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(query, params)
            row = await cursor.fetchone()
            if row is None:
                self.fail("canonical anchor query returned no row")
            return row
        finally:
            await connection.close()

    async def _execute_sql(
        self,
        query: LiteralString,
        params: tuple[object, ...],
    ) -> None:
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(query, params)
        finally:
            await connection.close()

    async def _database_fingerprint(self) -> tuple[tuple[str, str], ...]:
        connection = await self.database.connect(autocommit=True)
        try:
            table_cursor = await connection.execute(
                """
                SELECT tablename FROM pg_catalog.pg_tables
                WHERE schemaname=ANY(current_schemas(false))
                  AND left(tablename,5)='yaya_'
                ORDER BY tablename
                """
            )
            table_rows = list(await table_cursor.fetchall())
            table_names: list[str] = []
            for row in table_rows:
                table_name = row["tablename"]
                if not isinstance(table_name, str):
                    self.fail("PostgreSQL returned a non-string business table name")
                table_names.append(table_name)
            if not table_names:
                self.fail("PostgreSQL exposed no yaya_* business tables")
            result: list[tuple[str, str]] = []
            for table_name in table_names:
                query = sql.SQL(
                    "SELECT COALESCE("
                    "jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),"
                    "'[]'::jsonb) AS rows FROM {} AS t"
                ).format(sql.Identifier(table_name))
                cursor = await connection.execute(query)
                row = await cursor.fetchone()
                if row is None:
                    self.fail(f"fingerprint query for {table_name} returned no row")
                result.append(
                    (
                        table_name,
                        canonical_json_sha256({"rows": row["rows"]}),
                    )
                )
            return tuple(result)
        finally:
            await connection.close()

    async def test_run_free_list_get_restart_and_reads_do_not_write(self) -> None:
        turn = await self._prepare_turn(1)
        await self._commit_turn(turn)
        before = await self._database_fingerprint()

        page = await self.product.list_interactions(
            turn.context.actor,
            SESSION_ID,
            after_sequence=0,
            limit=50,
        )
        self.assertEqual(page.session_id, SESSION_ID)
        self.assertEqual(page.high_watermark_sequence, 1)
        self.assertEqual(len(page.interactions), 1)
        interaction = page.interactions[0].interaction
        self.assertEqual(interaction["sequence"], 1)
        self.assertEqual(interaction["session_id"], SESSION_ID)
        self.assertEqual(interaction["turn_id"], turn.event.turn_id)
        feedback = cast(dict[str, object], interaction["feedback"])
        self.assertEqual(feedback["command_id"], turn.context.command_id)
        self.assertIsNone(feedback["run_id"])

        fetched = await self.product.get_interaction(
            turn.context.actor,
            SESSION_ID,
            cast(str, interaction["interaction_id"]),
        )
        self.assertEqual(fetched, page.interactions[0])

        restarted_database = PostgresDatabase(self.server.dsn)
        restarted = PostgresProductInteractionReadRepository(
            restarted_database,
            ContractSchemaValidator(CONTRACTS_ROOT),
        )
        restarted_page = await restarted.list_interactions(
            turn.context.actor,
            SESSION_ID,
            after_sequence=0,
            limit=50,
        )
        restarted_get = await restarted.get_interaction(
            turn.context.actor,
            SESSION_ID,
            cast(str, interaction["interaction_id"]),
        )
        self.assertEqual(restarted_page, page)
        self.assertEqual(restarted_get, fetched)
        self.assertEqual(await self._database_fingerprint(), before)

    async def test_committed_run_feedback_causation_uses_canonical_world_event(self) -> None:
        receipt, evidence, state, intents = self._committed_run_fixture(1)
        turn = await self._prepare_turn(
            1,
            event_type="task_completed",
            evidence_refs=(evidence,),
        )
        world_event = await self._seed_committed_run_authority(
            turn,
            receipt,
            evidence,
            state,
            intents,
        )
        committed = await self.turns.commit(
            turn.event,
            RoleRoute("task_completed", "book_agent", "handled"),
            self._committed_run_decision(evidence),
            turn.claim_id,
            turn.context,
        )
        self.assertTrue(committed.created)
        await self._terminalize_committed_command(turn, evidence, receipt)
        before = await self._database_fingerprint()

        page = await self.product.list_interactions(
            turn.context.actor,
            SESSION_ID,
            after_sequence=0,
            limit=50,
        )
        self.assertEqual(len(page.interactions), 1)
        interaction = page.interactions[0].interaction
        feedback_event = cast(dict[str, object], interaction["feedback_event"])
        self.assertEqual(feedback_event["causation_id"], world_event.event_id)
        fetched = await self.product.get_interaction(
            turn.context.actor,
            SESSION_ID,
            cast(str, interaction["interaction_id"]),
        )
        self.assertEqual(fetched, page.interactions[0])
        self.assertEqual(await self._database_fingerprint(), before)

    async def test_bug_writer_rejects_failure_count_below_threshold_without_publication(
        self,
    ) -> None:
        turn = await self._prepare_turn(
            1,
            event_type="run_failed",
            failure_count=2,
        )
        decision = make_agent_decision("Inspect the repeated failed loop boundary.")
        directive = decision.teaching_directive
        if directive is None:
            self.fail("Bug threshold fixture has no TeachingDirective")
        decision = replace(
            decision,
            draft=replace(
                decision.draft,
                role="bug_agent",
                response_type="question",
                question="Which loop bound omits the final plot?",
            ),
            message_key="agent.bug_agent.question",
            evidence_refs=turn.event.evidence_refs,
            teaching_directive=replace(
                directive,
                phase=TeachingPhase.RECTIFICATION,
                allowed_response_types=("question",),
                required_evidence_ids=tuple(item.evidence_id for item in turn.event.evidence_refs),
            ),
        )
        with self.assertRaises(AgentPersistenceError) as rejected:
            await self.turns.commit(
                turn.event,
                RoleRoute("run_failed", "bug_agent", "handled"),
                decision,
                turn.claim_id,
                turn.context,
            )
        self.assertEqual(
            rejected.exception.code,
            "AGENT_FINAL_ROLE_INVARIANT_VIOLATION",
        )
        self.assertEqual(await self._interaction_rows(), [])

    async def test_committed_run_writer_rejects_missing_canonical_world_cause(self) -> None:
        receipt, evidence, state, intents = self._committed_run_fixture(1)
        turn = await self._prepare_turn(
            1,
            event_type="task_completed",
            evidence_refs=(evidence,),
        )
        world_event = await self._seed_committed_run_authority(
            turn,
            receipt,
            evidence,
            state,
            intents,
        )
        world_anchor = await self._fetch_one(
            """
            SELECT event_json FROM yaya_events
            WHERE tenant_id=%s AND event_id=%s
            """,
            (turn.context.actor.tenant_id, world_event.event_id),
        )
        before = await self._database_fingerprint()
        cases: tuple[tuple[str, LiteralString, tuple[object, ...]], ...] = (
            (
                "command_id",
                """
                UPDATE yaya_events SET event_json=jsonb_set(
                    event_json,'{command_id}',to_jsonb(%s::text),false
                ) WHERE tenant_id=%s AND event_id=%s
                """,
                (
                    "cmd_wrong_world_cause_0001",
                    turn.context.actor.tenant_id,
                    world_event.event_id,
                ),
            ),
            (
                "causation_id",
                """
                UPDATE yaya_events SET event_json=jsonb_set(
                    event_json,'{causation_id}',to_jsonb(%s::text),false
                ) WHERE tenant_id=%s AND event_id=%s
                """,
                (
                    "cmd_wrong_world_cause_0002",
                    turn.context.actor.tenant_id,
                    world_event.event_id,
                ),
            ),
        )
        for name, corrupt_sql, corrupt_params in cases:
            with self.subTest(anchor=name):
                await self._execute_sql(corrupt_sql, corrupt_params)
                corrupted = await self._database_fingerprint()
                try:
                    with self.assertRaisesRegex(
                        RepositoryAuthorityError,
                        "canonical World event",
                    ):
                        await self.turns.commit(
                            turn.event,
                            RoleRoute("task_completed", "book_agent", "handled"),
                            self._committed_run_decision(evidence),
                            turn.claim_id,
                            turn.context,
                        )
                    self.assertEqual(await self._interaction_rows(), [])
                    self.assertEqual(await self._database_fingerprint(), corrupted)
                finally:
                    await self._execute_sql(
                        """
                        UPDATE yaya_events SET event_json=%s
                        WHERE tenant_id=%s AND event_id=%s
                        """,
                        (
                            Jsonb(world_anchor["event_json"]),
                            turn.context.actor.tenant_id,
                            world_event.event_id,
                        ),
                    )
        self.assertEqual(await self._database_fingerprint(), before)

    async def test_committed_run_world_causation_corruption_matrix_fails_closed(self) -> None:
        receipt, evidence, state, intents = self._committed_run_fixture(1)
        turn = await self._prepare_turn(
            1,
            event_type="task_completed",
            evidence_refs=(evidence,),
        )
        world_event = await self._seed_committed_run_authority(
            turn,
            receipt,
            evidence,
            state,
            intents,
        )
        committed = await self.turns.commit(
            turn.event,
            RoleRoute("task_completed", "book_agent", "handled"),
            self._committed_run_decision(evidence),
            turn.claim_id,
            turn.context,
        )
        self.assertTrue(committed.created)
        await self._terminalize_committed_command(turn, evidence, receipt)
        interaction_row = await self._fetch_one(
            """
            SELECT interaction_id FROM yaya_agent_interactions
            WHERE tenant_id=%s AND session_id=%s AND sequence=1
            """,
            (turn.context.actor.tenant_id, SESSION_ID),
        )
        interaction_id = cast(str, interaction_row["interaction_id"])
        world_anchor = await self._fetch_one(
            """
            SELECT event_id,stream_id,sequence,event_type,event_json,occurred_at
            FROM yaya_events WHERE tenant_id=%s AND event_id=%s
            """,
            (turn.context.actor.tenant_id, world_event.event_id),
        )
        baseline = await self.product.get_interaction(
            turn.context.actor,
            SESSION_ID,
            interaction_id,
        )
        before = await self._database_fingerprint()
        late = world_event.occurred_at + timedelta(seconds=1)
        late_wire = late.isoformat().replace("+00:00", "Z")
        drifted_commit = world_event.occurred_at - timedelta(seconds=1)
        drifted_commit_wire = drifted_commit.isoformat().replace("+00:00", "Z")
        restore_sql: LiteralString = """
            UPDATE yaya_events
            SET stream_id=%s,sequence=%s,event_type=%s,event_json=%s,occurred_at=%s
            WHERE tenant_id=%s AND event_id=%s
        """
        restore_params = (
            world_anchor["stream_id"],
            world_anchor["sequence"],
            world_anchor["event_type"],
            Jsonb(world_anchor["event_json"]),
            world_anchor["occurred_at"],
            turn.context.actor.tenant_id,
            world_event.event_id,
        )
        cases: tuple[tuple[str, LiteralString, tuple[object, ...]], ...] = (
            (
                "command_id",
                """
                UPDATE yaya_events
                SET event_json=jsonb_set(
                    event_json,'{command_id}',to_jsonb(%s::text),false
                )
                WHERE tenant_id=%s AND event_id=%s
                """,
                (
                    "cmd_wrong_world_cause_0001",
                    turn.context.actor.tenant_id,
                    world_event.event_id,
                ),
            ),
            (
                "stream_id",
                """
                UPDATE yaya_events
                SET stream_id=%s,event_json=jsonb_set(
                    event_json,'{stream_id}',to_jsonb(%s::text),false
                )
                WHERE tenant_id=%s AND event_id=%s
                """,
                (
                    "world:world_wrong_product_0001",
                    "world:world_wrong_product_0001",
                    turn.context.actor.tenant_id,
                    world_event.event_id,
                ),
            ),
            (
                "receipt_sequence_range",
                """
                UPDATE yaya_events
                SET sequence=%s,event_json=jsonb_set(
                    event_json,'{sequence}',to_jsonb(%s::bigint),false
                )
                WHERE tenant_id=%s AND event_id=%s
                """,
                (42, 42, turn.context.actor.tenant_id, world_event.event_id),
            ),
            (
                "trace_id",
                """
                UPDATE yaya_events
                SET event_json=jsonb_set(
                    event_json,'{trace_id}',to_jsonb(%s::text),false
                )
                WHERE tenant_id=%s AND event_id=%s
                """,
                (
                    "trace_wrong_world_cause_0001",
                    turn.context.actor.tenant_id,
                    world_event.event_id,
                ),
            ),
            (
                "correlation_id",
                """
                UPDATE yaya_events
                SET event_json=jsonb_set(
                    event_json,'{correlation_id}',to_jsonb(%s::text),false
                )
                WHERE tenant_id=%s AND event_id=%s
                """,
                (
                    "corr_wrong_world_cause_0001",
                    turn.context.actor.tenant_id,
                    world_event.event_id,
                ),
            ),
            (
                "content_ref",
                """
                UPDATE yaya_events
                SET event_json=jsonb_set(
                    event_json,'{content_ref,content_hash}',
                    to_jsonb(%s::text),false
                )
                WHERE tenant_id=%s AND event_id=%s
                """,
                ("f" * 64, turn.context.actor.tenant_id, world_event.event_id),
            ),
            (
                "causation_id",
                """
                UPDATE yaya_events
                SET event_json=jsonb_set(
                    event_json,'{causation_id}',to_jsonb(%s::text),false
                )
                WHERE tenant_id=%s AND event_id=%s
                """,
                (
                    "cmd_wrong_world_cause_0002",
                    turn.context.actor.tenant_id,
                    world_event.event_id,
                ),
            ),
            (
                "run_id",
                """
                UPDATE yaya_events
                SET event_json=jsonb_set(
                    event_json,'{payload,run_id}',to_jsonb(%s::text),false
                )
                WHERE tenant_id=%s AND event_id=%s
                """,
                (
                    "run_wrong_world_cause_0001",
                    turn.context.actor.tenant_id,
                    world_event.event_id,
                ),
            ),
            (
                "world_id",
                """
                UPDATE yaya_events
                SET event_json=jsonb_set(
                    event_json,'{payload,world_id}',to_jsonb(%s::text),false
                )
                WHERE tenant_id=%s AND event_id=%s
                """,
                (
                    "world_wrong_product_0001",
                    turn.context.actor.tenant_id,
                    world_event.event_id,
                ),
            ),
            (
                "previous_world_revision",
                """
                UPDATE yaya_events
                SET event_json=jsonb_set(
                    jsonb_set(
                        event_json,'{payload,previous_world_revision}',
                        to_jsonb(%s::bigint),false
                    ),
                    '{payload,world_revision}',to_jsonb(%s::bigint),false
                )
                WHERE tenant_id=%s AND event_id=%s
                """,
                (4, 5, turn.context.actor.tenant_id, world_event.event_id),
            ),
            (
                "world_revision",
                """
                UPDATE yaya_events
                SET event_json=jsonb_set(
                    event_json,'{payload,world_revision}',
                    to_jsonb(%s::bigint),false
                )
                WHERE tenant_id=%s AND event_id=%s
                """,
                (7, turn.context.actor.tenant_id, world_event.event_id),
            ),
            (
                "state_hash",
                """
                UPDATE yaya_events
                SET event_json=jsonb_set(
                    event_json,'{payload,state_hash}',to_jsonb(%s::text),false
                )
                WHERE tenant_id=%s AND event_id=%s
                """,
                ("0" * 64, turn.context.actor.tenant_id, world_event.event_id),
            ),
            (
                "committed_at",
                """
                UPDATE yaya_events
                SET event_json=jsonb_set(
                    event_json,'{payload,committed_at}',to_jsonb(%s::text),false
                )
                WHERE tenant_id=%s AND event_id=%s
                """,
                (
                    drifted_commit_wire,
                    turn.context.actor.tenant_id,
                    world_event.event_id,
                ),
            ),
            (
                "occurred_at",
                """
                UPDATE yaya_events
                SET occurred_at=%s,event_json=jsonb_set(
                    event_json,'{occurred_at}',to_jsonb(%s::text),false
                )
                WHERE tenant_id=%s AND event_id=%s
                """,
                (
                    late,
                    late_wire,
                    turn.context.actor.tenant_id,
                    world_event.event_id,
                ),
            ),
        )
        for name, corrupt_sql, corrupt_params in cases:
            with self.subTest(anchor=name):
                await self._execute_sql(corrupt_sql, corrupt_params)
                try:
                    with self.assertRaises(ProductReadInvariantError):
                        await self.product.list_interactions(
                            turn.context.actor,
                            SESSION_ID,
                            after_sequence=0,
                            limit=50,
                        )
                    with self.assertRaises(ProductReadInvariantError):
                        await self.product.get_interaction(
                            turn.context.actor,
                            SESSION_ID,
                            interaction_id,
                        )
                finally:
                    await self._execute_sql(restore_sql, restore_params)
                self.assertEqual(
                    await self.product.get_interaction(
                        turn.context.actor,
                        SESSION_ID,
                        interaction_id,
                    ),
                    baseline,
                )
        self.assertEqual(await self._database_fingerprint(), before)

    async def test_command_publication_window_is_transient_and_job_state_is_closed(
        self,
    ) -> None:
        turn = await self._prepare_turn(1)
        receipt = await self.turns.commit(
            turn.event,
            RoleRoute("task_started", "world_agent", "handled"),
            make_agent_decision("Product response before Command terminalization."),
            turn.claim_id,
            turn.context,
        )
        self.assertTrue(receipt.created)
        interaction_row = await self._fetch_one(
            """
            SELECT interaction_id FROM yaya_agent_interactions
            WHERE tenant_id=%s AND session_id=%s AND sequence=1
            """,
            (turn.context.actor.tenant_id, SESSION_ID),
        )
        interaction_id = cast(str, interaction_row["interaction_id"])
        before_transient_reads = await self._database_fingerprint()
        with self.assertRaises(ProductReadDependencyError):
            await self.product.list_interactions(
                turn.context.actor,
                SESSION_ID,
                after_sequence=0,
                limit=50,
            )
        with self.assertRaises(ProductReadDependencyError):
            await self.product.get_interaction(
                turn.context.actor,
                SESSION_ID,
                interaction_id,
            )
        self.assertEqual(await self._database_fingerprint(), before_transient_reads)

        await self._terminalize_run_free_command(turn)
        baseline = await self.product.get_interaction(
            turn.context.actor,
            SESSION_ID,
            interaction_id,
        )
        terminal_fingerprint = await self._database_fingerprint()
        await self._execute_sql(
            """
            UPDATE yaya_command_jobs SET state='READY'
            WHERE tenant_id=%s AND command_id=%s
            """,
            (turn.context.actor.tenant_id, turn.context.command_id),
        )
        try:
            with self.assertRaises(ProductReadInvariantError):
                await self.product.list_interactions(
                    turn.context.actor,
                    SESSION_ID,
                    after_sequence=0,
                    limit=50,
                )
            with self.assertRaises(ProductReadInvariantError):
                await self.product.get_interaction(
                    turn.context.actor,
                    SESSION_ID,
                    interaction_id,
                )
        finally:
            await self._execute_sql(
                """
                UPDATE yaya_command_jobs SET state='DONE'
                WHERE tenant_id=%s AND command_id=%s
                """,
                (turn.context.actor.tenant_id, turn.context.command_id),
            )
        self.assertEqual(
            await self.product.get_interaction(
                turn.context.actor,
                SESSION_ID,
                interaction_id,
            ),
            baseline,
        )
        self.assertEqual(await self._database_fingerprint(), terminal_fingerprint)

    async def test_real_localhost_list_and_get_return_the_same_persisted_interaction(
        self,
    ) -> None:
        turn = await self._prepare_turn(1)
        await self._commit_turn(turn)
        authenticator = JwtAuthenticator(
            hmac_secret="product-localhost-secret-" + "l" * 48,
            issuer="yaya-product-localhost",
            audience="yaya-product-localhost-api",
        )
        application = ProductInteractionReadApplication(self.product, self.validator)
        api = ProductHttpApi(
            application=application,
            authenticator=authenticator,
            validator=self.validator,
        )
        ready = threading.Event()
        captured = threading.Event()
        server_box: list[Any] = []

        def capture_server(server: object) -> None:
            server_box.append(server)
            captured.set()

        thread = threading.Thread(
            target=serve_http,
            args=(api, "127.0.0.1", 0),
            kwargs={"ready": ready, "server_created": capture_server},
            name="yaya-product-localhost",
            daemon=True,
        )
        thread.start()
        if not captured.wait(10) or not ready.wait(10) or not server_box:
            self.fail("Product localhost server did not become ready")
        server = server_box[0]
        port = int(server.server_address[1])
        token = authenticator.issue_for_test(turn.context.actor, now=datetime.now(UTC))
        before = await self._database_fingerprint()

        def request(target: str, suffix: str) -> tuple[int, dict[str, str], dict[str, object]]:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                connection.request(
                    "GET",
                    target,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-Schema-Version": "1.0.0",
                        "X-Request-Id": f"req_product_local_{suffix}",
                        "X-Trace-Id": f"trace_product_local_{suffix}",
                        "X-Correlation-Id": f"corr_product_local_{suffix}",
                    },
                )
                response = connection.getresponse()
                payload = cast(dict[str, object], json.loads(response.read().decode("utf-8")))
                return (
                    response.status,
                    {name.lower(): value for name, value in response.getheaders()},
                    payload,
                )
            finally:
                connection.close()

        try:
            list_status, list_headers, page = await asyncio.to_thread(
                request,
                f"/product-experience/v1/sessions/{SESSION_ID}/agent-interactions?after_sequence=0",
                "list0001",
            )
            self.assertEqual(list_status, 200, page)
            self.validator.validate(
                "schemas/product-experience/agent-interaction-page.schema.json",
                page,
            )
            self.assertEqual(list_headers["x-interaction-high-watermark"], "1")
            interactions = cast(list[dict[str, object]], page["interactions"])
            self.assertEqual(len(interactions), 1)
            interaction_id = cast(str, interactions[0]["interaction_id"])

            get_status, get_headers, interaction = await asyncio.to_thread(
                request,
                f"/product-experience/v1/sessions/{SESSION_ID}/agent-interactions/{interaction_id}",
                "get00001",
            )
            self.assertEqual(get_status, 200, interaction)
            self.validator.validate(
                "schemas/product-experience/agent-interaction.schema.json",
                interaction,
            )
            self.assertEqual(interaction, interactions[0])
            self.assertEqual(get_headers["x-interaction-revision"], "1")
            self.assertRegex(
                get_headers["etag"],
                r'^"interaction:1:[a-f0-9]{64}"$',
            )

            database_connection = await self.database.connect(autocommit=True)
            try:
                cursor = await database_connection.execute(
                    """
                    SELECT payload_sha256 FROM yaya_projection_outbox
                    WHERE tenant_id=%s AND destination='product_agent_interactions'
                    """,
                    (turn.context.actor.tenant_id,),
                )
                outbox_row = await cursor.fetchone()
                if outbox_row is None:
                    self.fail("Product projection outbox row was not persisted")
                original_payload_sha256 = cast(str, outbox_row["payload_sha256"])
                await database_connection.execute(
                    """
                    UPDATE yaya_projection_outbox SET payload_sha256=%s
                    WHERE tenant_id=%s AND destination='product_agent_interactions'
                    """,
                    ("0" * 64, turn.context.actor.tenant_id),
                )
            finally:
                await database_connection.close()

            try:
                corrupt_targets = (
                    (
                        f"/product-experience/v1/sessions/{SESSION_ID}/"
                        "agent-interactions?after_sequence=0",
                        "corruptlist0001",
                    ),
                    (
                        f"/product-experience/v1/sessions/{SESSION_ID}/"
                        f"agent-interactions/{interaction_id}",
                        "corruptget0001",
                    ),
                )
                for target, suffix in corrupt_targets:
                    with self.subTest(target=target):
                        status, headers, error = await asyncio.to_thread(
                            request,
                            target,
                            suffix,
                        )
                        self.assertEqual(status, 500, error)
                        error_body = cast(dict[str, object], error["error"])
                        self.assertEqual(error_body["code"], "INVARIANT_VIOLATION")
                        self.assertEqual(
                            headers["x-request-id"],
                            f"req_product_local_{suffix}",
                        )
                        self.assertNotIn("etag", headers)
                        self.assertNotIn("x-interaction-revision", headers)
                        self.assertNotIn("x-interaction-high-watermark", headers)
            finally:
                database_connection = await self.database.connect(autocommit=True)
                try:
                    await database_connection.execute(
                        """
                        UPDATE yaya_projection_outbox SET payload_sha256=%s
                        WHERE tenant_id=%s AND destination='product_agent_interactions'
                        """,
                        (original_payload_sha256, turn.context.actor.tenant_id),
                    )
                finally:
                    await database_connection.close()

            self.assertEqual(await self._database_fingerprint(), before)
        finally:
            await asyncio.to_thread(server.shutdown)
            thread.join(timeout=10)
            if thread.is_alive():
                self.fail("Product localhost server did not stop")

    async def test_consecutive_pages_keep_a_stable_snapshot_during_commit(self) -> None:
        for index in range(1, 4):
            await self._commit_turn(await self._prepare_turn(index))

        first = await self.product.list_interactions(
            self.base_context.actor,
            SESSION_ID,
            after_sequence=0,
            limit=2,
        )
        second = await self.product.list_interactions(
            self.base_context.actor,
            SESSION_ID,
            after_sequence=2,
            limit=2,
        )
        empty = await self.product.list_interactions(
            self.base_context.actor,
            SESSION_ID,
            after_sequence=3,
            limit=2,
        )
        self.assertEqual(first.high_watermark_sequence, 3)
        self.assertEqual(
            [item.interaction["sequence"] for item in first.interactions],
            [1, 2],
        )
        self.assertEqual(second.high_watermark_sequence, 3)
        self.assertEqual(
            [item.interaction["sequence"] for item in second.interactions],
            [3],
        )
        self.assertEqual(empty.high_watermark_sequence, 3)
        self.assertEqual(empty.interactions, ())

        fourth = await self._prepare_turn(4)
        high_watermark_read = asyncio.Event()
        release = asyncio.Event()
        gated_database = _HighWatermarkGateDatabase(
            self.server.dsn,
            high_watermark_read,
            release,
        )
        gated_product = PostgresProductInteractionReadRepository(
            gated_database,
            ContractSchemaValidator(CONTRACTS_ROOT),
        )
        racing_read = asyncio.create_task(
            gated_product.list_interactions(
                self.base_context.actor,
                SESSION_ID,
                after_sequence=0,
                limit=50,
            )
        )
        await asyncio.wait_for(high_watermark_read.wait(), timeout=5)
        try:
            await self._commit_turn(fourth)
        finally:
            release.set()
        stable_page = await asyncio.wait_for(racing_read, timeout=5)
        self.assertEqual(stable_page.high_watermark_sequence, 3)
        self.assertEqual(
            [item.interaction["sequence"] for item in stable_page.interactions],
            [1, 2, 3],
        )
        final_page = await self.product.list_interactions(
            self.base_context.actor,
            SESSION_ID,
            after_sequence=0,
            limit=50,
        )
        self.assertEqual(final_page.high_watermark_sequence, 4)
        self.assertEqual(
            [item.interaction["sequence"] for item in final_page.interactions],
            [1, 2, 3, 4],
        )

    async def test_cross_scope_and_actor_identity_are_hidden(self) -> None:
        turn = await self._prepare_turn(1)
        await self._commit_turn(turn)
        rows = await self._interaction_rows()
        interaction_id = cast(str, rows[0]["interaction_id"])
        other_session_id = "session_other_0001"
        other_session = make_session(
            operation=turn.context,
            session_id=other_session_id,
        )
        await self._execute_sql(
            """
            INSERT INTO yaya_agent_sessions(
              tenant_id,session_id,actor_id,task_id,world_id,content_hash,snapshot_json
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                turn.context.actor.tenant_id,
                other_session_id,
                turn.context.actor.actor_id,
                TASK_ID,
                WORLD_ID,
                turn.context.content_ref.content_hash,
                Jsonb(encode(other_session)),
            ),
        )
        actors = (
            replace(turn.context.actor, tenant_id="tenant_other_0001"),
            replace(turn.context.actor, actor_id="student_other_0001"),
            replace(turn.context.actor, actor_type=ActorType.TEACHER),
        )
        before = await self._database_fingerprint()
        for actor in actors:
            with self.subTest(scope=(actor.tenant_id, actor.actor_id, actor.actor_type.value)):
                with self.assertRaises(ProductReadNotFoundError):
                    await self.product.list_interactions(
                        actor,
                        SESSION_ID,
                        after_sequence=0,
                        limit=50,
                    )
                with self.assertRaises(ProductReadNotFoundError):
                    await self.product.get_interaction(
                        actor,
                        SESSION_ID,
                        interaction_id,
                    )
        other_page = await self.product.list_interactions(
            turn.context.actor,
            other_session_id,
            after_sequence=0,
            limit=50,
        )
        self.assertEqual(other_page.session_id, other_session_id)
        self.assertEqual(other_page.high_watermark_sequence, 0)
        self.assertEqual(other_page.interactions, ())
        with self.assertRaises(ProductReadNotFoundError):
            await self.product.get_interaction(
                turn.context.actor,
                other_session_id,
                interaction_id,
            )
        with self.assertRaises(ProductReadNotFoundError):
            await self.product.get_interaction(
                turn.context.actor,
                SESSION_ID,
                "interaction_other_0001",
            )
        self.assertEqual(await self._database_fingerprint(), before)

    async def test_projection_identity_and_content_corruption_fail_closed(self) -> None:
        turn = await self._prepare_turn(1)
        await self._commit_turn(turn)
        rows = await self._interaction_rows()
        interaction_id = cast(str, rows[0]["interaction_id"])
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT projection_json FROM yaya_agent_interactions
                WHERE tenant_id=%s AND interaction_id=%s
                """,
                (turn.context.actor.tenant_id, interaction_id),
            )
            original_row = await cursor.fetchone()
            if original_row is None:
                self.fail("canonical Product projection was not found")
            original_projection = original_row["projection_json"]
            await connection.execute(
                """
                UPDATE yaya_agent_interactions
                SET projection_json=jsonb_set(
                    projection_json,'{interaction_id}',to_jsonb(%s::text),false
                )
                WHERE tenant_id=%s AND interaction_id=%s
                """,
                (
                    "interaction_corrupted_0001",
                    turn.context.actor.tenant_id,
                    interaction_id,
                ),
            )
        finally:
            await connection.close()
        with self.assertRaises(ProductReadInvariantError):
            await self.product.list_interactions(
                turn.context.actor,
                SESSION_ID,
                after_sequence=0,
                limit=50,
            )
        with self.assertRaises(ProductReadInvariantError):
            await self.product.get_interaction(
                turn.context.actor,
                SESSION_ID,
                interaction_id,
            )

        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                UPDATE yaya_agent_interactions SET projection_json=%s
                WHERE tenant_id=%s AND interaction_id=%s
                """,
                (
                    Jsonb(original_projection),
                    turn.context.actor.tenant_id,
                    interaction_id,
                ),
            )
            await connection.execute(
                """
                UPDATE yaya_agent_interactions
                SET projection_json=jsonb_set(
                    projection_json,
                    '{request_context,content_ref,content_hash}',
                    to_jsonb(%s::text),
                    false
                )
                WHERE tenant_id=%s AND interaction_id=%s
                """,
                ("0" * 64, turn.context.actor.tenant_id, interaction_id),
            )
        finally:
            await connection.close()
        with self.assertRaises(ProductReadInvariantError):
            await self.product.list_interactions(
                turn.context.actor,
                SESSION_ID,
                after_sequence=0,
                limit=50,
            )
        with self.assertRaises(ProductReadInvariantError):
            await self.product.get_interaction(
                turn.context.actor,
                SESSION_ID,
                interaction_id,
            )

    async def test_duplicate_committed_source_turn_fails_closed_and_restores(self) -> None:
        turn = await self._prepare_turn(1)
        await self._commit_turn(turn)
        interaction_row = await self._fetch_one(
            """
            SELECT interaction_id FROM yaya_agent_interactions
            WHERE tenant_id=%s AND session_id=%s AND sequence=1
            """,
            (turn.context.actor.tenant_id, SESSION_ID),
        )
        interaction_id = cast(str, interaction_row["interaction_id"])
        baseline = await self.product.get_interaction(
            turn.context.actor,
            SESSION_ID,
            interaction_id,
        )
        before = await self._database_fingerprint()
        duplicate_event_id = "event_duplicate_product_source_0001"
        await self._execute_sql(
            """
            INSERT INTO yaya_agent_turns(
              tenant_id,event_id,actor_id,content_hash,event_sha256,
              record_json,committed_at
            )
            SELECT tenant_id,%s,actor_id,content_hash,event_sha256,
                   record_json,committed_at
            FROM yaya_agent_turns
            WHERE tenant_id=%s AND event_id=%s
            """,
            (
                duplicate_event_id,
                turn.context.actor.tenant_id,
                turn.event.event_id,
            ),
        )
        try:
            with self.assertRaises(ProductReadInvariantError):
                await self.product.list_interactions(
                    turn.context.actor,
                    SESSION_ID,
                    after_sequence=0,
                    limit=50,
                )
            with self.assertRaises(ProductReadInvariantError):
                await self.product.get_interaction(
                    turn.context.actor,
                    SESSION_ID,
                    interaction_id,
                )
        finally:
            await self._execute_sql(
                """
                DELETE FROM yaya_agent_turns
                WHERE tenant_id=%s AND event_id=%s
                """,
                (turn.context.actor.tenant_id, duplicate_event_id),
            )
        self.assertEqual(
            await self.product.get_interaction(
                turn.context.actor,
                SESSION_ID,
                interaction_id,
            ),
            baseline,
        )
        self.assertEqual(await self._database_fingerprint(), before)

    async def test_run_free_canonical_anchor_corruption_matrix_fails_closed(self) -> None:
        turn = await self._prepare_turn(1)
        await self._commit_turn(turn)
        interaction_row = await self._fetch_one(
            """
            SELECT interaction_id,projection_json,created_at
            FROM yaya_agent_interactions
            WHERE tenant_id=%s AND session_id=%s AND sequence=1
            """,
            (turn.context.actor.tenant_id, SESSION_ID),
        )
        interaction_id = cast(str, interaction_row["interaction_id"])
        projection = cast(dict[str, object], interaction_row["projection_json"])
        feedback_event = cast(dict[str, object], projection["feedback_event"])
        feedback_event_id = cast(str, feedback_event["event_id"])
        turn_anchor = await self._fetch_one(
            """
            SELECT event_sha256,record_json FROM yaya_agent_turns
            WHERE tenant_id=%s AND event_id=%s
            """,
            (turn.context.actor.tenant_id, turn.event.event_id),
        )
        event_anchor = await self._fetch_one(
            """
            SELECT event_type,event_json,occurred_at FROM yaya_events
            WHERE tenant_id=%s AND event_id=%s
            """,
            (turn.context.actor.tenant_id, feedback_event_id),
        )
        command_anchor = await self._fetch_one(
            """
            SELECT request_sha256,record_json FROM yaya_commands
            WHERE tenant_id=%s AND command_id=%s
            """,
            (turn.context.actor.tenant_id, turn.context.command_id),
        )
        job_anchor = await self._fetch_one(
            """
            SELECT event_json,operation_context_json FROM yaya_command_jobs
            WHERE tenant_id=%s AND command_id=%s
            """,
            (turn.context.actor.tenant_id, turn.context.command_id),
        )
        session_anchor = await self._fetch_one(
            """
            SELECT snapshot_json FROM yaya_agent_sessions
            WHERE tenant_id=%s AND session_id=%s
            """,
            (turn.context.actor.tenant_id, SESSION_ID),
        )
        task_anchor = await self._fetch_one(
            """
            SELECT snapshot_json FROM yaya_tasks
            WHERE tenant_id=%s AND task_id=%s
            """,
            (turn.context.actor.tenant_id, TASK_ID),
        )
        baseline = await self.product.get_interaction(
            turn.context.actor,
            SESSION_ID,
            interaction_id,
        )
        before = await self._database_fingerprint()
        tenant_id = turn.context.actor.tenant_id
        cases: tuple[
            tuple[
                str,
                LiteralString,
                tuple[object, ...],
                LiteralString,
                tuple[object, ...],
            ],
            ...,
        ] = (
            (
                "source_turn_event_sha256",
                """
                UPDATE yaya_agent_turns SET event_sha256=%s
                WHERE tenant_id=%s AND event_id=%s
                """,
                ("0" * 64, tenant_id, turn.event.event_id),
                """
                UPDATE yaya_agent_turns SET event_sha256=%s
                WHERE tenant_id=%s AND event_id=%s
                """,
                (turn_anchor["event_sha256"], tenant_id, turn.event.event_id),
            ),
            (
                "committed_turn_validated_decision",
                """
                UPDATE yaya_agent_turns
                SET record_json=jsonb_set(
                    record_json,
                    '{$fields,decision,$fields,draft,$fields,message}',
                    to_jsonb(%s::text),
                    false
                )
                WHERE tenant_id=%s AND event_id=%s
                """,
                ("Corrupted committed decision.", tenant_id, turn.event.event_id),
                """
                UPDATE yaya_agent_turns SET record_json=%s
                WHERE tenant_id=%s AND event_id=%s
                """,
                (Jsonb(turn_anchor["record_json"]), tenant_id, turn.event.event_id),
            ),
            (
                "feedback_ready_payload_envelope",
                """
                UPDATE yaya_events
                SET event_json=jsonb_set(
                    event_json,'{$fields,payload,message}',to_jsonb(%s::text),false
                )
                WHERE tenant_id=%s AND event_id=%s
                """,
                ("Corrupted feedback payload.", tenant_id, feedback_event_id),
                """
                UPDATE yaya_events SET event_json=%s
                WHERE tenant_id=%s AND event_id=%s
                """,
                (Jsonb(event_anchor["event_json"]), tenant_id, feedback_event_id),
            ),
            (
                "feedback_ready_row_envelope",
                """
                UPDATE yaya_events SET event_type='agent.turn.feedback_corrupted'
                WHERE tenant_id=%s AND event_id=%s
                """,
                (tenant_id, feedback_event_id),
                """
                UPDATE yaya_events SET event_type=%s
                WHERE tenant_id=%s AND event_id=%s
                """,
                (event_anchor["event_type"], tenant_id, feedback_event_id),
            ),
            (
                "feedback_ready_feedback_sha256",
                """
                UPDATE yaya_agent_interactions
                SET projection_json=jsonb_set(
                    projection_json,
                    '{feedback_event,feedback_sha256}',
                    to_jsonb(%s::text),
                    false
                )
                WHERE tenant_id=%s AND interaction_id=%s
                """,
                ("0" * 64, tenant_id, interaction_id),
                """
                UPDATE yaya_agent_interactions SET projection_json=%s
                WHERE tenant_id=%s AND interaction_id=%s
                """,
                (Jsonb(projection), tenant_id, interaction_id),
            ),
            (
                "projection_source_sha256",
                """
                UPDATE yaya_agent_interactions
                SET projection_json=jsonb_set(
                    projection_json,
                    '{projection_source,source_sha256}',
                    to_jsonb(%s::text),
                    false
                )
                WHERE tenant_id=%s AND interaction_id=%s
                """,
                ("0" * 64, tenant_id, interaction_id),
                """
                UPDATE yaya_agent_interactions SET projection_json=%s
                WHERE tenant_id=%s AND interaction_id=%s
                """,
                (Jsonb(projection), tenant_id, interaction_id),
            ),
            (
                "feedback_payload_hash_closure",
                """
                UPDATE yaya_agent_interactions
                SET projection_json=jsonb_set(
                    projection_json,'{feedback,message}',to_jsonb(%s::text),false
                )
                WHERE tenant_id=%s AND interaction_id=%s
                """,
                ("Corrupted Product feedback.", tenant_id, interaction_id),
                """
                UPDATE yaya_agent_interactions SET projection_json=%s
                WHERE tenant_id=%s AND interaction_id=%s
                """,
                (Jsonb(projection), tenant_id, interaction_id),
            ),
            (
                "command_request_body_sha256",
                """
                UPDATE yaya_commands SET request_sha256=%s
                WHERE tenant_id=%s AND command_id=%s
                """,
                ("0" * 64, tenant_id, turn.context.command_id),
                """
                UPDATE yaya_commands SET request_sha256=%s
                WHERE tenant_id=%s AND command_id=%s
                """,
                (command_anchor["request_sha256"], tenant_id, turn.context.command_id),
            ),
            (
                "command_origin_context",
                """
                UPDATE yaya_commands
                SET record_json=jsonb_set(
                    record_json,
                    '{$fields,request_context,$fields,trace_id}',
                    to_jsonb(%s::text),
                    false
                )
                WHERE tenant_id=%s AND command_id=%s
                """,
                ("trace_corrupted_command_0001", tenant_id, turn.context.command_id),
                """
                UPDATE yaya_commands SET record_json=%s
                WHERE tenant_id=%s AND command_id=%s
                """,
                (Jsonb(command_anchor["record_json"]), tenant_id, turn.context.command_id),
            ),
            (
                "command_job_origin_context",
                """
                UPDATE yaya_command_jobs
                SET operation_context_json=jsonb_set(
                    operation_context_json,
                    '{$fields,trace_id}',
                    to_jsonb(%s::text),
                    false
                )
                WHERE tenant_id=%s AND command_id=%s
                """,
                ("trace_corrupted_job_0001", tenant_id, turn.context.command_id),
                """
                UPDATE yaya_command_jobs SET operation_context_json=%s
                WHERE tenant_id=%s AND command_id=%s
                """,
                (
                    Jsonb(job_anchor["operation_context_json"]),
                    tenant_id,
                    turn.context.command_id,
                ),
            ),
            (
                "command_job_source_event",
                """
                UPDATE yaya_command_jobs
                SET event_json=jsonb_set(
                    event_json,'{$fields,payload,corrupted}',to_jsonb(TRUE),true
                )
                WHERE tenant_id=%s AND command_id=%s
                """,
                (tenant_id, turn.context.command_id),
                """
                UPDATE yaya_command_jobs SET event_json=%s
                WHERE tenant_id=%s AND command_id=%s
                """,
                (Jsonb(job_anchor["event_json"]), tenant_id, turn.context.command_id),
            ),
            (
                "session_snapshot_identity",
                """
                UPDATE yaya_agent_sessions
                SET snapshot_json=jsonb_set(
                    snapshot_json,'{$fields,world_id}',to_jsonb(%s::text),false
                )
                WHERE tenant_id=%s AND session_id=%s
                """,
                ("world_corrupted_0001", tenant_id, SESSION_ID),
                """
                UPDATE yaya_agent_sessions SET snapshot_json=%s
                WHERE tenant_id=%s AND session_id=%s
                """,
                (Jsonb(session_anchor["snapshot_json"]), tenant_id, SESSION_ID),
            ),
            (
                "task_snapshot_identity",
                """
                UPDATE yaya_tasks
                SET snapshot_json=jsonb_set(
                    snapshot_json,'{$fields,task_id}',to_jsonb(%s::text),false
                )
                WHERE tenant_id=%s AND task_id=%s
                """,
                ("task_corrupted_0001", tenant_id, TASK_ID),
                """
                UPDATE yaya_tasks SET snapshot_json=%s
                WHERE tenant_id=%s AND task_id=%s
                """,
                (Jsonb(task_anchor["snapshot_json"]), tenant_id, TASK_ID),
            ),
            (
                "interaction_created_at",
                """
                UPDATE yaya_agent_interactions SET created_at=created_at+INTERVAL '1 second'
                WHERE tenant_id=%s AND interaction_id=%s
                """,
                (tenant_id, interaction_id),
                """
                UPDATE yaya_agent_interactions SET created_at=%s
                WHERE tenant_id=%s AND interaction_id=%s
                """,
                (interaction_row["created_at"], tenant_id, interaction_id),
            ),
            (
                "feedback_ready_occurred_at",
                """
                UPDATE yaya_events SET occurred_at=occurred_at+INTERVAL '1 second'
                WHERE tenant_id=%s AND event_id=%s
                """,
                (tenant_id, feedback_event_id),
                """
                UPDATE yaya_events SET occurred_at=%s
                WHERE tenant_id=%s AND event_id=%s
                """,
                (event_anchor["occurred_at"], tenant_id, feedback_event_id),
            ),
            (
                "canonical_links",
                """
                UPDATE yaya_agent_interactions
                SET projection_json=jsonb_set(
                    projection_json,'{links,self}',to_jsonb(%s::text),false
                )
                WHERE tenant_id=%s AND interaction_id=%s
                """,
                ("/product-experience/v1/sessions/wrong", tenant_id, interaction_id),
                """
                UPDATE yaya_agent_interactions SET projection_json=%s
                WHERE tenant_id=%s AND interaction_id=%s
                """,
                (Jsonb(projection), tenant_id, interaction_id),
            ),
        )
        for name, corrupt_sql, corrupt_params, restore_sql, restore_params in cases:
            with self.subTest(anchor=name):
                await self._execute_sql(corrupt_sql, corrupt_params)
                try:
                    with self.assertRaises(ProductReadInvariantError):
                        await self.product.list_interactions(
                            turn.context.actor,
                            SESSION_ID,
                            after_sequence=0,
                            limit=50,
                        )
                    with self.assertRaises(ProductReadInvariantError):
                        await self.product.get_interaction(
                            turn.context.actor,
                            SESSION_ID,
                            interaction_id,
                        )
                finally:
                    await self._execute_sql(restore_sql, restore_params)
                self.assertEqual(
                    await self.product.get_interaction(
                        turn.context.actor,
                        SESSION_ID,
                        interaction_id,
                    ),
                    baseline,
                )
        self.assertEqual(await self._database_fingerprint(), before)

    async def test_run_and_evidence_anchor_corruption_fail_closed(self) -> None:
        payload: dict[str, object] = {
            "evidence_kind": "SKILL_RUN",
            "run_id": "run_watering_0001",
            "sandbox_status": "FAILED",
            "world_status": "NOT_ATTEMPTED",
            "intent_count": 0,
        }
        evidence = EvidenceRef(
            "evidence_product_run_0001",
            EvidenceType.SANDBOX_LOG,
            self.base_context.requested_at,
            sha256=canonical_json_sha256(payload),
        )
        turn = await self._prepare_turn(
            1,
            event_type="run_failed",
            evidence_refs=(evidence,),
        )
        await self._seed_failed_run_authority(turn, evidence, payload)
        decision = replace(
            make_agent_decision("The run failed before applying the World."),
            evidence_refs=(evidence,),
        )
        receipt = await self.turns.commit(
            turn.event,
            RoleRoute("run_failed", "world_agent", "handled"),
            decision,
            turn.claim_id,
            turn.context,
        )
        self.assertTrue(receipt.created)
        await self._terminalize_failed_command(turn, evidence)
        interaction_row = await self._fetch_one(
            """
            SELECT interaction_id FROM yaya_agent_interactions
            WHERE tenant_id=%s AND session_id=%s AND sequence=1
            """,
            (turn.context.actor.tenant_id, SESSION_ID),
        )
        interaction_id = cast(str, interaction_row["interaction_id"])
        run_anchor = await self._fetch_one(
            """
            SELECT wire_json FROM yaya_runs WHERE tenant_id=%s AND run_id=%s
            """,
            (turn.context.actor.tenant_id, turn.event.run_id),
        )
        evidence_anchor = await self._fetch_one(
            """
            SELECT evidence_json,payload_sha256 FROM yaya_evidence
            WHERE tenant_id=%s AND evidence_id=%s
            """,
            (turn.context.actor.tenant_id, evidence.evidence_id),
        )
        baseline = await self.product.get_interaction(
            turn.context.actor,
            SESSION_ID,
            interaction_id,
        )
        before = await self._database_fingerprint()
        cases: tuple[
            tuple[
                str,
                LiteralString,
                tuple[object, ...],
                LiteralString,
                tuple[object, ...],
            ],
            ...,
        ] = (
            (
                "run_wire_skill_identity",
                """
                UPDATE yaya_runs
                SET wire_json=jsonb_set(
                    wire_json,'{skill,artifact_sha256}',to_jsonb(%s::text),false
                )
                WHERE tenant_id=%s AND run_id=%s
                """,
                ("0" * 64, turn.context.actor.tenant_id, turn.event.run_id),
                """
                UPDATE yaya_runs SET wire_json=%s
                WHERE tenant_id=%s AND run_id=%s
                """,
                (
                    Jsonb(run_anchor["wire_json"]),
                    turn.context.actor.tenant_id,
                    turn.event.run_id,
                ),
            ),
            (
                "evidence_source_identity",
                """
                UPDATE yaya_evidence
                SET evidence_json=jsonb_set(
                    evidence_json,'{source,source_id}',to_jsonb(%s::text),false
                )
                WHERE tenant_id=%s AND evidence_id=%s
                """,
                (
                    "run_corrupted_product_0001",
                    turn.context.actor.tenant_id,
                    evidence.evidence_id,
                ),
                """
                UPDATE yaya_evidence SET evidence_json=%s
                WHERE tenant_id=%s AND evidence_id=%s
                """,
                (
                    Jsonb(evidence_anchor["evidence_json"]),
                    turn.context.actor.tenant_id,
                    evidence.evidence_id,
                ),
            ),
            (
                "evidence_payload_hash",
                """
                UPDATE yaya_evidence SET payload_sha256=%s
                WHERE tenant_id=%s AND evidence_id=%s
                """,
                ("0" * 64, turn.context.actor.tenant_id, evidence.evidence_id),
                """
                UPDATE yaya_evidence SET payload_sha256=%s
                WHERE tenant_id=%s AND evidence_id=%s
                """,
                (
                    evidence_anchor["payload_sha256"],
                    turn.context.actor.tenant_id,
                    evidence.evidence_id,
                ),
            ),
        )
        for name, corrupt_sql, corrupt_params, restore_sql, restore_params in cases:
            with self.subTest(anchor=name):
                await self._execute_sql(corrupt_sql, corrupt_params)
                try:
                    with self.assertRaises(ProductReadInvariantError):
                        await self.product.list_interactions(
                            turn.context.actor,
                            SESSION_ID,
                            after_sequence=0,
                            limit=50,
                        )
                    with self.assertRaises(ProductReadInvariantError):
                        await self.product.get_interaction(
                            turn.context.actor,
                            SESSION_ID,
                            interaction_id,
                        )
                finally:
                    await self._execute_sql(restore_sql, restore_params)
                self.assertEqual(
                    await self.product.get_interaction(
                        turn.context.actor,
                        SESSION_ID,
                        interaction_id,
                    ),
                    baseline,
                )
        self.assertEqual(await self._database_fingerprint(), before)

    async def test_task_hint_cap_corruption_fails_closed(self) -> None:
        turn = await self._prepare_turn(1, event_type="hint_requested")
        decision = make_agent_decision("Use the smallest loop-boundary hint.")
        directive = decision.teaching_directive
        if directive is None:
            self.fail("hint corruption fixture has no TeachingDirective")
        decision = replace(
            decision,
            draft=replace(
                decision.draft,
                role="teaching_agent",
                response_type="hint",
                hint_level=1,
            ),
            message_key="agent.teaching_agent.hint",
            teaching_directive=replace(
                directive,
                hint_level=1,
                allowed_response_types=("question", "hint"),
            ),
        )
        receipt = await self.turns.commit(
            turn.event,
            RoleRoute("hint_requested", "teaching_agent", "handled"),
            decision,
            turn.claim_id,
            turn.context,
        )
        self.assertTrue(receipt.created)
        await self._terminalize_run_free_command(turn)
        page = await self.product.list_interactions(
            turn.context.actor,
            SESSION_ID,
            after_sequence=0,
            limit=50,
        )
        self.assertEqual(page.interactions[0].interaction["hint_level"], 1)
        task_anchor = await self._fetch_one(
            """
            SELECT snapshot_json FROM yaya_tasks
            WHERE tenant_id=%s AND task_id=%s
            """,
            (turn.context.actor.tenant_id, TASK_ID),
        )
        await self._execute_sql(
            """
            UPDATE yaya_tasks
            SET snapshot_json=jsonb_set(
                snapshot_json,'{$fields,max_hint_level}','0'::jsonb,false
            )
            WHERE tenant_id=%s AND task_id=%s
            """,
            (turn.context.actor.tenant_id, TASK_ID),
        )
        try:
            with self.assertRaisesRegex(ProductReadInvariantError, "hint cap"):
                await self.product.list_interactions(
                    turn.context.actor,
                    SESSION_ID,
                    after_sequence=0,
                    limit=50,
                )
            interaction_id = cast(
                str,
                page.interactions[0].interaction["interaction_id"],
            )
            with self.assertRaisesRegex(ProductReadInvariantError, "hint cap"):
                await self.product.get_interaction(
                    turn.context.actor,
                    SESSION_ID,
                    interaction_id,
                )
        finally:
            await self._execute_sql(
                """
                UPDATE yaya_tasks SET snapshot_json=%s
                WHERE tenant_id=%s AND task_id=%s
                """,
                (
                    Jsonb(task_anchor["snapshot_json"]),
                    turn.context.actor.tenant_id,
                    TASK_ID,
                ),
            )

    async def test_sequence_gap_corruption_fails_closed(self) -> None:
        for index in range(1, 4):
            await self._commit_turn(await self._prepare_turn(index))
        rows = await self._interaction_rows()
        first_id = cast(str, rows[0]["interaction_id"])
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                DELETE FROM yaya_agent_interactions
                WHERE tenant_id=%s AND session_id=%s AND sequence=2
                """,
                (self.base_context.actor.tenant_id, SESSION_ID),
            )
        finally:
            await connection.close()
        with self.assertRaisesRegex(ProductReadInvariantError, "durable gap"):
            await self.product.list_interactions(
                self.base_context.actor,
                SESSION_ID,
                after_sequence=0,
                limit=50,
            )
        with self.assertRaisesRegex(ProductReadInvariantError, "durable gap"):
            await self.product.get_interaction(
                self.base_context.actor,
                SESSION_ID,
                first_id,
            )

    async def test_canonical_outbox_hash_corruption_fails_closed(self) -> None:
        turn = await self._prepare_turn(1)
        await self._commit_turn(turn)
        rows = await self._interaction_rows()
        interaction_id = cast(str, rows[0]["interaction_id"])
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                UPDATE yaya_projection_outbox SET payload_sha256=%s
                WHERE tenant_id=%s AND destination='product_agent_interactions'
                """,
                ("0" * 64, turn.context.actor.tenant_id),
            )
        finally:
            await connection.close()
        with self.assertRaisesRegex(ProductReadInvariantError, "outbox drifted"):
            await self.product.list_interactions(
                turn.context.actor,
                SESSION_ID,
                after_sequence=0,
                limit=50,
            )
        with self.assertRaisesRegex(ProductReadInvariantError, "outbox drifted"):
            await self.product.get_interaction(
                turn.context.actor,
                SESSION_ID,
                interaction_id,
            )

    async def test_concurrent_real_commits_and_failed_retry_are_gap_free(self) -> None:
        first = await self._prepare_turn(1)
        second = await self._prepare_turn(2)
        initial_receipts = await asyncio.gather(
            self.turns.commit(
                first.event,
                RoleRoute("task_started", "world_agent", "handled"),
                make_agent_decision("Concurrent Product response 1"),
                first.claim_id,
                first.context,
            ),
            self.turns.commit(
                second.event,
                RoleRoute("task_started", "world_agent", "handled"),
                make_agent_decision("Concurrent Product response 2"),
                second.claim_id,
                second.context,
            ),
        )
        self.assertTrue(all(receipt.created for receipt in initial_receipts))
        await self._terminalize_run_free_command(first)
        await self._terminalize_run_free_command(second)
        self.assertEqual(
            [row["sequence"] for row in await self._interaction_rows()],
            [1, 2],
        )

        failed = await self._prepare_turn(3)
        contender = await self._prepare_turn(4)
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                CREATE FUNCTION yaya_test_fail_product_sequence() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION 'injected Product projection failure'
                        USING ERRCODE = '40001';
                END
                $$
                """
            )
            await connection.execute(
                """
                CREATE TRIGGER yaya_test_fail_product_sequence
                BEFORE INSERT ON yaya_projection_outbox
                FOR EACH ROW
                WHEN (
                    NEW.destination='product_agent_interactions'
                    AND NEW.payload_json ->> 'turn_id'='turn_product_0003'
                )
                EXECUTE FUNCTION yaya_test_fail_product_sequence()
                """
            )
        finally:
            await connection.close()
        try:
            results = await asyncio.gather(
                self.turns.commit(
                    failed.event,
                    RoleRoute("task_started", "world_agent", "handled"),
                    make_agent_decision("Fail once after sequence allocation"),
                    failed.claim_id,
                    failed.context,
                ),
                self.turns.commit(
                    contender.event,
                    RoleRoute("task_started", "world_agent", "handled"),
                    make_agent_decision("Surviving Product contender"),
                    contender.claim_id,
                    contender.context,
                ),
                return_exceptions=True,
            )
        finally:
            connection = await self.database.connect(autocommit=True)
            try:
                await connection.execute(
                    """
                    DROP TRIGGER IF EXISTS yaya_test_fail_product_sequence
                    ON yaya_projection_outbox
                    """
                )
                await connection.execute(
                    "DROP FUNCTION IF EXISTS yaya_test_fail_product_sequence()"
                )
            finally:
                await connection.close()
        self.assertIsInstance(results[0], psycopg.Error)
        self.assertNotIsInstance(results[1], BaseException)
        self.assertTrue(cast(Any, results[1]).created)
        await self._terminalize_run_free_command(contender)
        rows_after_failure = await self._interaction_rows()
        self.assertEqual([row["sequence"] for row in rows_after_failure], [1, 2, 3])
        self.assertEqual(
            {row["turn_id"] for row in rows_after_failure},
            {first.event.turn_id, second.event.turn_id, contender.event.turn_id},
        )

        retry = await self.turns.commit(
            failed.event,
            RoleRoute("task_started", "world_agent", "handled"),
            make_agent_decision("Fail once after sequence allocation"),
            failed.claim_id,
            failed.context,
        )
        self.assertTrue(retry.created)
        await self._terminalize_run_free_command(failed)
        final_rows = await self._interaction_rows()
        self.assertEqual([row["sequence"] for row in final_rows], [1, 2, 3, 4])
        self.assertEqual(len({row["sequence"] for row in final_rows}), 4)
        connection = await self.database.connect(autocommit=True)
        try:
            with self.assertRaises(psycopg.errors.UniqueViolation):
                await connection.execute(
                    """
                    UPDATE yaya_agent_interactions SET sequence=1
                    WHERE tenant_id=%s AND session_id=%s AND sequence=2
                    """,
                    (self.base_context.actor.tenant_id, SESSION_ID),
                )
        finally:
            await connection.close()
        final_page = await self.product.list_interactions(
            self.base_context.actor,
            SESSION_ID,
            after_sequence=0,
            limit=50,
        )
        self.assertEqual(
            [item.interaction["sequence"] for item in final_page.interactions],
            [1, 2, 3, 4],
        )


if __name__ == "__main__":
    unittest.main()
