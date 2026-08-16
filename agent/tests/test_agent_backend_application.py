from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
CONTRACTS_ROOT = Path(__file__).resolve().parents[1] / "contracts"
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

import psycopg  # noqa: E402
from agent_runtime_fixtures import (  # noqa: E402
    NOW,
    SESSION_ID,
    TASK_ID,
    WORLD_ID,
    make_operation,
    make_session,
    make_skill,
    make_task,
    make_versions,
    make_world_state,
)
from postgres_test_support import postgres_test_server  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from yaya_agent_backend.application import (  # noqa: E402
    AgentTurnApplication,
    BackendApplicationError,
    HttpAttempt,
)
from yaya_agent_backend.codec import encode, plain  # noqa: E402
from yaya_agent_backend.database import PostgresDatabase  # noqa: E402
from yaya_agent_contracts import (  # noqa: E402
    ActiveSkill,
    ActorRef,
    BuildArtifact,
    CertifiedSkill,
    OperationContext,
    RequestContext,
    canonical_json_sha256,
)


def _request_context(context: OperationContext) -> RequestContext:
    return RequestContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        trace_id=context.trace_id,
        requested_at=context.requested_at,
        actor=context.actor,
        content_ref=context.content_ref,
    )


def _attempt(suffix: str) -> HttpAttempt:
    return HttpAttempt(
        request_id=f"req_application_{suffix}",
        trace_id=f"trace_application_{suffix}",
        correlation_id=f"corr_application_{suffix}",
        requested_at=NOW + timedelta(seconds=int(suffix)),
    )


def _body(*, turn_id: str = "turn_application_0001", sequence: int = 1) -> dict[str, object]:
    skill = make_skill().ref
    return {
        "turn_id": turn_id,
        "expected_world_revision": 5,
        "input": {
            "type": "MESSAGE",
            "text": "Water every plot exactly once.",
            "locale": "en-US",
        },
        "skill_bindings": [
            {
                "skill_id": skill.skill_id,
                "skill_version_id": skill.skill_version_id,
                "artifact_sha256": skill.artifact_sha256,
                "certification_id": skill.certification_id,
            }
        ],
        "client_state": {
            "last_event_sequence": 40,
            "client_turn_sequence": sequence,
        },
    }


def _raw(body: dict[str, object]) -> bytes:
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


