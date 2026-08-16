from __future__ import annotations

import asyncio
import json
import sys
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

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
from psycopg.types.json import Jsonb  # noqa: E402
from yaya_agent_backend.application import (  # noqa: E402
    AgentTurnApplication,
    AgentTurnWorker,
    HttpAttempt,
    WorkerLease,
    _validate_final_role_for_terminalization,
)
from yaya_agent_backend.codec import decode_as, encode, plain  # noqa: E402
from yaya_agent_backend.database import PostgresDatabase  # noqa: E402
from yaya_agent_backend.outcome_authority import PostgresRunOutcomeAuthority  # noqa: E402
from yaya_agent_backend.product_repositories import (  # noqa: E402
    PostgresProductInteractionReadRepository,
)
from yaya_agent_backend.repositories import PostgresAgentTurnRepository  # noqa: E402
from yaya_agent_backend.wire import ContractSchemaValidator  # noqa: E402
from yaya_agent_backend.world_uow import world_commit_identifier  # noqa: E402
from yaya_agent_contracts import (  # noqa: E402
    ActiveSkill,
    BuildArtifact,
    CertifiedSkill,
    CommandRecord,
    CommandStatus,
    EvidenceRef,
    EvidenceType,
    OperationContext,
    RequestContext,
    RuntimeEvent,
    RuntimeEventType,
    WorldCommitReceipt,
    canonical_json_sha256,
)
from yaya_agent_runtime import (  # noqa: E402
    CommittedAgentTurn,
    GameEvent,
    RoleRoute,
    RoleRouter,
    RunResultSnapshot,
    TeachingPhase,
    world_commit_receipt_sha256,
)
from yaya_agent_runtime.errors import AgentPersistenceError  # noqa: E402
from yaya_agent_runtime.hub import AgentHub, AgentHubResult  # noqa: E402


def _request_context(context: OperationContext) -> RequestContext:
    return RequestContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        trace_id=context.trace_id,
        requested_at=context.requested_at,
        actor=context.actor,
        content_ref=context.content_ref,
    )


def _body() -> dict[str, object]:
    skill = make_skill().ref
    return {
        "turn_id": "turn_worker_fault_0001",
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
            "client_turn_sequence": 1,
        },
    }


def _raw(body: dict[str, object]) -> bytes:
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


class _DurableReplayHub:
    """Tiny transport-loss harness around the real durable turn repository."""

    def __init__(
        self,
        repository: PostgresAgentTurnRepository,
        *,
        drop_first_commit_response: bool,
    ) -> None:
        self._repository = repository
        self._drop_first_commit_response = drop_first_commit_response
        self.handle_calls = 0
        self.provider_calls = 0

    async def handle(
        self,
        event: GameEvent,
        context: OperationContext,
    ) -> AgentHubResult:
        self.handle_calls += 1
        claim = await self._repository.claim(event, context)
        if claim.record is not None:
            return AgentHubResult(
                claim.record.route,
                claim.record.decision,
                True,
                True,
            )

        self.provider_calls += 1
        route = RoleRouter().route(event)
        base = make_agent_decision("Durably committed before the worker response.")
        decision = replace(
            base,
            draft=replace(base.draft, role=cast(str, route.role)),
            message_key="agent.xiaohutao.message",
            source="provider_fallback",
            degraded=True,
            fallback_reason="PROVIDER_TIMEOUT",
            completed_at=event.occurred_at + timedelta(milliseconds=1),
            teaching_directive=None,
        )
        receipt = await self._repository.commit(
            event,
            route,
            decision,
            claim.claim_id or "",
            context,
        )
        if self._drop_first_commit_response:
            self._drop_first_commit_response = False
            raise ConnectionResetError("simulated response loss after durable AgentTurn commit")
        return AgentHubResult(
            receipt.record.route,
            receipt.record.decision,
            True,
            not receipt.created,
        )


class _RollbackOnceHub:
    def __init__(self, delegate: _DurableReplayHub) -> None:
        self.delegate = delegate
        self.handle_calls = 0

    async def handle(
        self,
        event: GameEvent,
        context: OperationContext,
    ) -> AgentHubResult:
        self.handle_calls += 1
        if self.handle_calls == 1:
            raise AgentPersistenceError(
                "SIDE_EFFECT_ROLLED_BACK",
                "The invocation transaction rolled back before any side effect committed",
            )
        return await self.delegate.handle(event, context)


