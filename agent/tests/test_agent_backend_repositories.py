from __future__ import annotations

import asyncio
import sys
import unittest
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import LiteralString, cast

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
    TURN_ID,
    WORLD_ID,
    make_agent_decision,
    make_event,
    make_operation,
    make_session,
    make_skill,
    make_task,
    make_teaching_directive,
    make_versions,
    make_world_state,
)
from postgres_test_support import postgres_test_server  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from yaya_agent_backend.codec import (  # noqa: E402
    decode_as,
    encode,
    internal_record_sha256,
)
from yaya_agent_backend.database import (  # noqa: E402
    PostgresCommitStateUnknown,
    PostgresDatabase,
)
from yaya_agent_backend.product_semantics import (  # noqa: E402
    validate_interaction_semantics,
)
from yaya_agent_backend.repositories import (  # noqa: E402
    AgentTurnFenceError,
    AgentTurnLeaseConflict,
    PostgresAgentTurnRepository,
    PostgresLearnerRepository,
    PostgresSkillRepository,
    PostgresTaskRepository,
    RepositoryAuthorityError,
)
from yaya_agent_contracts import (  # noqa: E402
    ActorRef,
    CommandRecord,
    CommandStatus,
    EvidenceRef,
    EvidenceType,
    LearnerModelSnapshot,
    NewCommand,
    OperationContext,
    RequestContext,
    RuntimeEvent,
    canonical_json_sha256,
)
from yaya_agent_runtime import (  # noqa: E402
    LEARNER_PROJECTION_POLICY_VERSION,
    AgentDecision,
    DecisionDraft,
    GameEvent,
    LearnerInference,
    LearnerProfileSnapshot,
    RoleRoute,
    RunResultSnapshot,
    TeachingPhase,
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


class _CommitResponseLossDatabase(PostgresDatabase):
    def __init__(self, dsn: str) -> None:
        super().__init__(dsn)
        self.lose_next_commit_response = True

    @asynccontextmanager
    async def transaction_with_commit_boundary(
        self,
    ) -> AsyncGenerator[psycopg.AsyncConnection[dict[str, object]]]:
        async with super().transaction_with_commit_boundary() as connection:
            yield connection
        if self.lose_next_commit_response:
            self.lose_next_commit_response = False
            raise PostgresCommitStateUnknown("simulated lost COMMIT response")


class AgentBackendRepositoryTests(unittest.IsolatedAsyncioTestCase):
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

    async def _reset_database(self) -> None:
        connection = await self.database.connect(autocommit=True)
        try:
            for table in (
                "yaya_events",
                "yaya_agent_interactions",
                "yaya_projection_outbox",
                "yaya_agent_messages",
                "yaya_agent_turns",
                "yaya_learner_projection_jobs",
                "yaya_learner_projection_job_evidence",
            ):
                await connection.execute(
                    f"DROP TRIGGER IF EXISTS yaya_test_fail_agent_turn_publish ON {table}"
                )
            await connection.execute("DROP FUNCTION IF EXISTS yaya_test_fail_agent_turn_publish()")
            await connection.execute(
                """
                TRUNCATE yaya_agent_turns,yaya_agent_interactions,
                  yaya_projection_outbox,yaya_agent_messages,yaya_events,
                  yaya_learner_projection_failures,
                  yaya_learner_projection_receipts,
                  yaya_learner_projection_job_evidence,
                  yaya_learner_projection_jobs,yaya_learner_models,
                  yaya_evidence,yaya_runs,yaya_commands,yaya_registry_active,
                  yaya_registry_certifications,yaya_skills,yaya_agent_sessions,
                  yaya_worlds,yaya_tasks CASCADE
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

    async def _seed_turn_command(self, context: OperationContext) -> CommandRecord:
        command = NewCommand(
            command_type="EXECUTE_AGENT_TURN",
            idempotency_key="agent-turn:test:00000001",
            request_sha256="9" * 64,
            versions=make_versions(),
        )
        record = command.initial_record(context, context.requested_at)
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                INSERT INTO yaya_commands(
                  tenant_id,actor_id,operation,idempotency_key,command_id,
                  session_id,turn_id,client_turn_sequence,request_sha256,content_hash,
                  revision,status,updated_at,record_json
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    context.actor.actor_id,
                    command.operation,
                    command.idempotency_key,
                    context.command_id,
                    SESSION_ID,
                    TURN_ID,
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
        return record

    async def _seed_failed_run(
        self,
        context: OperationContext,
        event: GameEvent,
    ) -> None:
        if event.run_id is None or event.skill_ref is None or len(event.evidence_refs) != 1:
            self.fail("failed-run fixture is not identity-complete")
        evidence = event.evidence_refs[0]
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
            failed_actions=({"reason": "watering_loop_short"},),
            failure_key=event.failure_key,
            evidence_refs=(evidence,),
            world_commit=None,
            request_context=_request_context(context),
        )
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                INSERT INTO yaya_evidence(
                  tenant_id,evidence_id,actor_id,content_hash,evidence_type,
                  payload_sha256,evidence_json,recorded_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    evidence.evidence_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    evidence.evidence_type.value,
                    evidence.sha256,
                    Jsonb(
                        {
                            "integrity": {"payload_sha256": evidence.sha256},
                            "payload": {"failure_key": event.failure_key},
                        }
                    ),
                    NOW,
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
                    context.actor.tenant_id,
                    event.run_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    event.session_id,
                    event.turn_id,
                    event.command_id,
                    WORLD_ID,
                    event.skill_ref.skill_version_id,
                    event.failure_key,
                    Jsonb(encode(run)),
                    Jsonb({}),
                    NOW,
                ),
            )
        finally:
            await connection.close()

    @staticmethod
    def _inference_decision(event: GameEvent) -> AgentDecision:
        evidence = event.evidence_refs[0]
        directive = replace(
            make_teaching_directive(),
            phase=TeachingPhase.RECTIFICATION,
            target_concept="for_loop",
            allowed_response_types=("question",),
            required_evidence_ids=(evidence.evidence_id,),
            reason_codes=(
                "FAILED_EVIDENCE_RECTIFICATION",
                "PATCH_DISABLED_RUNTIME_STAGE",
                "FULL_SOLUTION_DISABLED",
            ),
        )
        return AgentDecision(
            draft=DecisionDraft(
                role="teaching_agent",
                response_type="question",
                message="The failed run is durable and needs one bounded correction.",
                question="Which loop boundary leaves the final plot dry?",
                hint_level=None,
                learner_inference=LearnerInference(
                    concept="for_loop",
                    score_delta=-0.1,
                    confidence=0.75,
                    reason="The exact failed test evidence shows one loop-boundary error.",
                    evidence_ids=(evidence.evidence_id,),
                ),
                skill_patch=None,
                requires_student_confirmation=False,
            ),
            message_key="agent.teaching_agent.question",
            source="provider",
            degraded=False,
            fallback_reason=None,
            provider="fixture-provider",
            model="fixture-model",
            input_tokens=11,
            output_tokens=7,
            tool_calls=(),
            evidence_refs=(evidence,),
            completed_at=event.occurred_at,
            teaching_directive=directive,
        )

    async def test_learner_profile_read_requires_current_projection_provenance(self) -> None:
        context = make_operation()
        repository = PostgresLearnerRepository(self.database)
        missing = await repository.get_profile(
            context.actor.actor_id,
            ("for_loop",),
            context,
        )
        self.assertEqual((missing.revision, missing.competencies), (0, {}))

        legacy = LearnerProfileSnapshot(
            student_id=context.actor.actor_id,
            revision=1,
            competencies={},
            request_context=_request_context(context),
            evidence_refs=(),
        )
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO yaya_learner_models(
                    tenant_id,learner_id,actor_id,content_hash,revision,
                    projected_through_sequence,snapshot_json
                ) VALUES (%s,%s,%s,%s,1,1,%s)
                """,
                (
                    context.actor.tenant_id,
                    context.actor.actor_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    Jsonb(encode(legacy)),
                ),
            )
        with self.assertRaises(RepositoryAuthorityError):
            await repository.get_profile(
                context.actor.actor_id,
                ("for_loop",),
                context,
            )

        cases = (
            ("learner_projection_unknown", LEARNER_PROJECTION_POLICY_VERSION, 1),
            (
                LEARNER_PROJECTION_POLICY_VERSION,
                "learner_projection_unknown",
                1,
            ),
            (LEARNER_PROJECTION_POLICY_VERSION, LEARNER_PROJECTION_POLICY_VERSION, 2),
        )
        for row_policy, snapshot_policy, row_revision in cases:
            with self.subTest(
                row_policy=row_policy,
                snapshot_policy=snapshot_policy,
                row_revision=row_revision,
            ):
                snapshot = LearnerModelSnapshot(
                    learner_id=context.actor.actor_id,
                    revision=1,
                    model_version=snapshot_policy,
                    projected_through_sequence=1,
                    competencies={},
                    updated_at=NOW,
                    evidence_refs=(),
                )

                async def update_model() -> None:
                    async with self.database.transaction() as connection:
                        await connection.execute(
                            """
                            UPDATE yaya_learner_models
                            SET revision=%s,projected_through_sequence=1,
                                snapshot_json=%s,request_context_json=%s,
                                projection_policy_version=%s,snapshot_sha256=%s,
                                updated_at=%s
                            WHERE tenant_id=%s AND learner_id=%s
                            """,
                            (
                                row_revision,
                                Jsonb(encode(snapshot)),
                                Jsonb(encode(context)),
                                row_policy,
                                internal_record_sha256(snapshot),
                                snapshot.updated_at,
                                context.actor.tenant_id,
                                context.actor.actor_id,
                            ),
                        )

                if row_revision != snapshot.projected_through_sequence:
                    with self.assertRaises(psycopg.errors.CheckViolation):
                        await update_model()
                    continue
                await update_model()
                with self.assertRaises(RepositoryAuthorityError):
                    await repository.get_profile(
                        context.actor.actor_id,
                        ("for_loop",),
                        context,
                    )

    async def test_learner_profile_fails_closed_on_snapshot_tamper_and_open_evidence(
        self,
    ) -> None:
        context = make_operation()
        repository = PostgresLearnerRepository(self.database)
        evidence = EvidenceRef(
            evidence_id="evidence_profile_integrity_00000001",
            evidence_type=EvidenceType.TEST_REPORT,
            created_at=NOW,
            sha256="d" * 64,
        )
        snapshot = LearnerModelSnapshot(
            learner_id=context.actor.actor_id,
            revision=1,
            model_version=LEARNER_PROJECTION_POLICY_VERSION,
            projected_through_sequence=1,
            competencies={
                "for_loop": {
                    "concept": "for_loop",
                    "evidence_stage": "OBSERVED",
                    "assistance_level": 1,
                    "last_observed_at": NOW.isoformat().replace("+00:00", "Z"),
                    "next_review_at": (NOW + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
                    "evidence_ids": [evidence.evidence_id],
                }
            },
            updated_at=NOW,
            evidence_refs=(evidence,),
        )
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO yaya_learner_models(
                    tenant_id,learner_id,actor_id,content_hash,revision,
                    projected_through_sequence,snapshot_json,snapshot_sha256,
                    request_context_json,projection_policy_version,updated_at
                ) VALUES (%s,%s,%s,%s,1,1,%s,%s,%s,%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    context.actor.actor_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    Jsonb(encode(snapshot)),
                    internal_record_sha256(snapshot),
                    Jsonb(encode(context)),
                    LEARNER_PROJECTION_POLICY_VERSION,
                    snapshot.updated_at,
                ),
            )
        profile = await repository.get_profile(
            context.actor.actor_id,
            ("for_loop",),
            context,
        )
        self.assertEqual(profile.revision, 1)
        self.assertEqual(profile.evidence_refs, (evidence,))

        decodable_tamper = replace(
            snapshot,
            evidence_refs=(
                evidence,
                replace(evidence, evidence_id="evidence_profile_integrity_00000002"),
            ),
        )
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                UPDATE yaya_learner_models SET snapshot_json=%s
                WHERE tenant_id=%s AND learner_id=%s
                """,
                (
                    Jsonb(encode(decodable_tamper)),
                    context.actor.tenant_id,
                    context.actor.actor_id,
                ),
            )
        with self.assertRaises(RepositoryAuthorityError):
            await repository.get_profile(
                context.actor.actor_id,
                ("for_loop",),
                context,
            )

        open_evidence = replace(snapshot, evidence_refs=())
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                UPDATE yaya_learner_models
                SET snapshot_json=%s,snapshot_sha256=%s,updated_at=%s
                WHERE tenant_id=%s AND learner_id=%s
                """,
                (
                    Jsonb(encode(open_evidence)),
                    internal_record_sha256(open_evidence),
                    open_evidence.updated_at,
                    context.actor.tenant_id,
                    context.actor.actor_id,
                ),
            )
        with self.assertRaises(RepositoryAuthorityError):
            await repository.get_profile(
                context.actor.actor_id,
                ("for_loop",),
                context,
            )

        async with self.database.transaction() as connection:
            await connection.execute(
                """
                UPDATE yaya_learner_models
                SET snapshot_json=%s,snapshot_sha256=%s,updated_at=%s
                WHERE tenant_id=%s AND learner_id=%s
                """,
                (
                    Jsonb(encode(snapshot)),
                    internal_record_sha256(snapshot),
                    snapshot.updated_at + timedelta(seconds=1),
                    context.actor.tenant_id,
                    context.actor.actor_id,
                ),
            )
        with self.assertRaises(RepositoryAuthorityError):
            await repository.get_profile(
                context.actor.actor_id,
                ("for_loop",),
                context,
            )

    async def test_role_drift_read_and_agent_turn_replay_are_authorized(self) -> None:
        original = make_operation()
        await self._seed_authority(original)
        await self._seed_turn_command(original)
        changed_actor = ActorRef(
            tenant_id=original.actor.tenant_id,
            actor_id=original.actor.actor_id,
            actor_type=original.actor.actor_type,
            roles=("game:player", "learner:read"),
        )
        changed = OperationContext(
            request_id=original.request_id,
            correlation_id=original.correlation_id,
            trace_id=original.trace_id,
            requested_at=original.requested_at,
            actor=changed_actor,
            content_ref=original.content_ref,
            command_id=original.command_id,
            causation_id=None,
            deadline_at=original.deadline_at,
        )
        task = await PostgresTaskRepository(self.database).get_task(TASK_ID, changed)
        self.assertEqual(task.request_context.actor.roles, ("game:player",))

        turns = PostgresAgentTurnRepository(self.database, CONTRACTS_ROOT)
        event = make_event("task_started")
        claim = await turns.claim(event, original)
        self.assertIsNotNone(claim.claim_id)
        receipt = await turns.commit(
            event,
            RoleRoute("task_started", "world_agent", "handled"),
            make_agent_decision(),
            claim.claim_id or "",
            original,
        )
        self.assertTrue(receipt.created)
        discarded_response_replay = await turns.commit(
            event,
            RoleRoute("task_started", "world_agent", "handled"),
            make_agent_decision(),
            "discarded_response_has_no_live_claim",
            original,
        )
        self.assertFalse(discarded_response_replay.created)
        self.assertEqual(discarded_response_replay.record, receipt.record)
        replay = await turns.get_committed(event, changed)
        self.assertEqual(replay, receipt.record)
        self.assertEqual(replay.actor.roles if replay else (), ("game:player",))

        connection = await self.database.connect(autocommit=True)
        try:
            counts = await connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM yaya_agent_interactions) AS interactions,
                  (SELECT count(*) FROM yaya_events) AS events,
                  (SELECT count(*) FROM yaya_projection_outbox) AS outbox,
                  (SELECT count(*) FROM yaya_agent_messages) AS messages
                """
            )
            row = await counts.fetchone()
        finally:
            await connection.close()
        if row is None:
            self.fail("atomic Agent turn count query returned no row")
        self.assertEqual(
            (row["interactions"], row["events"], row["outbox"], row["messages"]),
            (1, 1, 2, 1),
        )

    async def test_product_commit_time_covers_database_clock_lag(self) -> None:
        context = make_operation()
        await self._seed_authority(context)
        await self._seed_turn_command(context)
        connection = await self.database.connect(autocommit=True)
        try:
            clock_cursor = await connection.execute("SELECT clock_timestamp() AS value")
            clock_row = await clock_cursor.fetchone()
        finally:
            await connection.close()
        if clock_row is None:
            self.fail("database clock query returned no row")

        decision = replace(
            make_agent_decision(),
            completed_at=cast(datetime, clock_row["value"]) + timedelta(seconds=5),
        )
        event = make_event("task_started")
        turns = PostgresAgentTurnRepository(self.database, CONTRACTS_ROOT)
        claim = await turns.claim(event, context)
        receipt = await turns.commit(
            event,
            RoleRoute("task_started", "world_agent", "handled"),
            decision,
            claim.claim_id or "",
            context,
        )
        self.assertTrue(receipt.created)

        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT interaction_id,projection_json,created_at
                FROM yaya_agent_interactions
                WHERE tenant_id=%s AND session_id=%s
                """,
                (context.actor.tenant_id, event.session_id),
            )
            interaction_row = await cursor.fetchone()
        finally:
            await connection.close()
        if interaction_row is None:
            self.fail("clock-skew AgentInteraction was not persisted")
        interaction = cast(dict[str, object], interaction_row["projection_json"])
        created_at = cast(datetime, interaction_row["created_at"])
        self.assertTrue(created_at >= decision.completed_at)
        validate_interaction_semantics(
            interaction,
            authenticated_actor=context.actor,
            expected_session_id=event.session_id,
            expected_interaction_id=cast(str, interaction_row["interaction_id"]),
        )

    async def test_agent_turn_atomically_enqueues_hash_bound_learner_inference(self) -> None:
        context = make_operation()
        await self._seed_authority(context)
        await self._seed_turn_command(context)
        base_event = make_event("run_failed")
        evidence = EvidenceRef(
            base_event.evidence_refs[0].evidence_id,
            base_event.evidence_refs[0].evidence_type,
            base_event.evidence_refs[0].created_at,
            sha256=canonical_json_sha256(
                {"failure_key": base_event.failure_key or "watering_loop_short"}
            ),
        )
        event = replace(
            base_event,
            event_id="evt_run_failed_00000001",
            evidence_refs=(evidence,),
        )
        await self._seed_failed_run(context, event)
        decision = self._inference_decision(event)
        route = RoleRoute("run_failed", "teaching_agent", "handled")
        turns = PostgresAgentTurnRepository(self.database, CONTRACTS_ROOT)
        claim = await turns.claim(event, context)
        created = await turns.commit(event, route, decision, claim.claim_id or "", context)
        self.assertTrue(created.created)

        replay = await turns.commit(
            event,
            route,
            decision,
            "lost_response_replay_has_no_live_fence",
            context,
        )
        self.assertFalse(replay.created)
        self.assertEqual(replay.record, created.record)

        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM yaya_events) AS events,
                  (SELECT count(*) FROM yaya_agent_interactions) AS interactions,
                  (SELECT count(*) FROM yaya_projection_outbox) AS outbox,
                  (SELECT count(*) FROM yaya_agent_messages) AS messages,
                  (SELECT count(*) FROM yaya_learner_projection_jobs) AS jobs,
                  (SELECT count(*) FROM yaya_learner_projection_job_evidence) AS job_evidence,
                  (SELECT count(*) FROM yaya_agent_turns WHERE record_json IS NOT NULL)
                    AS committed
                """
            )
            counts = await cursor.fetchone()
            job_cursor = await connection.execute(
                """
                SELECT event_json,source_event_id,source_stream_id,
                       source_stream_sequence,state
                FROM yaya_learner_projection_jobs
                """
            )
            job = await job_cursor.fetchone()
            destinations_cursor = await connection.execute(
                """
                SELECT destination FROM yaya_projection_outbox ORDER BY destination
                """
            )
            destinations = tuple(row["destination"] for row in await destinations_cursor.fetchall())
        finally:
            await connection.close()
        if counts is None or job is None:
            self.fail("learner inference atomic surfaces were not persisted")
        self.assertEqual(
            (
                counts["events"],
                counts["interactions"],
                counts["outbox"],
                counts["messages"],
                counts["jobs"],
                counts["job_evidence"],
                counts["committed"],
            ),
            (2, 1, 3, 1, 1, 1, 1),
        )
        inference_event = decode_as(job["event_json"], RuntimeEvent)
        self.assertEqual(inference_event.causation_id, event.event_id)
        self.assertEqual(inference_event.payload["source_event_id"], event.event_id)
        self.assertEqual(job["source_event_id"], event.event_id)
        self.assertEqual(job["source_stream_id"], f"learner:{context.actor.actor_id}")
        self.assertEqual(job["source_stream_sequence"], 1)
        self.assertEqual(job["state"], "READY")
        self.assertEqual(
            destinations,
            (
                "agent_feedback_events",
                "learner_projection_events",
                "product_agent_interactions",
            ),
        )

    async def test_learner_inference_rejects_noncanonical_source_event_without_writes(
        self,
    ) -> None:
        context = make_operation()
        await self._seed_authority(context)
        await self._seed_turn_command(context)
        event = make_event("run_failed")
        evidence = EvidenceRef(
            event.evidence_refs[0].evidence_id,
            event.evidence_refs[0].evidence_type,
            event.evidence_refs[0].created_at,
            sha256=canonical_json_sha256(
                {"failure_key": event.failure_key or "watering_loop_short"}
            ),
        )
        event = replace(event, evidence_refs=(evidence,))
        await self._seed_failed_run(context, event)
        decision = self._inference_decision(event)
        turns = PostgresAgentTurnRepository(self.database, CONTRACTS_ROOT)
        claim = await turns.claim(event, context)

        with self.assertRaisesRegex(
            RepositoryAuthorityError,
            "canonical event identifier",
        ):
            await turns.commit(
                event,
                RoleRoute("run_failed", "teaching_agent", "handled"),
                decision,
                claim.claim_id or "",
                context,
            )

        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM yaya_events) AS events,
                  (SELECT count(*) FROM yaya_agent_interactions) AS interactions,
                  (SELECT count(*) FROM yaya_projection_outbox) AS outbox,
                  (SELECT count(*) FROM yaya_agent_messages) AS messages,
                  (SELECT count(*) FROM yaya_learner_projection_jobs) AS jobs,
                  (SELECT count(*) FROM yaya_agent_turns WHERE record_json IS NOT NULL)
                    AS committed
                """
            )
            counts = await cursor.fetchone()
        finally:
            await connection.close()
        if counts is None:
            self.fail("learner inference rejection count query returned no row")
        self.assertEqual(
            (
                counts["events"],
                counts["interactions"],
                counts["outbox"],
                counts["messages"],
                counts["jobs"],
                counts["committed"],
            ),
            (0, 0, 0, 0, 0, 0),
        )

    async def test_learner_inference_write_faults_roll_back_the_entire_agent_turn(self) -> None:
        fault_points = (
            ("yaya_events", "NEW.event_type = 'learner.inference.recorded'"),
            ("yaya_learner_projection_jobs", None),
            ("yaya_learner_projection_job_evidence", None),
            (
                "yaya_projection_outbox",
                "NEW.destination = 'learner_projection_events'",
            ),
            ("yaya_agent_turns", "NEW.record_json IS NOT NULL"),
        )
        for table, predicate in fault_points:
            with self.subTest(write_point=table):
                await self._reset_database()
                context = make_operation()
                await self._seed_authority(context)
                await self._seed_turn_command(context)
                base_event = make_event("run_failed")
                evidence = EvidenceRef(
                    base_event.evidence_refs[0].evidence_id,
                    base_event.evidence_refs[0].evidence_type,
                    base_event.evidence_refs[0].created_at,
                    sha256=canonical_json_sha256(
                        {"failure_key": base_event.failure_key or "watering_loop_short"}
                    ),
                )
                event = replace(
                    base_event,
                    event_id="evt_run_failed_00000001",
                    evidence_refs=(evidence,),
                )
                await self._seed_failed_run(context, event)
                route = RoleRoute("run_failed", "teaching_agent", "handled")
                decision = self._inference_decision(event)
                turns = PostgresAgentTurnRepository(self.database, CONTRACTS_ROOT)
                claim = await turns.claim(event, context)
                claim_id = claim.claim_id or ""

                connection = await self.database.connect(autocommit=True)
                try:
                    await connection.execute(
                        """
                        CREATE FUNCTION yaya_test_fail_agent_turn_publish() RETURNS trigger
                        LANGUAGE plpgsql AS $$
                        BEGIN
                            RAISE EXCEPTION 'injected learner inference publish failure'
                                USING ERRCODE = '40001';
                        END
                        $$
                        """
                    )
                    when_clause = f" WHEN ({predicate})" if predicate is not None else ""
                    await connection.execute(
                        cast(
                            LiteralString,
                            f"""
                            CREATE TRIGGER yaya_test_fail_agent_turn_publish
                            BEFORE INSERT OR UPDATE ON {table}
                            FOR EACH ROW{when_clause}
                            EXECUTE FUNCTION yaya_test_fail_agent_turn_publish()
                            """,
                        )
                    )
                finally:
                    await connection.close()

                with self.assertRaises(psycopg.Error):
                    await turns.commit(event, route, decision, claim_id, context)

                connection = await self.database.connect(autocommit=True)
                try:
                    cursor = await connection.execute(
                        """
                        SELECT
                          (SELECT count(*) FROM yaya_events) AS events,
                          (SELECT count(*) FROM yaya_agent_interactions) AS interactions,
                          (SELECT count(*) FROM yaya_projection_outbox) AS outbox,
                          (SELECT count(*) FROM yaya_agent_messages) AS messages,
                          (SELECT count(*) FROM yaya_learner_projection_jobs) AS jobs,
                          (SELECT count(*) FROM yaya_learner_projection_job_evidence)
                            AS job_evidence,
                          (SELECT count(*) FROM yaya_agent_turns
                           WHERE record_json IS NOT NULL) AS committed,
                          (SELECT claim_id FROM yaya_agent_turns
                           WHERE tenant_id=%s AND event_id=%s) AS claim_id
                        """,
                        (context.actor.tenant_id, event.event_id),
                    )
                    row = await cursor.fetchone()
                    await connection.execute(
                        cast(
                            LiteralString,
                            f"DROP TRIGGER yaya_test_fail_agent_turn_publish ON {table}",
                        )
                    )
                    await connection.execute("DROP FUNCTION yaya_test_fail_agent_turn_publish()")
                finally:
                    await connection.close()
                if row is None:
                    self.fail("learner inference rollback count query returned no row")
                self.assertEqual(
                    (
                        row["events"],
                        row["interactions"],
                        row["outbox"],
                        row["messages"],
                        row["jobs"],
                        row["job_evidence"],
                        row["committed"],
                    ),
                    (0, 0, 0, 0, 0, 0, 0),
                )
                self.assertEqual(row["claim_id"], claim_id)
                self.assertIsNone(await turns.get_committed(event, context))

    async def test_lost_commit_response_reconciles_inference_without_duplication(self) -> None:
        context = make_operation()
        await self._seed_authority(context)
        await self._seed_turn_command(context)
        base_event = make_event("run_failed")
        evidence = EvidenceRef(
            base_event.evidence_refs[0].evidence_id,
            base_event.evidence_refs[0].evidence_type,
            base_event.evidence_refs[0].created_at,
            sha256=canonical_json_sha256(
                {"failure_key": base_event.failure_key or "watering_loop_short"}
            ),
        )
        event = replace(
            base_event,
            event_id="evt_run_failed_00000001",
            evidence_refs=(evidence,),
        )
        await self._seed_failed_run(context, event)
        decision = self._inference_decision(event)
        route = RoleRoute("run_failed", "teaching_agent", "handled")
        lossy_database = _CommitResponseLossDatabase(self.server.dsn)
        turns = PostgresAgentTurnRepository(lossy_database, CONTRACTS_ROOT)
        claim = await turns.claim(event, context)

        reconciled = await turns.commit(
            event,
            route,
            decision,
            claim.claim_id or "",
            context,
        )
        self.assertFalse(reconciled.created)
        self.assertEqual(reconciled.record.decision, decision)
        replay = await turns.commit(event, route, decision, "no_live_claim", context)
        self.assertFalse(replay.created)
        self.assertEqual(replay.record, reconciled.record)

        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM yaya_events) AS events,
                  (SELECT count(*) FROM yaya_projection_outbox) AS outbox,
                  (SELECT count(*) FROM yaya_learner_projection_jobs) AS jobs,
                  (SELECT count(*) FROM yaya_learner_projection_job_evidence) AS evidence,
                  (SELECT count(*) FROM yaya_agent_messages) AS messages,
                  (SELECT count(*) FROM yaya_agent_interactions) AS interactions
                """
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            self.fail("commit-response-loss reconciliation returned no durable counts")
        self.assertEqual(
            (
                row["events"],
                row["outbox"],
                row["jobs"],
                row["evidence"],
                row["messages"],
                row["interactions"],
            ),
            (2, 3, 1, 1, 1, 1),
        )

    async def test_terminal_command_cannot_publish_agent_turn(self) -> None:
        context = make_operation()
        await self._seed_authority(context)
        command = await self._seed_turn_command(context)
        turns = PostgresAgentTurnRepository(self.database, CONTRACTS_ROOT)
        event = make_event("task_started")
        claim = await turns.claim(event, context)
        terminal = replace(
            command,
            status=CommandStatus.CANCELLED,
            terminal=True,
            revision=command.revision + 1,
            updated_at=command.updated_at + timedelta(milliseconds=1),
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
                    terminal.revision,
                    terminal.status.value,
                    terminal.updated_at,
                    Jsonb(encode(terminal)),
                    context.actor.tenant_id,
                    context.command_id,
                ),
            )
        finally:
            await connection.close()
        with self.assertRaisesRegex(RepositoryAuthorityError, "Command record identity drifted"):
            await turns.commit(
                event,
                RoleRoute("task_started", "world_agent", "handled"),
                make_agent_decision(),
                claim.claim_id or "",
                context,
            )

    async def test_agent_turn_publish_faults_roll_back_every_projection_as_one_unit(self) -> None:
        fault_points = (
            ("yaya_events", "INSERT", None),
            ("yaya_agent_interactions", "INSERT", None),
            (
                "yaya_projection_outbox",
                "INSERT",
                "NEW.destination = 'agent_feedback_events'",
            ),
            (
                "yaya_projection_outbox",
                "INSERT",
                "NEW.destination = 'product_agent_interactions'",
            ),
            ("yaya_agent_messages", "INSERT", None),
            ("yaya_agent_turns", "UPDATE", "NEW.record_json IS NOT NULL"),
        )
        for table, operation, predicate in fault_points:
            with self.subTest(write_point=f"{table}:{operation}:{predicate}"):
                await self._reset_database()
                context = make_operation()
                await self._seed_authority(context)
                await self._seed_turn_command(context)
                turns = PostgresAgentTurnRepository(self.database, CONTRACTS_ROOT)
                event = make_event("task_started")
                route = RoleRoute("task_started", "world_agent", "handled")
                decision = make_agent_decision()
                claim = await turns.claim(event, context)
                claim_id = claim.claim_id or ""

                connection = await self.database.connect(autocommit=True)
                try:
                    await connection.execute(
                        """
                        CREATE FUNCTION yaya_test_fail_agent_turn_publish() RETURNS trigger
                        LANGUAGE plpgsql AS $$
                        BEGIN
                            RAISE EXCEPTION 'injected AgentTurn publish failure'
                                USING ERRCODE = '40001';
                        END
                        $$
                        """
                    )
                    when_clause = f" WHEN ({predicate})" if predicate is not None else ""
                    await connection.execute(
                        cast(
                            LiteralString,
                            f"""
                        CREATE TRIGGER yaya_test_fail_agent_turn_publish
                        BEFORE {operation} ON {table}
                        FOR EACH ROW{when_clause}
                        EXECUTE FUNCTION yaya_test_fail_agent_turn_publish()
                        """,
                        )
                    )
                finally:
                    await connection.close()

                with self.assertRaises(psycopg.Error):
                    await turns.commit(event, route, decision, claim_id, context)

                connection = await self.database.connect(autocommit=True)
                try:
                    cursor = await connection.execute(
                        """
                        SELECT
                          (SELECT count(*) FROM yaya_events) AS events,
                          (SELECT count(*) FROM yaya_agent_interactions) AS interactions,
                          (SELECT count(*) FROM yaya_projection_outbox) AS outbox,
                          (SELECT count(*) FROM yaya_agent_messages) AS messages,
                          (SELECT count(*) FROM yaya_agent_turns
                           WHERE record_json IS NOT NULL) AS committed,
                          (SELECT claim_id FROM yaya_agent_turns
                           WHERE tenant_id=%s AND event_id=%s) AS claim_id
                        """,
                        (context.actor.tenant_id, event.event_id),
                    )
                    row = await cursor.fetchone()
                    await connection.execute(
                        cast(
                            LiteralString,
                            f"DROP TRIGGER yaya_test_fail_agent_turn_publish ON {table}",
                        )
                    )
                    await connection.execute("DROP FUNCTION yaya_test_fail_agent_turn_publish()")
                finally:
                    await connection.close()
                if row is None:
                    self.fail("fault rollback count query returned no row")
                self.assertEqual(
                    (
                        row["events"],
                        row["interactions"],
                        row["outbox"],
                        row["messages"],
                        row["committed"],
                    ),
                    (0, 0, 0, 0, 0),
                )
                self.assertEqual(row["claim_id"], claim_id)
                self.assertIsNone(await turns.get_committed(event, context))

                recovered = await turns.commit(event, route, decision, claim_id, context)
                self.assertTrue(recovered.created)
                replay = await turns.commit(
                    event,
                    route,
                    decision,
                    "response_was_lost_after_commit",
                    context,
                )
                self.assertFalse(replay.created)
                self.assertEqual(replay.record, recovered.record)

    async def test_active_boolean_without_registry_binding_is_not_exposed(self) -> None:
        context = make_operation()
        await self._seed_authority(context)
        skills = await PostgresSkillRepository(self.database).list_active_skills(
            context.actor.actor_id, context
        )
        self.assertEqual(skills, ())

    async def test_expired_claim_cannot_be_abandoned(self) -> None:
        context = make_operation()
        repository = PostgresAgentTurnRepository(
            self.database,
            CONTRACTS_ROOT,
            claim_ttl_ms=10,
        )
        event = make_event("task_started")
        claim = await repository.claim(event, context)
        await asyncio.sleep(0.05)
        with self.assertRaises(AgentTurnFenceError):
            await repository.abandon(event, claim.claim_id or "", context)
        with self.assertRaises(AgentTurnFenceError):
            await repository.renew(event, claim.claim_id or "", 100, context)
        with self.assertRaises(AgentTurnFenceError):
            await repository.commit(
                event,
                RoleRoute("task_started", "world_agent", "handled"),
                make_agent_decision(),
                claim.claim_id or "",
                context,
            )

    async def test_agent_turn_claim_shape_takeover_and_old_token_are_fenced(self) -> None:
        context = make_operation()
        event = make_event("task_started")
        connection = await self.database.connect(autocommit=True)
        try:
            with self.assertRaises(psycopg.errors.CheckViolation):
                await connection.execute(
                    """
                    INSERT INTO yaya_agent_turns(
                      tenant_id,event_id,actor_id,content_hash,event_sha256,
                      claim_id,claim_expires_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,NULL)
                    """,
                    (
                        context.actor.tenant_id,
                        "event_half_lease_0001",
                        context.actor.actor_id,
                        context.content_ref.content_hash,
                        "a" * 64,
                        "claim_half_lease_0001",
                    ),
                )
        finally:
            await connection.close()

        first_repository = PostgresAgentTurnRepository(
            self.database,
            CONTRACTS_ROOT,
            claim_ttl_ms=500,
        )
        first = await first_repository.claim(event, context)
        self.assertIsNotNone(first.claim_id)
        with self.assertRaises(AgentTurnLeaseConflict):
            await first_repository.claim(event, context)
        await asyncio.sleep(0.6)
        second_repository = PostgresAgentTurnRepository(
            self.database,
            CONTRACTS_ROOT,
            claim_ttl_ms=2_000,
        )
        second = await second_repository.claim(event, context)
        self.assertIsNotNone(second.claim_id)
        self.assertNotEqual(second.claim_id, first.claim_id)

        stale_id = first.claim_id or ""
        with self.assertRaises(AgentTurnFenceError):
            await first_repository.renew(event, stale_id, 100, context)
        with self.assertRaises(AgentTurnFenceError):
            await first_repository.abandon(event, stale_id, context)
        with self.assertRaises(AgentTurnFenceError):
            await first_repository.commit(
                event,
                RoleRoute("task_started", "world_agent", "handled"),
                make_agent_decision(),
                stale_id,
                context,
            )

        current_id = second.claim_id or ""
        renewed = await second_repository.renew(event, current_id, 2_000, context)
        self.assertEqual(renewed.claim_id, current_id)
        await second_repository.abandon(event, current_id, context)


if __name__ == "__main__":
    unittest.main()