class AgentBackendApplicationTests(unittest.IsolatedAsyncioTestCase):
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
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                "DROP TRIGGER IF EXISTS yaya_test_fail_job_insert ON yaya_command_jobs"
            )
            await connection.execute("DROP FUNCTION IF EXISTS yaya_test_fail_job_insert()")
            await connection.execute(
                """
                TRUNCATE yaya_events,yaya_outbox,yaya_audit,
                  yaya_registry_active,yaya_registry_certifications,
                  yaya_skills,yaya_agent_sessions,yaya_worlds,yaya_tasks CASCADE
                """
            )
        finally:
            await connection.close()
        self.origin = make_operation()
        await self._seed_authority(self.origin)
        self.application = AgentTurnApplication(
            self.database,
            CONTRACTS_ROOT,
            make_versions(),
        )

    async def _seed_authority(self, context: OperationContext) -> None:
        task = make_task(context)
        session = make_session(operation=context)
        skill = make_skill(context)
        state = make_world_state()
        artifact = BuildArtifact(
            artifact_sha256=skill.ref.artifact_sha256,
            source_sha256=skill.source_sha256,
            compiler_profile="gcc-cpp20",
            compiler_version="gcc 15",
            sandbox_image_digest="gcc@sha256:" + "c" * 64,
            test_suite_version="watering-1",
            artifact_uri="file:///application-test/skill",
        )
        certified = CertifiedSkill(
            certification_id=skill.ref.certification_id,
            skill_id=skill.ref.skill_id,
            skill_version_id=skill.ref.skill_version_id,
            semantic_version="1.0.0",
            artifact=artifact,
            capabilities=("WORLD_READ", "WATER"),
            certified_at=NOW,
            revoked_at=None,
        )
        active = ActiveSkill(certified, 1, NOW)
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
            await connection.execute(
                """
                INSERT INTO yaya_registry_certifications(
                  tenant_id,certification_id,skill_id,skill_version_id,
                  artifact_sha256,record_json,rejected
                ) VALUES (%s,%s,%s,%s,%s,%s,FALSE)
                """,
                (
                    context.actor.tenant_id,
                    certified.certification_id,
                    certified.skill_id,
                    certified.skill_version_id,
                    certified.artifact.artifact_sha256,
                    Jsonb(encode(certified)),
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_registry_active(
                  tenant_id,actor_id,skill_id,record_json,revision
                ) VALUES (%s,%s,%s,%s,1)
                """,
                (
                    context.actor.tenant_id,
                    context.actor.actor_id,
                    certified.skill_id,
                    Jsonb(encode(active)),
                ),
            )
        finally:
            await connection.close()

    async def _accept(
        self,
        body: dict[str, object],
        *,
        key: str,
        attempt: str,
        raw_body: bytes | None = None,
        actor: ActorRef | None = None,
    ):
        return await self.application.accept(
            actor=actor or self.origin.actor,
            attempt=_attempt(attempt),
            session_id=SESSION_ID,
            idempotency_key=key,
            raw_body=_raw(body) if raw_body is None else raw_body,
            body=body,
        )

    async def test_acceptance_receipt_replay_and_turn_conflicts_are_atomic(self) -> None:
        body = _body()
        raw_body = _raw(body)
        first = await self._accept(
            body,
            key="agent-turn:application:0001",
            attempt="1",
            raw_body=raw_body,
        )
        self.assertFalse(first.replayed)
        self.assertEqual(first.receipt["status"], "ACCEPTED")
        self.assertEqual(first.receipt["trace_id"], "trace_application_1")

        changed_roles = replace(
            self.origin.actor,
            roles=("game:player", "learner:read"),
        )
        replay = await self._accept(
            body,
            key="agent-turn:application:0001",
            attempt="2",
            raw_body=raw_body,
            actor=changed_roles,
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.command.command_id, first.command.command_id)
        self.assertEqual(replay.receipt, first.receipt)
        self.assertEqual(replay.receipt["trace_id"], "trace_application_1")
        self.assertEqual(replay.operation_context.trace_id, "trace_application_1")

        whitespace_variant = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        with self.assertRaises(BackendApplicationError) as byte_conflict:
            await self._accept(
                body,
                key="agent-turn:application:0001",
                attempt="3",
                raw_body=whitespace_variant,
            )
        self.assertEqual(byte_conflict.exception.code, "IDEMPOTENCY_KEY_REUSED")

        changed_body = _body()
        changed_input = dict(changed_body["input"])  # type: ignore[arg-type]
        changed_input["text"] = "A different byte-level request."
        changed_body["input"] = changed_input
        with self.assertRaises(BackendApplicationError) as reused:
            await self._accept(
                changed_body,
                key="agent-turn:application:0001",
                attempt="3",
            )
        self.assertEqual(reused.exception.code, "IDEMPOTENCY_KEY_REUSED")

        same_turn_next_sequence = _body(sequence=2)
        with self.assertRaises(BackendApplicationError) as duplicate_turn:
            await self._accept(
                same_turn_next_sequence,
                key="agent-turn:application:0002",
                attempt="4",
            )
        self.assertEqual(duplicate_turn.exception.code, "EVENT_SEQUENCE_GAP")

        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM yaya_commands) AS commands,
                  (SELECT count(*) FROM yaya_command_jobs) AS jobs,
                  (SELECT request_sha256 FROM yaya_commands LIMIT 1) AS request_sha256,
                  (SELECT client_turn_sequence FROM yaya_agent_sessions
                    WHERE tenant_id=%s AND session_id=%s) AS sequence
                """,
                (self.origin.actor.tenant_id, SESSION_ID),
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual((row["commands"], row["jobs"], row["sequence"]), (1, 1, 1))
        self.assertEqual(row["request_sha256"], hashlib.sha256(raw_body).hexdigest())

    async def test_accept_rejects_assigned_task_mismatch_with_catalog_error(self) -> None:
        body = _body()
        body["input"] = {"type": "ASSIGNED_TASK", "task_id": "task_other_0001"}
        with self.assertRaises(BackendApplicationError) as mismatch:
            await self._accept(
                body,
                key="agent-turn:application:task-mismatch",
                attempt="5",
            )
        self.assertEqual(mismatch.exception.code, "CONTENT_VERSION_MISMATCH")
        self.assertEqual(mismatch.exception.http_status, 409)

    async def test_concurrent_first_attempts_create_exactly_one_command_and_job(self) -> None:
        body = _body()
        raw_body = _raw(body)
        results = await asyncio.gather(
            self._accept(
                body,
                key="agent-turn:application:concurrent",
                attempt="7",
                raw_body=raw_body,
            ),
            self._accept(
                body,
                key="agent-turn:application:concurrent",
                attempt="8",
                raw_body=raw_body,
            ),
        )
        self.assertEqual(sorted(result.replayed for result in results), [False, True])
        self.assertEqual(results[0].command.command_id, results[1].command.command_id)
        self.assertEqual(results[0].receipt, results[1].receipt)
        first = next(result for result in results if not result.replayed)
        self.assertEqual(first.receipt["trace_id"], first.operation_context.trace_id)

        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM yaya_commands) AS commands,
                  (SELECT count(*) FROM yaya_command_jobs) AS jobs,
                  (SELECT client_turn_sequence FROM yaya_agent_sessions
                    WHERE tenant_id=%s AND session_id=%s) AS sequence
                """,
                (self.origin.actor.tenant_id, SESSION_ID),
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual((row["commands"], row["jobs"], row["sequence"]), (1, 1, 1))

    async def test_known_statement_failure_rolls_back_without_unknown_commit_state(self) -> None:
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                CREATE FUNCTION yaya_test_fail_job_insert() RETURNS trigger AS $$
                BEGIN
                  RAISE EXCEPTION 'injected known job insert failure' USING ERRCODE='23514';
                END
                $$ LANGUAGE plpgsql
                """
            )
            await connection.execute(
                """
                CREATE TRIGGER yaya_test_fail_job_insert
                BEFORE INSERT ON yaya_command_jobs
                FOR EACH ROW EXECUTE FUNCTION yaya_test_fail_job_insert()
                """
            )
        finally:
            await connection.close()

        with self.assertRaises(BackendApplicationError) as failure:
            await self._accept(
                _body(),
                key="agent-turn:application:known-rollback",
                attempt="9",
            )
        self.assertNotEqual(failure.exception.code, "UNKNOWN_COMMIT_STATE")

        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM yaya_commands) AS commands,
                  (SELECT count(*) FROM yaya_command_jobs) AS jobs,
                  (SELECT client_turn_sequence FROM yaya_agent_sessions
                    WHERE tenant_id=%s AND session_id=%s) AS sequence
                """,
                (self.origin.actor.tenant_id, SESSION_ID),
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual((row["commands"], row["jobs"], row["sequence"]), (0, 0, 0))

    async def test_commit_acknowledged_deferred_constraint_failure_is_not_unknown(self) -> None:
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute("DROP TABLE IF EXISTS yaya_test_deferred_boundary")
            await connection.execute(
                """
                CREATE TABLE yaya_test_deferred_boundary(
                  value INTEGER,
                  CONSTRAINT yaya_test_deferred_boundary_unique UNIQUE(value)
                    DEFERRABLE INITIALLY DEFERRED
                )
                """
            )
        finally:
            await connection.close()

        try:
            with self.assertRaises(psycopg.errors.UniqueViolation):
                async with self.database.transaction_with_commit_boundary() as transaction:
                    await transaction.execute(
                        "INSERT INTO yaya_test_deferred_boundary(value) VALUES (1),(1)"
                    )
        finally:
            connection = await self.database.connect(autocommit=True)
            try:
                await connection.execute("DROP TABLE IF EXISTS yaya_test_deferred_boundary")
            finally:
                await connection.close()

    async def test_command_and_world_queries_validate_wire_and_reject_durable_drift(self) -> None:
        accepted = await self._accept(
            _body(),
            key="agent-turn:application:query",
            attempt="6",
        )
        command = await self.application.get_command(
            accepted.command.command_id,
            self.origin.actor,
        )
        self.assertEqual(command.payload["command_id"], accepted.command.command_id)
        world = await self.application.get_world(WORLD_ID, self.origin.actor)
        self.assertEqual(world.payload["revision"], 5)
        self.assertEqual(world.headers["X-World-Revision"], "5")
        self.assertIn(WORLD_ID, world.headers["ETag"])

        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                UPDATE yaya_commands SET revision=revision+1,status='VALIDATING'
                WHERE tenant_id=%s AND command_id=%s
                """,
                (self.origin.actor.tenant_id, accepted.command.command_id),
            )
        finally:
            await connection.close()
        with self.assertRaises(BackendApplicationError) as drifted:
            await self.application.get_command(
                accepted.command.command_id,
                self.origin.actor,
            )
        self.assertEqual(drifted.exception.code, "INVARIANT_VIOLATION")

    async def test_evidence_query_recomputes_the_frozen_payload_hash(self) -> None:
        evidence_id = "evidence_application_0001"
        evidence_payload: dict[str, object] = {
            "evidence_kind": "SKILL_RUN",
            "run_id": "run_application_0001",
            "sandbox_status": "SUCCEEDED",
            "world_status": "REJECTED",
            "intent_count": 7,
        }
        payload_hash = canonical_json_sha256(evidence_payload)
        request_context = plain(_request_context(self.origin))
        versions = {
            key: value
            for key, value in plain(make_versions()).items()  # type: ignore[union-attr]
            if value is not None
        }
        evidence = {
            "request_context": request_context,
            "evidence_ref": {
                "evidence_id": evidence_id,
                "evidence_type": "SANDBOX_LOG",
                "created_at": plain(NOW),
                "sha256": payload_hash,
            },
            "subject": {"learner_id": self.origin.actor.actor_id},
            "source": {
                "source_type": "SKILL_RUN",
                "source_id": "run_application_0001",
                "command_id": self.origin.command_id,
                "world_id": WORLD_ID,
            },
            "occurred_at": plain(NOW),
            "recorded_at": plain(NOW),
            "integrity": {
                "payload_sha256": payload_hash,
                "previous_evidence_sha256": None,
            },
            "payload": evidence_payload,
            "related_evidence": [],
            "versions": versions,
        }
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                INSERT INTO yaya_evidence(
                  tenant_id,evidence_id,actor_id,content_hash,evidence_type,
                  payload_sha256,evidence_json
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    self.origin.actor.tenant_id,
                    evidence_id,
                    self.origin.actor.actor_id,
                    self.origin.content_ref.content_hash,
                    "SANDBOX_LOG",
                    payload_hash,
                    Jsonb(evidence),
                ),
            )
        finally:
            await connection.close()

        result = await self.application.get_evidence(evidence_id, self.origin.actor)
        self.assertEqual(result.payload, evidence)
        self.assertEqual(result.headers["ETag"], f'"{payload_hash}"')

        tampered = dict(evidence)
        tampered_payload = dict(evidence_payload)
        tampered_payload["intent_count"] = 8
        tampered["payload"] = tampered_payload
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                UPDATE yaya_evidence SET evidence_json=%s
                WHERE tenant_id=%s AND evidence_id=%s
                """,
                (Jsonb(tampered), self.origin.actor.tenant_id, evidence_id),
            )
        finally:
            await connection.close()
        with self.assertRaises(BackendApplicationError) as invalid:
            await self.application.get_evidence(evidence_id, self.origin.actor)
        self.assertEqual(invalid.exception.code, "INVARIANT_VIOLATION")


if __name__ == "__main__":
    unittest.main()