class _DecisionCommitHub:
    """Commit one supplied decision through the real AgentTurn repository."""

    def __init__(self, repository: PostgresAgentTurnRepository, decision: Any) -> None:
        self._repository = repository
        self._decision = decision

    async def handle(
        self,
        event: GameEvent,
        context: OperationContext,
    ) -> AgentHubResult:
        claim = await self._repository.claim(event, context)
        if claim.record is not None:
            return AgentHubResult(
                claim.record.route,
                claim.record.decision,
                True,
                True,
            )
        route = RoleRouter().route(event)
        receipt = await self._repository.commit(
            event,
            route,
            self._decision,
            claim.claim_id or "",
            context,
        )
        return AgentHubResult(
            receipt.record.route,
            receipt.record.decision,
            True,
            not receipt.created,
        )


class _ForbiddenHubDependency:
    """Records any dependency access after a durable replay lookup should fail."""

    def __init__(self) -> None:
        self.accesses: list[str] = []

    def __getattr__(self, name: str) -> Any:
        self.accesses.append(name)
        raise AssertionError(f"authority collision unexpectedly reached Hub dependency {name}")


class AgentBackendWorkerFaultTests(unittest.IsolatedAsyncioTestCase):
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
                TRUNCATE yaya_agent_turns,yaya_agent_interactions,
                  yaya_projection_outbox,yaya_agent_messages,yaya_events,
                  yaya_command_jobs,yaya_runs,yaya_commands,
                  yaya_registry_active,yaya_registry_certifications,
                  yaya_skills,yaya_agent_sessions,yaya_worlds,yaya_tasks CASCADE
                """
            )
        finally:
            await connection.close()
        self.origin = make_operation()
        await self._seed_authority(self.origin)
        application = AgentTurnApplication(
            self.database,
            CONTRACTS_ROOT,
            make_versions(),
        )
        body = _body()
        self.accepted = await application.accept(
            actor=self.origin.actor,
            attempt=HttpAttempt(
                request_id="req_worker_fault_0001",
                trace_id="trace_worker_fault_0001",
                correlation_id="corr_worker_fault_0001",
                requested_at=NOW,
            ),
            session_id=SESSION_ID,
            idempotency_key="agent-turn:worker-fault:0001",
            raw_body=_raw(body),
            body=body,
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
            artifact_uri="file:///worker-fault-test/skill",
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

    def _worker(
        self,
        worker_id: str,
        hub: Any,
    ) -> AgentTurnWorker:
        return AgentTurnWorker(
            database=self.database,
            hub=cast(AgentHub, hub),
            validator=ContractSchemaValidator(CONTRACTS_ROOT),
            worker_id=worker_id,
            configured_lease_seconds=2,
            poll_ms=10,
            runtime_budget_ms=1_000,
        )

    async def _expire_job(self) -> None:
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                UPDATE yaya_command_jobs
                SET lease_expires_at=clock_timestamp()-interval '1 second'
                WHERE tenant_id=%s AND command_id=%s
                """,
                (self.origin.actor.tenant_id, self.accepted.command.command_id),
            )
        finally:
            await connection.close()

    async def _job_command_row(self) -> dict[str, object]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT j.state,j.attempt,j.worker_id,j.lease_id,j.lease_expires_at,
                       j.last_error_code,c.status,c.revision,c.record_json
                FROM yaya_command_jobs j
                JOIN yaya_commands c
                  ON c.tenant_id=j.tenant_id AND c.command_id=j.command_id
                WHERE j.tenant_id=%s AND j.command_id=%s
                """,
                (self.origin.actor.tenant_id, self.accepted.command.command_id),
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            self.fail("accepted command job disappeared")
        return row

    async def _non_job_authority_snapshot(self) -> dict[str, list[object]]:
        """Capture every durable table except the Command/Job being terminalized."""

        table_names = (
            "yaya_tasks",
            "yaya_worlds",
            "yaya_agent_sessions",
            "yaya_skills",
            "yaya_compile_results",
            "yaya_evidence",
            "yaya_runs",
            "yaya_skill_invocations",
            "yaya_counterexamples",
            "yaya_learner_models",
            "yaya_agent_messages",
            "yaya_agent_turns",
            "yaya_agent_interactions",
            "yaya_projection_outbox",
            "yaya_agent_traces",
            "yaya_events",
            "yaya_outbox",
            "yaya_audit",
            "yaya_registry_certifications",
            "yaya_registry_active",
        )
        snapshot: dict[str, list[object]] = {}
        connection = await self.database.connect(autocommit=True)
        try:
            for table_name in table_names:
                cursor = await connection.execute(
                    f"SELECT to_jsonb(t) AS value FROM {table_name} AS t ORDER BY to_jsonb(t)::text"
                )
                snapshot[table_name] = [row["value"] for row in await cursor.fetchall()]
        finally:
            await connection.close()
        return snapshot

    async def _assert_stale_mutations_are_fenced(
        self,
        worker: AgentTurnWorker,
        lease: WorkerLease,
    ) -> None:
        for operation in (
            lambda: worker._advance_to_runtime(  # pyright: ignore[reportPrivateUsage]
                lease
            ),
            lambda: worker._mark_done(lease),  # pyright: ignore[reportPrivateUsage]
            lambda: worker._release_for_retry(  # pyright: ignore[reportPrivateUsage]
                lease, "STALE_OWNER"
            ),
        ):
            with self.assertRaises(AgentPersistenceError) as rejected:
                await operation()
            self.assertEqual(rejected.exception.code, "AGENT_JOB_FENCE_LOST")

    async def test_database_rejects_every_half_leased_job_state(self) -> None:
        connection = await self.database.connect(autocommit=True)
        try:
            with self.assertRaises(psycopg.errors.CheckViolation):
                await connection.execute(
                    """
                    UPDATE yaya_command_jobs
                    SET state='LEASED',worker_id='worker_half_lease_0001'
                    WHERE tenant_id=%s AND command_id=%s
                    """,
                    (self.origin.actor.tenant_id, self.accepted.command.command_id),
                )
            with self.assertRaises(psycopg.errors.CheckViolation):
                await connection.execute(
                    """
                    UPDATE yaya_command_jobs
                    SET worker_id='worker_half_lease_0001',lease_id='lease_half_lease_0001',
                        lease_expires_at=clock_timestamp()+interval '1 minute'
                    WHERE tenant_id=%s AND command_id=%s
                    """,
                    (self.origin.actor.tenant_id, self.accepted.command.command_id),
                )
        finally:
            await connection.close()
        row = await self._job_command_row()
        self.assertEqual(
            (row["state"], row["attempt"], row["worker_id"], row["lease_id"]),
            ("READY", 0, None, None),
        )

    async def test_concurrent_workers_claim_exactly_once(self) -> None:
        hub = _DurableReplayHub(
            PostgresAgentTurnRepository(self.database, CONTRACTS_ROOT),
            drop_first_commit_response=False,
        )
        first = self._worker("worker_concurrent_a_0001", hub)
        second = self._worker("worker_concurrent_b_0001", hub)
        claims = await asyncio.gather(first.claim_one(), second.claim_one())
        self.assertEqual(sum(item is not None for item in claims), 1)
        self.assertEqual(sum(item is None for item in claims), 1)
        row = await self._job_command_row()
        self.assertEqual((row["state"], row["attempt"]), ("LEASED", 1))
        self.assertIn(
            row["worker_id"],
            {"worker_concurrent_a_0001", "worker_concurrent_b_0001"},
        )
        self.assertEqual(hub.handle_calls, 0)

    async def test_same_session_claims_wait_for_prior_terminal_command_and_done_job(self) -> None:
        application = AgentTurnApplication(
            self.database,
            CONTRACTS_ROOT,
            make_versions(),
        )
        second_body = _body()
        second_body["turn_id"] = "turn_worker_fault_0002"
        second_body["client_state"] = {
            "last_event_sequence": 40,
            "client_turn_sequence": 2,
        }
        second_accepted = await application.accept(
            actor=self.origin.actor,
            attempt=HttpAttempt(
                request_id="req_worker_fault_0002",
                trace_id="trace_worker_fault_0002",
                correlation_id="corr_worker_fault_0002",
                requested_at=NOW,
            ),
            session_id=SESSION_ID,
            idempotency_key="agent-turn:worker-fault:0002",
            raw_body=_raw(second_body),
            body=second_body,
        )
        hub = _DurableReplayHub(
            PostgresAgentTurnRepository(self.database, CONTRACTS_ROOT),
            drop_first_commit_response=False,
        )
        first_worker = self._worker("worker_session_serial_a_0001", hub)
        second_worker = self._worker("worker_session_serial_b_0001", hub)
        claims = await asyncio.gather(first_worker.claim_one(), second_worker.claim_one())
        acquired = [item for item in claims if item is not None]
        self.assertEqual(len(acquired), 1)
        self.assertEqual(acquired[0].command_id, self.accepted.command.command_id)

        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                UPDATE yaya_command_jobs
                SET state='DONE',worker_id=NULL,lease_id=NULL,lease_expires_at=NULL
                WHERE tenant_id=%s AND command_id=%s
                """,
                (self.origin.actor.tenant_id, self.accepted.command.command_id),
            )
        finally:
            await connection.close()
        self.assertIsNone(await second_worker.claim_one())

        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                UPDATE yaya_commands SET status='APPLIED'
                WHERE tenant_id=%s AND command_id=%s
                """,
                (self.origin.actor.tenant_id, self.accepted.command.command_id),
            )
        finally:
            await connection.close()
        self.assertIsNone(await second_worker.claim_one())

        terminal_prior = replace(
            self.accepted.command,
            status=CommandStatus.APPLIED,
            stage="COMPLETE",
            terminal=True,
            result={
                "result_type": "NO_EFFECT",
                "reason_code": "MODEL_FALLBACK_NO_RUN",
            },
            error=None,
            revision=self.accepted.command.revision + 1,
            updated_at=self.accepted.command.updated_at + timedelta(milliseconds=1),
        )
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                UPDATE yaya_commands
                SET revision=%s,status=%s,updated_at=%s,record_json=%s
                WHERE tenant_id=%s AND command_id=%s
                """,
                (
                    terminal_prior.revision,
                    terminal_prior.status.value,
                    terminal_prior.updated_at,
                    Jsonb(encode(terminal_prior)),
                    self.origin.actor.tenant_id,
                    self.accepted.command.command_id,
                ),
            )
        finally:
            await connection.close()
        next_lease = await second_worker.claim_one()
        self.assertIsNotNone(next_lease)
        assert next_lease is not None
        self.assertEqual(next_lease.command_id, second_accepted.command.command_id)

    async def test_degraded_book_replay_has_stable_terminalization_invariant_code(self) -> None:
        event = make_event("task_completed")
        decision = make_agent_decision("Do not publish this degraded summary.")
        directive = decision.teaching_directive
        if directive is None:
            self.fail("degraded Book fixture has no TeachingDirective")
        decision = replace(
            decision,
            draft=replace(
                decision.draft,
                role="book_agent",
                response_type="growth_summary",
            ),
            message_key="agent.book_agent.growth_summary",
            source="provider_fallback",
            degraded=True,
            fallback_reason="PROVIDER_TIMEOUT",
            evidence_refs=event.evidence_refs,
            teaching_directive=replace(
                directive,
                phase=TeachingPhase.SUMMARIZATION,
                allowed_response_types=("growth_summary",),
                required_evidence_ids=tuple(item.evidence_id for item in event.evidence_refs),
            ),
        )
        record = CommittedAgentTurn(
            event=event,
            actor=self.origin.actor,
            content_ref=self.origin.content_ref,
            route=RoleRoute("task_completed", "book_agent", "handled"),
            decision=decision,
        )
        with self.assertRaises(AgentPersistenceError) as rejected:
            _validate_final_role_for_terminalization(record)
        self.assertEqual(
            rejected.exception.code,
            "AGENT_FINAL_ROLE_INVARIANT_VIOLATION",
        )

    async def test_successful_run_rejects_world_state_hash_outside_typed_receipt(self) -> None:
        state = make_world_state()
        durable_state_hash = canonical_json_sha256(state)
        receipt_state_hash = "f" * 64
        if receipt_state_hash == durable_state_hash:
            receipt_state_hash = "e" * 64
        receipt = WorldCommitReceipt(
            world_id=WORLD_ID,
            previous_revision=5,
            world_revision=6,
            first_event_sequence=41,
            last_event_sequence=41,
            committed_at=NOW,
            state_hash=receipt_state_hash,
        )
        evidence = EvidenceRef(
            evidence_id="evidence_world_hash_mismatch_0001",
            evidence_type=EvidenceType.WORLD_COMMIT,
            created_at=NOW,
            sha256=world_commit_receipt_sha256(receipt),
        )
        event = make_event("task_completed")
        run = RunResultSnapshot(
            run_id=event.run_id or "run_world_hash_mismatch_0001",
            session_id=event.session_id,
            turn_id=event.turn_id,
            command_id=event.command_id,
            world_id=WORLD_ID,
            skill_ref=make_skill().ref,
            task_success=True,
            world_revision_before=5,
            world_revision_after=6,
            world_difference={"watered_plots": 8, "total_plots": 8},
            failed_actions=(),
            failure_key=None,
            evidence_refs=(evidence,),
            world_commit=receipt,
            request_context=_request_context(self.origin),
        )

        class _Cursor:
            async def fetchall(self) -> list[dict[str, object]]:
                return [
                    {
                        "actor_id": self_origin.actor.actor_id,
                        "content_hash": self_origin.content_ref.content_hash,
                        "stream_id": f"world:{WORLD_ID}",
                        "revision": 6,
                        "last_event_sequence": 41,
                        "state_hash": durable_state_hash,
                        "state_json": state,
                        "request_context_json": encode(_request_context(self_origin)),
                    }
                ]

        class _Connection:
            async def execute(self, query: object, params: object) -> _Cursor:
                del query, params
                return _Cursor()

        self_origin = self.origin
        authority = PostgresRunOutcomeAuthority(
            cast(Any, None),
            ContractSchemaValidator(CONTRACTS_ROOT),
        )
        with self.assertRaises(AgentPersistenceError) as rejected:
            await authority._validate_world(  # pyright: ignore[reportPrivateUsage,reportArgumentType]
                cast(Any, _Connection()),
                run=run,
                root_event=event,
                context=self.origin,
            )
        self.assertEqual(
            rejected.exception.code,
            "AGENT_OUTCOME_INVARIANT_VIOLATION",
        )

    async def test_world_commit_plain_event_wire_accepts_canonical_and_rejects_corruption(
        self,
    ) -> None:
        state = make_world_state()
        state_hash = canonical_json_sha256(state)
        receipt = WorldCommitReceipt(
            world_id=WORLD_ID,
            previous_revision=5,
            world_revision=6,
            first_event_sequence=41,
            last_event_sequence=41,
            committed_at=NOW,
            state_hash=state_hash,
        )
        evidence = EvidenceRef(
            evidence_id="evidence_world_wire_0001",
            evidence_type=EvidenceType.WORLD_COMMIT,
            created_at=NOW,
            sha256=world_commit_receipt_sha256(receipt),
        )
        root_event = make_event("task_completed")
        run = RunResultSnapshot(
            run_id=root_event.run_id or "run_world_wire_0001",
            session_id=root_event.session_id,
            turn_id=root_event.turn_id,
            command_id=root_event.command_id,
            world_id=WORLD_ID,
            skill_ref=root_event.skill_ref or make_skill().ref,
            task_success=True,
            world_revision_before=5,
            world_revision_after=6,
            world_difference={"watered_plots": 8, "total_plots": 8},
            failed_actions=(),
            failure_key=None,
            evidence_refs=(evidence,),
            world_commit=receipt,
            request_context=_request_context(self.origin),
        )
        evidence_wire = {
            "evidence_id": evidence.evidence_id,
            "evidence_type": evidence.evidence_type.value,
            "created_at": plain(evidence.created_at),
            "sha256": evidence.sha256,
        }
        world_event = RuntimeEvent(
            event_id="evt_world_wire_test_0001",
            event_type=RuntimeEventType.WORLD_COMMITTED,
            event_version=1,
            stream_id=f"world:{WORLD_ID}",
            sequence=41,
            occurred_at=NOW,
            producer="world_uow",
            trace_id=self.origin.trace_id,
            command_id=root_event.command_id,
            correlation_id=self.origin.correlation_id,
            causation_id=root_event.command_id,
            content_ref=self.origin.content_ref,
            payload={
                "commit_id": world_commit_identifier(
                    self.origin.actor.tenant_id,
                    f"world:{WORLD_ID}",
                    run.run_id,
                    5,
                ),
                "run_id": run.run_id,
                "world_id": WORLD_ID,
                "previous_world_revision": 5,
                "world_revision": 6,
                "state_hash": state_hash,
                "applied_intent_ids": ("intent_world_wire_0001",),
                "committed_at": plain(NOW),
                "evidence_refs": (evidence_wire,),
            },
        )
        world_row = {
            "actor_id": self.origin.actor.actor_id,
            "content_hash": self.origin.content_ref.content_hash,
            "stream_id": f"world:{WORLD_ID}",
            "revision": 6,
            "last_event_sequence": 41,
            "state_hash": state_hash,
            "state_json": state,
            "request_context_json": encode(_request_context(self.origin)),
        }
        event_row = {
            "event_id": world_event.event_id,
            "stream_id": world_event.stream_id,
            "sequence": world_event.sequence,
            "event_type": world_event.event_type,
            "event_json": plain(world_event),
            "occurred_at": world_event.occurred_at,
        }

        class _Cursor:
            def __init__(self, rows: list[dict[str, object]]) -> None:
                self._rows = rows

            async def fetchall(self) -> list[dict[str, object]]:
                return self._rows

        class _Connection:
            def __init__(self, stored_event: dict[str, object]) -> None:
                self._stored_event = stored_event
                self._calls = 0

            async def execute(self, query: object, params: object) -> _Cursor:
                del query, params
                self._calls += 1
                return _Cursor([world_row] if self._calls == 1 else [self._stored_event])

        authority = PostgresRunOutcomeAuthority(
            cast(Any, None),
            ContractSchemaValidator(CONTRACTS_ROOT),
        )
        await authority._validate_world(  # pyright: ignore[reportPrivateUsage,reportArgumentType]
            cast(Any, _Connection(event_row)),
            run=run,
            root_event=root_event,
            context=self.origin,
        )

        corrupted_event_row = dict(event_row)
        corrupted_wire = cast(dict[str, object], plain(world_event))
        corrupted_wire["producer"] = "INVALID_PRODUCER"
        corrupted_event_row["event_json"] = corrupted_wire
        with self.assertRaises(ValueError):
            await authority._validate_world(  # pyright: ignore[reportPrivateUsage,reportArgumentType]
                cast(Any, _Connection(corrupted_event_row)),
                run=run,
                root_event=root_event,
                context=self.origin,
            )

    async def test_crashed_worker_takeover_fences_old_done_retry_and_transition(self) -> None:
        hub = _DurableReplayHub(
            PostgresAgentTurnRepository(self.database, CONTRACTS_ROOT),
            drop_first_commit_response=False,
        )
        crashed = self._worker("worker_crashed_0001", hub)
        restarted = self._worker("worker_restarted_0001", hub)
        old_lease = await crashed.claim_one()
        self.assertIsNotNone(old_lease)
        self.assertIsNone(await restarted.claim_one())
        if old_lease is None:
            self.fail("first worker did not acquire the durable job")

        await self._expire_job()
        await self._assert_stale_mutations_are_fenced(crashed, old_lease)
        before_takeover = await self._job_command_row()
        self.assertEqual(
            (before_takeover["state"], before_takeover["attempt"], before_takeover["status"]),
            ("LEASED", 1, "ACCEPTED"),
        )

        new_lease = await restarted.claim_one()
        self.assertIsNotNone(new_lease)
        if new_lease is None:
            self.fail("restart worker did not take over the expired job")
        self.assertNotEqual(new_lease.lease_id, old_lease.lease_id)
        await self._assert_stale_mutations_are_fenced(crashed, old_lease)

        await restarted._process(new_lease)  # pyright: ignore[reportPrivateUsage]
        completed = await self._job_command_row()
        self.assertEqual(
            (
                completed["state"],
                completed["attempt"],
                completed["worker_id"],
                completed["lease_id"],
                completed["status"],
                completed["revision"],
            ),
            ("DONE", 2, None, None, "APPLIED", 3),
        )
        command = decode_as(completed["record_json"], CommandRecord)
        self.assertEqual(
            dict(command.result or {}),
            {"result_type": "NO_EFFECT", "reason_code": "MODEL_FALLBACK_NO_RUN"},
        )
        self.assertEqual((hub.handle_calls, hub.provider_calls), (1, 1))
        self.assertFalse(await restarted.run_once())

    async def test_commit_response_loss_replays_after_restart_without_second_provider_call(
        self,
    ) -> None:
        hub = _DurableReplayHub(
            PostgresAgentTurnRepository(self.database, CONTRACTS_ROOT),
            drop_first_commit_response=True,
        )
        first_process = self._worker("worker_response_loss_0001", hub)
        restarted = self._worker("worker_response_replay_0001", hub)

        self.assertTrue(await first_process.run_once())
        after_loss = await self._job_command_row()
        self.assertEqual(
            (after_loss["state"], after_loss["attempt"], after_loss["status"]),
            ("READY", 1, "VALIDATING"),
        )
        self.assertEqual((hub.handle_calls, hub.provider_calls), (1, 1))

        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM yaya_agent_turns WHERE record_json IS NOT NULL) AS turns,
                  (SELECT count(*) FROM yaya_agent_interactions) AS interactions,
                  (SELECT count(*) FROM yaya_projection_outbox) AS outbox,
                  (SELECT count(*) FROM yaya_agent_messages) AS messages
                """
            )
            first_counts = await cursor.fetchone()
            await connection.execute(
                """
                UPDATE yaya_command_jobs SET available_at=clock_timestamp()
                WHERE tenant_id=%s AND command_id=%s
                """,
                (self.origin.actor.tenant_id, self.accepted.command.command_id),
            )
        finally:
            await connection.close()
        if first_counts is None:
            self.fail("projection count query returned no row")
        self.assertEqual(
            (
                first_counts["turns"],
                first_counts["interactions"],
                first_counts["outbox"],
                first_counts["messages"],
            ),
            (1, 1, 2, 1),
        )

        self.assertTrue(await restarted.run_once())
        completed = await self._job_command_row()
        self.assertEqual(
            (completed["state"], completed["attempt"], completed["status"], completed["revision"]),
            ("DONE", 2, "APPLIED", 3),
        )
        command = decode_as(completed["record_json"], CommandRecord)
        self.assertEqual(
            dict(command.result or {}),
            {"result_type": "NO_EFFECT", "reason_code": "MODEL_FALLBACK_NO_RUN"},
        )
        self.assertEqual((hub.handle_calls, hub.provider_calls), (2, 1))
        self.assertFalse(await restarted.run_once())

        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM yaya_agent_turns WHERE record_json IS NOT NULL) AS turns,
                  (SELECT count(*) FROM yaya_agent_interactions) AS interactions,
                  (SELECT count(*) FROM yaya_projection_outbox) AS outbox,
                  (SELECT count(*) FROM yaya_agent_messages) AS messages
                """
            )
            replay_counts = await cursor.fetchone()
        finally:
            await connection.close()
        if replay_counts is None:
            self.fail("replay projection count query returned no row")
        self.assertEqual(
            (
                replay_counts["turns"],
                replay_counts["interactions"],
                replay_counts["outbox"],
                replay_counts["messages"],
            ),
            (1, 1, 2, 1),
        )

    async def test_known_side_effect_rollback_is_retried_by_a_restarted_worker(self) -> None:
        durable = _DurableReplayHub(
            PostgresAgentTurnRepository(self.database, CONTRACTS_ROOT),
            drop_first_commit_response=False,
        )
        hub = _RollbackOnceHub(durable)
        first = self._worker("worker_rollback_first_0001", hub)
        restarted = self._worker("worker_rollback_retry_0001", hub)

        self.assertTrue(await first.run_once())
        rolled_back = await self._job_command_row()
        self.assertEqual(
            (rolled_back["state"], rolled_back["attempt"], rolled_back["status"]),
            ("READY", 1, "VALIDATING"),
        )
        self.assertEqual(rolled_back["worker_id"], None)
        self.assertEqual((hub.handle_calls, durable.handle_calls), (1, 0))

        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                UPDATE yaya_command_jobs SET available_at=clock_timestamp()
                WHERE tenant_id=%s AND command_id=%s
                """,
                (self.origin.actor.tenant_id, self.accepted.command.command_id),
            )
        finally:
            await connection.close()

        self.assertTrue(await restarted.run_once())
        completed = await self._job_command_row()
        self.assertEqual(
            (
                completed["state"],
                completed["attempt"],
                completed["worker_id"],
                completed["status"],
                completed["revision"],
            ),
            ("DONE", 2, None, "APPLIED", 3),
        )
        self.assertEqual(
            (hub.handle_calls, durable.handle_calls, durable.provider_calls), (2, 1, 1)
        )
        self.assertFalse(await restarted.run_once())

    async def test_worker_terminalizes_a_derived_run_free_turn_for_product_reads(
        self,
    ) -> None:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT event_json FROM yaya_command_jobs
                WHERE tenant_id=%s AND command_id=%s
                """,
                (self.origin.actor.tenant_id, self.accepted.command.command_id),
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            self.fail("accepted command has no durable source event")
        accepted_event = decode_as(row["event_json"], GameEvent)
        derived_event = replace(
            accepted_event,
            event_id="event_hint_requested_worker_derived_0001",
            event_type="hint_requested",
            occurred_at=accepted_event.occurred_at + timedelta(milliseconds=1),
            run_id=None,
            failure_count=0,
            failure_key=None,
            evidence_refs=(),
            payload={},
        )
        decision = make_agent_decision("Use one bounded hint for the next attempt.")
        directive = decision.teaching_directive
        if directive is None:
            self.fail("derived hint fixture has no TeachingDirective")
        decision = replace(
            decision,
            draft=replace(
                decision.draft,
                role="teaching_agent",
                response_type="hint",
                hint_level=1,
            ),
            message_key="agent.teaching_agent.hint",
            completed_at=derived_event.occurred_at + timedelta(milliseconds=1),
            teaching_directive=replace(
                directive,
                hint_level=1,
                allowed_response_types=("question", "hint"),
            ),
        )
        repository = PostgresAgentTurnRepository(self.database, CONTRACTS_ROOT)
        worker = self._worker(
            "worker_derived_hint_0001",
            _DecisionCommitHub(repository, decision),
        )
        lease = await worker.claim_one()
        self.assertIsNotNone(lease)
        assert lease is not None
        self.assertEqual(lease.event, accepted_event)
        result = await worker.process_claimed_event(lease, derived_event)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.persisted)
        self.assertFalse(result.replayed)

        completed = await self._job_command_row()
        self.assertEqual((completed["state"], completed["status"]), ("DONE", "APPLIED"))
        command = decode_as(completed["record_json"], CommandRecord)
        self.assertTrue(command.terminal)
        self.assertIsNotNone(command.result)
        assert command.result is not None
        self.assertEqual(command.result["result_type"], "NO_EFFECT")
        product = PostgresProductInteractionReadRepository(
            self.database,
            ContractSchemaValidator(CONTRACTS_ROOT),
        )
        page = await product.list_interactions(
            self.origin.actor,
            SESSION_ID,
            after_sequence=0,
            limit=50,
        )
        self.assertEqual(len(page.interactions), 1)
        interaction = page.interactions[0].interaction
        self.assertEqual(interaction["turn_id"], derived_event.turn_id)
        self.assertIsNone(cast(dict[str, object], interaction["feedback"])["run_id"])
        self.assertEqual(
            await product.get_interaction(
                self.origin.actor,
                SESSION_ID,
                cast(str, interaction["interaction_id"]),
            ),
            page.interactions[0],
        )

        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT event_json FROM yaya_command_jobs
                WHERE tenant_id=%s AND command_id=%s
                """,
                (self.origin.actor.tenant_id, self.accepted.command.command_id),
            )
            durable_job = await cursor.fetchone()
        finally:
            await connection.close()
        self.assertIsNotNone(durable_job)
        assert durable_job is not None
        self.assertEqual(decode_as(durable_job["event_json"], GameEvent), accepted_event)

    async def test_conflicting_agent_turn_authority_fails_job_once_without_ready_livelock(
        self,
    ) -> None:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT event_json FROM yaya_command_jobs
                WHERE tenant_id=%s AND command_id=%s
                """,
                (self.origin.actor.tenant_id, self.accepted.command.command_id),
            )
            row = await cursor.fetchone()
            if row is None:
                self.fail("accepted command has no durable Worker event")
            event = decode_as(row["event_json"], GameEvent)
            conflicting_actor = "actor_worker_authority_conflict_0001"
            conflicting_content = (
                "0" * 64 if self.origin.content_ref.content_hash != "0" * 64 else "1" * 64
            )
            actual_event_sha = canonical_json_sha256(cast(dict[str, object], encode(event)))
            conflicting_event_sha = "2" * 64 if actual_event_sha != "2" * 64 else "3" * 64
            self.assertNotEqual(conflicting_actor, self.origin.actor.actor_id)
            self.assertNotEqual(conflicting_content, self.origin.content_ref.content_hash)
            self.assertNotEqual(conflicting_event_sha, actual_event_sha)
            await connection.execute(
                """
                INSERT INTO yaya_agent_turns(
                  tenant_id,event_id,actor_id,content_hash,event_sha256
                ) VALUES (%s,%s,%s,%s,%s)
                """,
                (
                    self.origin.actor.tenant_id,
                    event.event_id,
                    conflicting_actor,
                    conflicting_content,
                    conflicting_event_sha,
                ),
            )
        finally:
            await connection.close()

        before_authority = await self._non_job_authority_snapshot()
        forbidden = _ForbiddenHubDependency()
        hub = AgentHub(
            router=RoleRouter(),
            contexts=cast(Any, forbidden),
            runtime=cast(Any, forbidden),
            turns=PostgresAgentTurnRepository(self.database, CONTRACTS_ROOT),
        )
        worker = self._worker("worker_authority_conflict_0001", hub)

        self.assertTrue(await worker.run_once())
        terminal_row = await self._job_command_row()
        self.assertEqual(
            (
                terminal_row["state"],
                terminal_row["attempt"],
                terminal_row["worker_id"],
                terminal_row["lease_id"],
                terminal_row["lease_expires_at"],
                terminal_row["last_error_code"],
                terminal_row["status"],
                terminal_row["revision"],
            ),
            ("DONE", 1, None, None, None, None, "FAILED", 3),
        )
        command = decode_as(terminal_row["record_json"], CommandRecord)
        self.assertTrue(command.terminal)
        self.assertIsNone(command.result)
        self.assertIsNotNone(command.error)
        if command.error is None:
            self.fail("permanent authority collision has no terminal Command error")
        self.assertEqual(command.error.code, "INVARIANT_VIOLATION")
        self.assertEqual(command.error.stage, "VALIDATE")
        self.assertFalse(command.error.retryable)
        self.assertEqual(
            dict(command.error.details),
            {"cause_code": "AGENT_TURN_LOOKUP_FAILED"},
        )
        self.assertEqual(forbidden.accesses, [])
        self.assertEqual(await self._non_job_authority_snapshot(), before_authority)

        self.assertFalse(await worker.run_once(), "DONE poison job became READY again")
        after_idle = await self._job_command_row()
        self.assertEqual(
            (
                after_idle["state"],
                after_idle["attempt"],
                after_idle["status"],
                after_idle["revision"],
            ),
            ("DONE", 1, "FAILED", 3),
        )
        self.assertEqual(await self._non_job_authority_snapshot(), before_authority)


if __name__ == "__main__":
    unittest.main()
