from __future__ import annotations

import asyncio
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
    SESSION_ID,
    TASK_ID,
    WORLD_ID,
    make_operation,
    make_session,
    make_skill,
    make_task,
    make_versions,
)
from postgres_test_support import postgres_test_server  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from yaya_agent_backend.codec import encode  # noqa: E402
from yaya_agent_backend.database import PostgresDatabase  # noqa: E402
from yaya_agent_backend.stores import (  # noqa: E402
    PostgresAuditStore,
    PostgresCommandStore,
    PostgresEventStore,
    PostgresLearnerStore,
    PostgresOutboxStore,
    PostgresRegistryStore,
)
from yaya_agent_contracts import (  # noqa: E402
    ActorRef,
    AuditQuery,
    AuditRecord,
    BuildArtifact,
    CertificationEvidence,
    CertifiedSkill,
    CommandStatus,
    CommandTransition,
    ContentRef,
    ContractError,
    DeliveryPayload,
    DeliveryReceipt,
    ErrorCategory,
    EvidenceRef,
    EvidenceType,
    Failure,
    FeishuReportDraftBody,
    NewCommand,
    OperationContext,
    OutboxMessage,
    OutboxStatus,
    RuntimeEvent,
    RuntimeEventType,
    Success,
    TestCaseResult,
    UncommittedEvent,
)
from yaya_agent_runtime import CompileResultSnapshot  # noqa: E402


def _role_drift(context: OperationContext) -> OperationContext:
    actor = ActorRef(
        tenant_id=context.actor.tenant_id,
        actor_id=context.actor.actor_id,
        actor_type=context.actor.actor_type,
        roles=("game:player", "learner:read"),
    )
    return replace(context, actor=actor)


def _new_command(request_hash: str = "8" * 64) -> NewCommand:
    return NewCommand(
        command_type="EXECUTE_AGENT_TURN",
        idempotency_key="agent-turn:store-test:0001",
        request_sha256=request_hash,
        versions=make_versions(),
    )


def _outbox(context: OperationContext) -> OutboxMessage:
    payload = DeliveryPayload(
        delivery_id="delivery_store_test_0001",
        operation="FEISHU_REPORT_DRAFT",
        deduplication_key="feishu-report:store-test:0001",
        attempt=1,
        body=FeishuReportDraftBody("report_store_test_0001"),
    )
    return OutboxMessage(
        message_id=payload.delivery_id,
        destination=payload.operation,
        idempotency_key=payload.deduplication_key,
        payload=payload,
        created_at=NOW,
        operation_context=context,
    )


def _learner_event(
    context: OperationContext,
    *,
    sequence: int = 1,
    suffix: str = "0001",
) -> RuntimeEvent:
    evidence = EvidenceRef(
        evidence_id=f"evidence_learner_{suffix}",
        evidence_type=EvidenceType.ACTION_LOG,
        created_at=NOW,
        sha256="c" * 64,
    )
    return RuntimeEvent(
        event_id=f"evt_learner_{suffix}",
        event_type=RuntimeEventType.LEARNER_EVIDENCE_RECORDED,
        event_version=1,
        stream_id=f"learner:{context.actor.actor_id}",
        sequence=sequence,
        occurred_at=NOW + timedelta(seconds=sequence),
        producer="agent_hub",
        trace_id=context.trace_id,
        command_id=context.command_id,
        correlation_id=context.correlation_id,
        causation_id=context.command_id,
        content_ref=context.content_ref,
        payload={
            "learner_id": context.actor.actor_id,
            "evidence_refs": [
                {
                    "evidence_id": evidence.evidence_id,
                    "evidence_type": evidence.evidence_type.value,
                    "created_at": evidence.created_at.isoformat().replace("+00:00", "Z"),
                    "sha256": evidence.sha256,
                }
            ],
            "competency_ids": ["for_loop"],
            "recorded_at": (NOW + timedelta(seconds=sequence)).isoformat().replace("+00:00", "Z"),
        },
    )


