from __future__ import annotations

import asyncio
import hashlib
import sys
import unittest
from pathlib import Path
from typing import cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from agent_runtime_fixtures import (  # noqa: E402
    make_operation,
    make_skill_ref,
    make_versions,
    make_world_state,
)
from postgres_test_support import postgres_test_server  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from yaya_agent_backend.codec import encode  # noqa: E402
from yaya_agent_backend.database import PostgresDatabase  # noqa: E402
from yaya_agent_backend.world import WateringWorldEngine  # noqa: E402
from yaya_agent_backend.world_uow import (  # noqa: E402
    PostgresWorldUnitOfWork,
    world_commit_identifier,
)
from yaya_agent_contracts import (  # noqa: E402
    Failure,
    NewCommand,
    OperationContext,
    RequestContext,
    Success,
    UncommittedEvent,
    WaterIntent,
    WorldAtomicCommit,
    WorldCommand,
    WorldSnapshot,
    canonical_json_sha256,
)

WORLD_ID = "world_uow_watering_0001"
STREAM_ID = f"world:{WORLD_ID}"
RUN_ID = "run_world_uow_0001"


def _request_context(context: OperationContext) -> RequestContext:
    return RequestContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        trace_id=context.trace_id,
        requested_at=context.requested_at,
        actor=context.actor,
        content_ref=context.content_ref,
    )


def _atomic_request(context: OperationContext) -> WorldAtomicCommit:
    intents = tuple(
        WaterIntent(
            intent_id=f"intent_world_uow_{index:04d}",
            actor_entity_id="avatar_0001",
            expected_world_revision=5,
            plot_id=f"plot_{index:04d}",
            amount_ml=100,
        )
        for index in range(1, 9)
    )
    state = make_world_state()
    proposal = WateringWorldEngine().stage(
        WorldSnapshot(
            request_context=_request_context(context),
            world_id=WORLD_ID,
            revision=5,
            last_event_sequence=0,
            state_hash=canonical_json_sha256(state),
            generated_at=context.requested_at,
            world_rules_version="farm-rules-1",
            state=state,
        ),
        make_skill_ref(),
        intents,
    )
    committed_at = context.requested_at.astimezone().isoformat()
    event = UncommittedEvent(
        event_type="world.committed",
        event_version=1,
        producer="world_engine",
        trace_id=context.trace_id,
        command_id=context.command_id,
        correlation_id=context.correlation_id,
        causation_id=context.command_id,
        content_ref=context.content_ref,
        payload={
            "commit_id": world_commit_identifier(
                context.actor.tenant_id,
                STREAM_ID,
                RUN_ID,
                5,
            ),
            "run_id": RUN_ID,
            "world_id": WORLD_ID,
            "previous_world_revision": 5,
            "world_revision": 6,
            "state_hash": proposal.state_hash,
            "applied_intent_ids": [intent.intent_id for intent in intents],
            "committed_at": committed_at,
            "evidence_refs": [],
        },
    )
    return WorldAtomicCommit(
        stream_id=STREAM_ID,
        expected_stream_sequence=0,
        command=WorldCommand(
            run_id=RUN_ID,
            world_id=WORLD_ID,
            expected_world_revision=5,
            world_rules_version="farm-rules-1",
            skill_ref=make_skill_ref(),
            intents=intents,
        ),
        events=(event,),
        outbox_messages=(),
    )


class AgentBackendWorldUnitOfWorkTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._server_context = postgres_test_server()
        cls.server = cls._server_context.__enter__()
        cls.database = PostgresDatabase(cls.server.dsn)
        asyncio.run(cls.database.migrate())

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server_context.__exit__(None, None, None)

    async def asyncSetUp(self) -> None:
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                "DROP TRIGGER IF EXISTS yaya_test_fail_world_projection ON yaya_projection_outbox"
            )
            await connection.execute("DROP FUNCTION IF EXISTS yaya_test_fail_world_projection()")
            await connection.execute(
                "TRUNCATE yaya_projection_outbox,yaya_events,yaya_outbox,"
                "yaya_command_jobs,yaya_commands,yaya_worlds CASCADE"
            )
        finally:
            await connection.close()
        self.context = make_operation(command_id="cmd_world_uow_0001")
        await self._seed(self.context)
        self.uow = PostgresWorldUnitOfWork(self.database, WateringWorldEngine())

    async def _seed(self, context: OperationContext) -> None:
        state = make_world_state()
        command = NewCommand(
            command_type="EXECUTE_AGENT_TURN",
            idempotency_key="agent-turn:world-uow:0001",
            request_sha256=hashlib.sha256(context.command_id.encode()).hexdigest(),
            versions=make_versions(),
        )
        record = command.initial_record(context, context.requested_at)
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                INSERT INTO yaya_worlds(
                  tenant_id,world_id,actor_id,content_hash,stream_id,revision,
                  last_event_sequence,state_hash,world_rules_version,state_json,
                  request_context_json
                ) VALUES (%s,%s,%s,%s,%s,5,0,%s,'farm-rules-1',%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    WORLD_ID,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    STREAM_ID,
                    canonical_json_sha256(state),
                    Jsonb(state),
                    Jsonb(encode(_request_context(context))),
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_commands(
                  tenant_id,actor_id,operation,idempotency_key,command_id,
                  request_sha256,content_hash,revision,status,updated_at,record_json
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    context.actor.actor_id,
                    command.operation,
                    command.idempotency_key,
                    context.command_id,
                    command.request_sha256,
                    context.content_ref.content_hash,
                    record.revision,
                    record.status.value,
                    record.updated_at,
                    Jsonb(encode(record)),
                ),
            )
        finally:
            await connection.close()

    async def _counts(self) -> tuple[int, int, int]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT
                  (SELECT revision FROM yaya_worlds WHERE world_id=%s) AS revision,
                  (SELECT COUNT(*) FROM yaya_events) AS events,
                  (SELECT COUNT(*) FROM yaya_projection_outbox) AS projections
                """,
                (WORLD_ID,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise AssertionError("PostgreSQL count query returned no row")
            return (
                cast(int, row["revision"]),
                cast(int, row["events"]),
                cast(int, row["projections"]),
            )
        finally:
            await connection.close()

    async def test_public_port_atomically_commits_cas_events_and_projection_outbox(self) -> None:
        request = _atomic_request(self.context)

        result = await self.uow.commit(request, self.context)

        self.assertIsInstance(result, Success)
        receipt = cast(Success, result).value
        self.assertEqual(receipt.stream_id, STREAM_ID)
        self.assertEqual(receipt.world.previous_revision, 5)
        self.assertEqual(receipt.world.world_revision, 6)
        self.assertEqual(receipt.events.previous_sequence, 0)
        self.assertEqual(receipt.events.next_sequence, 1)
        self.assertEqual(await self._counts(), (6, 1, 1))

        replay = await self.uow.commit(request, self.context)
        self.assertIsInstance(replay, Failure)
        self.assertEqual(cast(Failure, replay).error.code, "WORLD_REVISION_CONFLICT")
        self.assertEqual(await self._counts(), (6, 1, 1))

    async def test_projection_failure_rolls_back_world_cas_and_events(self) -> None:
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                CREATE FUNCTION yaya_test_fail_world_projection() RETURNS trigger AS $$
                BEGIN
                  RAISE EXCEPTION 'injected projection failure' USING ERRCODE='40001';
                END;
                $$ LANGUAGE plpgsql
                """
            )
            await connection.execute(
                """
                CREATE TRIGGER yaya_test_fail_world_projection
                BEFORE INSERT ON yaya_projection_outbox
                FOR EACH ROW EXECUTE FUNCTION yaya_test_fail_world_projection()
                """
            )
        finally:
            await connection.close()

        result = await self.uow.commit(_atomic_request(self.context), self.context)

        self.assertIsInstance(result, Failure)
        self.assertEqual(cast(Failure, result).error.code, "DEPENDENCY_UNAVAILABLE")
        self.assertEqual(await self._counts(), (5, 0, 0))

    async def test_same_connection_participant_joins_and_obeys_outer_rollback(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "rollback outer transaction"):
            async with self.database.transaction() as connection:
                result = await self.uow.participant.commit_on(
                    connection,
                    _atomic_request(self.context),
                    self.context,
                )
                self.assertIsInstance(result, Success)
                raise RuntimeError("rollback outer transaction")

        self.assertEqual(await self._counts(), (5, 0, 0))

    def test_invocation_has_no_world_persistence_sql_bypass(self) -> None:
        backend = PACKAGE_ROOT / "yaya_agent_backend"
        invocation = (backend / "invocation.py").read_text(encoding="utf-8")
        world_uow = (backend / "world_uow.py").read_text(encoding="utf-8")
        forbidden = (
            "UPDATE yaya_worlds",
            "INSERT INTO yaya_events",
            "INSERT INTO yaya_projection_outbox",
            "'world_events'",
        )
        for statement in forbidden:
            self.assertNotIn(statement, invocation)
            self.assertIn(statement, world_uow)


if __name__ == "__main__":
    unittest.main()