class AgentBackendStoreTests(unittest.IsolatedAsyncioTestCase):
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
                """
                TRUNCATE yaya_commands,yaya_events,yaya_outbox,yaya_audit,
                  yaya_learner_models,yaya_registry_active,
                  yaya_registry_certifications,yaya_compile_results,yaya_skills,
                  yaya_agent_sessions,yaya_worlds,yaya_tasks CASCADE
                """
            )
        finally:
            await connection.close()

    async def _seed_skill_build(
        self,
        context: OperationContext,
    ) -> tuple[CertificationEvidence, CertifiedSkill]:
        task = make_task(context)
        session = make_session(operation=context)
        skill = make_skill(context)
        artifact = BuildArtifact(
            artifact_sha256=skill.ref.artifact_sha256,
            source_sha256=skill.source_sha256,
            compiler_profile="cpp20-release",
            compiler_version="msvc-19.44",
            sandbox_image_digest="sha256:" + "d" * 64,
            test_suite_version="watering-tests-1",
            artifact_uri="artifact://watering/skill.exe",
        )
        test = TestCaseResult(
            test_case_id="watering_required_0001",
            visibility="HIDDEN",
            status="PASSED",
            duration_ms=10,
            diagnostic_codes=(),
            evidence_refs=(),
        )
        evidence = CertificationEvidence(
            build_id="build_store_test_0001",
            artifact=artifact,
            tests=(test,),
            all_required_tests_passed=True,
            evidence_refs=(),
        )
        compile_result = CompileResultSnapshot(
            build_id=evidence.build_id,
            skill_ref=skill.ref,
            succeeded=True,
            diagnostics=(),
            evidence_refs=(),
            request_context=context,
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
            metadata={"build_id": evidence.build_id},
        )
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                INSERT INTO yaya_tasks(tenant_id,task_id,actor_id,content_hash,snapshot_json)
                VALUES (%s,%s,%s,%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    TASK_ID,
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
                ) VALUES (%s,%s,%s,%s,%s,0,0,%s,'farm-rules-1',%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    WORLD_ID,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    f"world:{WORLD_ID}",
                    "a" * 64,
                    Jsonb({}),
                    Jsonb({}),
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
                  session_id,content_hash,artifact_sha256,snapshot_json
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                INSERT INTO yaya_compile_results(
                  tenant_id,build_id,actor_id,content_hash,snapshot_json
                ) VALUES (%s,%s,%s,%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    evidence.build_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    Jsonb(encode(compile_result)),
                ),
            )
        finally:
            await connection.close()
        return evidence, certified

    async def test_command_idempotency_role_drift_and_cas(self) -> None:
        context = make_operation()
        store = PostgresCommandStore(self.database)
        created = await store.accept_once(_new_command(), context)
        self.assertIsInstance(created, Success)
        self.assertTrue(created.value.created if isinstance(created, Success) else False)

        replay = await store.accept_once(_new_command(), _role_drift(context))
        self.assertIsInstance(replay, Success)
        self.assertFalse(replay.value.created if isinstance(replay, Success) else True)
        self.assertEqual(
            replay.value.command.request_context.actor.roles if isinstance(replay, Success) else (),
            ("game:player",),
        )

        other_content = replace(
            context,
            content_ref=ContentRef("YAYA_FARM_001", "2.0.0", "b" * 64),
            command_id="cmd_store_other_0001",
        )
        cross_content_replay = await store.accept_once(_new_command(), other_content)
        self.assertIsInstance(cross_content_replay, Success)
        self.assertFalse(
            cross_content_replay.value.created
            if isinstance(cross_content_replay, Success)
            else True
        )
        self.assertEqual(
            (
                cross_content_replay.value.command.request_context.content_ref
                if isinstance(cross_content_replay, Success)
                else None
            ),
            context.content_ref,
        )

        conflict = await store.accept_once(_new_command("7" * 64), other_content)
        self.assertIsInstance(conflict, Failure)
        self.assertEqual(
            conflict.error.code if isinstance(conflict, Failure) else "", "IDEMPOTENCY_KEY_REUSED"
        )

        if not isinstance(created, Success):
            self.fail("command create did not succeed")
        previous = created.value.command
        next_record = replace(
            previous,
            status=CommandStatus.VALIDATING,
            stage="VALIDATE",
            updated_at=previous.updated_at + timedelta(milliseconds=1),
            revision=previous.revision + 1,
        )
        transition = CommandTransition(previous, next_record)
        applied = await store.transition(transition, context)
        self.assertEqual(applied, Success(next_record))
        stale = await store.transition(transition, context)
        self.assertIsInstance(stale, Failure)
        self.assertEqual(
            stale.error.code if isinstance(stale, Failure) else "", "EVENT_SEQUENCE_GAP"
        )

    async def test_known_transaction_body_failure_is_not_unknown_commit(self) -> None:
        context = make_operation()
        store = PostgresCommandStore(self.database)
        created = await store.accept_once(_new_command(), context)
        if not isinstance(created, Success):
            self.fail("command create did not succeed")
        previous = created.value.command
        next_record = replace(
            previous,
            status=CommandStatus.VALIDATING,
            stage="VALIDATE",
            updated_at=previous.updated_at + timedelta(milliseconds=1),
            revision=previous.revision + 1,
        )
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                CREATE FUNCTION yaya_test_known_rollback() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                  RAISE EXCEPTION 'known rollback' USING ERRCODE='08006';
                END
                $$
                """
            )
            await connection.execute(
                """
                CREATE TRIGGER yaya_test_known_rollback
                BEFORE UPDATE ON yaya_commands FOR EACH ROW
                EXECUTE FUNCTION yaya_test_known_rollback()
                """
            )
        finally:
            await connection.close()
        outcome = await store.transition(CommandTransition(previous, next_record), context)
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute("DROP TRIGGER yaya_test_known_rollback ON yaya_commands")
            await connection.execute("DROP FUNCTION yaya_test_known_rollback()")
        finally:
            await connection.close()
        self.assertIsInstance(outcome, Failure)
        self.assertEqual(
            outcome.error.code if isinstance(outcome, Failure) else "",
            "DEPENDENCY_UNAVAILABLE",
        )
        unchanged = await store.get(context.command_id, context)
        self.assertEqual(unchanged, Success(previous))

    async def test_event_stream_sequence_cas_and_read(self) -> None:
        context = make_operation()
        command = await PostgresCommandStore(self.database).accept_once(_new_command(), context)
        self.assertIsInstance(command, Success)
        store = PostgresEventStore(self.database)
        event = UncommittedEvent(
            event_type="agent.test.recorded",
            event_version=1,
            producer="agent_hub",
            trace_id=context.trace_id,
            command_id=context.command_id,
            correlation_id=context.correlation_id,
            causation_id=context.command_id,
            content_ref=context.content_ref,
            payload={"result": "ok"},
        )
        first = await store.append("agent-session:store_test_0001", "NO_STREAM", (event,), context)
        self.assertIsInstance(first, Success)
        self.assertEqual(first.value.next_sequence if isinstance(first, Success) else 0, 1)
        stale = await store.append("agent-session:store_test_0001", 0, (event,), context)
        self.assertIsInstance(stale, Failure)
        self.assertEqual(
            stale.error.code if isinstance(stale, Failure) else "", "EVENT_SEQUENCE_GAP"
        )
        page = await store.read_stream("agent-session:store_test_0001", 0, 10, context)
        self.assertIsInstance(page, Success)
        self.assertEqual(len(page.value.items) if isinstance(page, Success) else 0, 1)

        for reserved_stream in (
            f"learner:{context.actor.actor_id}",
            f"learner-model:{context.actor.actor_id}",
        ):
            with self.subTest(reserved_stream=reserved_stream):
                forbidden_learner = await store.append(
                    reserved_stream,
                    "NO_STREAM",
                    (event,),
                    context,
                )
                self.assertIsInstance(forbidden_learner, Failure)
                self.assertEqual(
                    forbidden_learner.error.code if isinstance(forbidden_learner, Failure) else "",
                    "INVARIANT_VIOLATION",
                )
                reserved_page = await store.read_stream(
                    reserved_stream,
                    0,
                    10,
                    context,
                )
                self.assertEqual(
                    reserved_page.value.items if isinstance(reserved_page, Success) else None,
                    (),
                )

        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                INSERT INTO yaya_worlds(
                  tenant_id,world_id,actor_id,content_hash,stream_id,revision,
                  last_event_sequence,state_hash,world_rules_version,state_json,
                  request_context_json
                ) VALUES (%s,'world_store_test_0001',%s,%s,%s,0,0,%s,
                          'farm-rules-1',%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    "world:store_test_0001",
                    "a" * 64,
                    Jsonb({}),
                    Jsonb({}),
                ),
            )
        finally:
            await connection.close()
        forbidden = await store.append("world:store_test_0001", "NO_STREAM", (event,), context)
        self.assertIsInstance(forbidden, Failure)
        self.assertEqual(
            forbidden.error.code if isinstance(forbidden, Failure) else "",
            "INVARIANT_VIOLATION",
        )

    async def test_audit_role_drift_uses_stable_actor_authority(self) -> None:
        context = make_operation()
        record = AuditRecord(
            audit_id="audit_store_test_0001",
            occurred_at=NOW,
            operation="READ_COMMAND",
            outcome="ALLOWED",
            actor=context.actor,
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            trace_id=context.trace_id,
            resource_type="COMMAND",
            resource_id=context.command_id,
            purpose=None,
            subject_hash=None,
            evidence_ids=(),
            error_code=None,
            details={},
        )
        store = PostgresAuditStore(self.database)
        drifted = _role_drift(context)
        self.assertEqual(await store.append(record, drifted), Success(record))
        page = await store.query(AuditQuery(), drifted)
        self.assertIsInstance(page, Success)
        self.assertEqual(page.value.items if isinstance(page, Success) else (), (record,))

    async def test_learner_project_requires_durable_worker_fence(self) -> None:
        context = make_operation()
        command = await PostgresCommandStore(self.database).accept_once(_new_command(), context)
        self.assertIsInstance(command, Success)
        store = PostgresLearnerStore(self.database)
        rejected = await store.project(_learner_event(context), 0, context)
        self.assertIsInstance(rejected, Failure)
        self.assertEqual(
            rejected.error.code if isinstance(rejected, Failure) else "",
            "INVARIANT_VIOLATION",
        )
        snapshot = await store.get_snapshot(context.actor.actor_id, context)
        self.assertIsInstance(snapshot, Failure)
        self.assertEqual(
            snapshot.error.code if isinstance(snapshot, Failure) else "",
            "NOT_FOUND",
        )

    async def test_registry_rejection_is_idempotent_but_never_silently_overwrites(self) -> None:
        context = make_operation()
        evidence, certified = await self._seed_skill_build(context)
        reason = ContractError(
            code="SKILL_NOT_CERTIFIED",
            category=ErrorCategory.SKILL,
            retryable=False,
            user_message_key="skill.not_certified",
            stage="REGISTRY",
            message="Hidden certification test failed",
        )
        store = PostgresRegistryStore(self.database)
        self.assertEqual(
            await store.reject_certification(evidence, reason, context),
            Success(None),
        )
        self.assertEqual(
            await store.reject_certification(evidence, reason, context),
            Success(None),
        )
        changed = await store.reject_certification(
            evidence,
            replace(reason, message="Different rejection payload"),
            context,
        )
        self.assertIsInstance(changed, Failure)
        self.assertEqual(
            changed.error.code if isinstance(changed, Failure) else "",
            "IDEMPOTENCY_KEY_REUSED",
        )

        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute("TRUNCATE yaya_registry_certifications")
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
        finally:
            await connection.close()
        accepted_conflict = await store.reject_certification(evidence, reason, context)
        self.assertIsInstance(accepted_conflict, Failure)
        self.assertEqual(
            accepted_conflict.error.code if isinstance(accepted_conflict, Failure) else "",
            "INVARIANT_VIOLATION",
        )

    async def test_outbox_idempotency_lease_and_terminal_fencing(self) -> None:
        context = make_operation()
        store = PostgresOutboxStore(self.database)
        message = _outbox(context)
        self.assertEqual(await store.enqueue(message, context), Success(message))
        self.assertEqual(await store.enqueue(message, context), Success(message))

        conflicting_payload = replace(
            message.payload,
            delivery_id="delivery_store_test_0002",
            body=FeishuReportDraftBody("report_store_test_0002"),
        )
        conflicting = replace(
            message,
            message_id=conflicting_payload.delivery_id,
            payload=conflicting_payload,
        )
        conflict = await store.enqueue(conflicting, context)
        self.assertIsInstance(conflict, Failure)
        self.assertEqual(
            conflict.error.code if isinstance(conflict, Failure) else "", "IDEMPOTENCY_KEY_REUSED"
        )

        claimed = await store.claim_ready("worker_store_0001", 10, 30, context)
        self.assertIsInstance(claimed, Success)
        claimed_message = claimed.value[0] if isinstance(claimed, Success) else message
        self.assertEqual(claimed_message.status, OutboxStatus.SENDING)
        self.assertEqual(claimed_message.attempt, 1)
        second_claim = await store.claim_ready("worker_store_0002", 10, 30, context)
        self.assertEqual(second_claim, Success(()))

        receipt = DeliveryReceipt(
            delivery_id=claimed_message.message_id,
            operation=claimed_message.destination,
            deduplication_key=claimed_message.idempotency_key,
            report_id=claimed_message.payload.body.report_id,
            remote_object_id="remote_store_test_0001",
            sent_at=NOW + timedelta(seconds=1),
            attempt=claimed_message.attempt,
        )
        sent = await store.mark_sent(
            claimed_message.message_id,
            claimed_message.lease_id or "",
            receipt,
            context,
        )
        self.assertIsInstance(sent, Success)
        self.assertEqual(
            sent.value.status if isinstance(sent, Success) else None, OutboxStatus.SENT
        )

        error = ContractError(
            code="DEPENDENCY_UNAVAILABLE",
            category=ErrorCategory.DEPENDENCY,
            retryable=True,
            user_message_key="dependency.temporarily_unavailable",
            stage="COMPLETE",
        )
        stale = await store.mark_retry(
            claimed_message.message_id,
            claimed_message.lease_id or "",
            error,
            NOW + timedelta(minutes=1),
            context,
        )
        self.assertIsInstance(stale, Failure)
        self.assertEqual(
            stale.error.code if isinstance(stale, Failure) else "", "EVENT_SEQUENCE_GAP"
        )

    async def test_outbox_expired_lease_takeover_fences_previous_worker(self) -> None:
        context = make_operation()
        store = PostgresOutboxStore(self.database)
        message = _outbox(context)
        self.assertEqual(await store.enqueue(message, context), Success(message))
        first = await store.claim_ready("worker_store_expired_0001", 1, 1, context)
        self.assertIsInstance(first, Success)
        self.assertEqual(len(first.value) if isinstance(first, Success) else 0, 1)
        stale = first.value[0] if isinstance(first, Success) else message
        await asyncio.sleep(1.1)
        second = await PostgresOutboxStore(self.database).claim_ready(
            "worker_store_takeover_0002",
            1,
            30,
            context,
        )
        self.assertIsInstance(second, Success)
        self.assertEqual(len(second.value) if isinstance(second, Success) else 0, 1)
        current = second.value[0] if isinstance(second, Success) else message
        self.assertEqual(current.attempt, 2)
        self.assertNotEqual(current.lease_id, stale.lease_id)

        stale_receipt = DeliveryReceipt(
            delivery_id=message.message_id,
            operation=message.destination,
            deduplication_key=message.idempotency_key,
            report_id=message.payload.body.report_id,
            remote_object_id="remote_store_expired_0001",
            sent_at=NOW + timedelta(seconds=1),
            attempt=1,
        )
        stale_finish = await store.mark_sent(
            message.message_id,
            stale.lease_id or "",
            stale_receipt,
            context,
        )
        self.assertIsInstance(stale_finish, Failure)
        self.assertEqual(
            stale_finish.error.code if isinstance(stale_finish, Failure) else "",
            "EVENT_SEQUENCE_GAP",
        )

        current_receipt = replace(
            stale_receipt,
            remote_object_id="remote_store_takeover_0002",
            sent_at=NOW + timedelta(seconds=2),
            attempt=2,
        )
        finished = await store.mark_sent(
            message.message_id,
            current.lease_id or "",
            current_receipt,
            context,
        )
        self.assertIsInstance(finished, Success)
        self.assertEqual(
            finished.value.status if isinstance(finished, Success) else None,
            OutboxStatus.SENT,
        )


if __name__ == "__main__":
    unittest.main()
