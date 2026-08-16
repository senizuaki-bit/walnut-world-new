from __future__ import annotations

import asyncio
import sys
import unittest
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
CONTRACTS_ROOT = Path(__file__).resolve().parents[1] / "contracts"
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from agent_runtime_fixtures import (  # noqa: E402
    NOW,
    SESSION_ID,
    TASK_ID,
    WORLD_ID,
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
from psycopg import sql  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from yaya_agent_backend.codec import (  # noqa: E402
    decode_as,
    encode,
    internal_record_sha256,
    plain,
)
from yaya_agent_backend.database import PostgresDatabase  # noqa: E402
from yaya_agent_backend.learner_projection import (  # noqa: E402
    LearnerProjectionFenceLost,
    LearnerProjectionLease,
    LearnerProjectionWorker,
)
from yaya_agent_backend.repositories import PostgresAgentTurnRepository  # noqa: E402
from yaya_agent_backend.stores import PostgresLearnerStore  # noqa: E402
from yaya_agent_contracts import (  # noqa: E402
    CommandRecord,
    ContractError,
    ErrorCategory,
    EvidenceRef,
    EvidenceType,
    Failure,
    LearnerModelSnapshot,
    LearnerUpdate,
    NewCommand,
    OperationContext,
    RequestContext,
    RuntimeEvent,
    Success,
    canonical_json_sha256,
)
from yaya_agent_runtime import (  # noqa: E402
    AgentDecision,
    CompileResultSnapshot,
    DecisionDraft,
    GameEvent,
    LearnerInference,
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


class PostgresLearnerStoreTests(unittest.IsolatedAsyncioTestCase):
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
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                TRUNCATE yaya_learner_projection_failures,
                  yaya_learner_projection_receipts,
                  yaya_learner_projection_job_evidence,
                  yaya_learner_projection_jobs,yaya_learner_models,
                  yaya_outbox,yaya_projection_outbox,yaya_agent_interactions,
                  yaya_agent_messages,yaya_agent_turns,yaya_events,
                  yaya_evidence,yaya_runs,yaya_compile_results,yaya_commands,
                  yaya_registry_active,
                  yaya_registry_certifications,yaya_skills,yaya_agent_sessions,
                  yaya_worlds,yaya_tasks CASCADE
                """
            )
        self.base_context = make_operation()
        await self._seed_authority(self.base_context)

    async def _seed_authority(self, context: OperationContext) -> None:
        task = make_task(context)
        session = make_session(operation=context)
        skill = make_skill(context)
        state = make_world_state()
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO yaya_tasks(
                    tenant_id,task_id,actor_id,content_hash,snapshot_json
                ) VALUES (%s,%s,%s,%s,%s)
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
                    tenant_id,session_id,actor_id,task_id,world_id,
                    content_hash,snapshot_json
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
                    tenant_id,skill_id,skill_version_id,certification_id,
                    actor_id,session_id,content_hash,artifact_sha256,
                    snapshot_json,active
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

    def _context(self, sequence: int) -> OperationContext:
        return replace(
            self.base_context,
            request_id=f"req_learner_store_{sequence:08d}",
            correlation_id=f"corr_learner_store_{sequence:08d}",
            trace_id=f"trace_learner_store_{sequence:08d}",
            command_id=f"cmd_learner_store_{sequence:08d}",
            requested_at=NOW + timedelta(seconds=sequence),
            deadline_at=NOW + timedelta(minutes=5, seconds=sequence),
        )

    async def _seed_command(
        self,
        context: OperationContext,
        turn_id: str,
        sequence: int,
        *,
        test_suite_version: str | None = None,
    ) -> CommandRecord:
        command = NewCommand(
            command_type="EXECUTE_AGENT_TURN",
            idempotency_key=f"learner-store:{sequence:08d}",
            request_sha256=canonical_json_sha256({"sequence": sequence}),
            versions=replace(
                make_versions(),
                test_suite_version=test_suite_version,
            ),
        )
        record = command.initial_record(context, context.requested_at)
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO yaya_commands(
                    tenant_id,actor_id,operation,idempotency_key,command_id,
                    session_id,turn_id,client_turn_sequence,request_sha256,
                    content_hash,revision,status,updated_at,record_json
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    context.actor.actor_id,
                    command.operation,
                    command.idempotency_key,
                    context.command_id,
                    SESSION_ID,
                    turn_id,
                    sequence,
                    command.request_sha256,
                    context.content_ref.content_hash,
                    record.revision,
                    record.status.value,
                    record.updated_at,
                    Jsonb(encode(record)),
                ),
            )
        return record

    async def _seed_failed_run(
        self,
        context: OperationContext,
        event: GameEvent,
        run_id: str,
        evidence_document: dict[str, object],
        recorded_at: datetime,
    ) -> None:
        if event.skill_ref is None or len(event.evidence_refs) != 1:
            self.fail("failed-run fixture is not identity-complete")
        evidence = event.evidence_refs[0]
        run = RunResultSnapshot(
            run_id=run_id,
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
        async with self.database.transaction() as connection:
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
                    Jsonb(evidence_document),
                    recorded_at,
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_runs(
                    tenant_id,run_id,actor_id,content_hash,session_id,turn_id,
                    command_id,world_id,skill_version_id,failure_key,
                    task_success,snapshot_json,wire_json,created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE,%s,%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    run_id,
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
                    recorded_at,
                ),
            )

    @staticmethod
    def _decision(event: GameEvent, learner_revision: int) -> AgentDecision:
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
            learner_revision=learner_revision,
        )
        return AgentDecision(
            draft=DecisionDraft(
                role="teaching_agent",
                response_type="question",
                message="The failed run needs one bounded correction.",
                question="Which loop boundary leaves the final plot dry?",
                hint_level=None,
                learner_inference=LearnerInference(
                    concept="for_loop",
                    score_delta=-0.1,
                    confidence=0.75,
                    reason="The immutable failed test shows a loop-boundary error.",
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

    async def _commit_inference(
        self,
        sequence: int,
        learner_revision: int,
        *,
        bind_event_run_id: bool = True,
    ) -> tuple[RuntimeEvent, OperationContext, dict[str, object]]:
        context = self._context(sequence)
        turn_id = f"turn_learner_store_{sequence:08d}"
        run_id = f"run_learner_store_{sequence:08d}"
        await self._seed_command(context, turn_id, sequence)
        evidence_payload: dict[str, object] = {
            "evidence_kind": "SKILL_RUN",
            "run_id": run_id,
            "sandbox_status": "SUCCEEDED",
            "world_status": "REJECTED",
            "intent_count": 8,
        }
        evidence_sha256 = canonical_json_sha256(evidence_payload)
        base_event = (
            make_event("run_failed")
            if bind_event_run_id
            else make_event("hint_requested", failure_count=1)
        )
        evidence = EvidenceRef(
            evidence_id=f"evidence_learner_store_{sequence:08d}",
            evidence_type=EvidenceType.SANDBOX_LOG,
            created_at=NOW + timedelta(seconds=sequence),
            sha256=evidence_sha256,
        )
        recorded_at = evidence.created_at + timedelta(milliseconds=250)
        event = replace(
            base_event,
            event_id=f"evt_learner_source_{sequence:08d}",
            turn_id=turn_id,
            command_id=context.command_id,
            run_id=run_id if bind_event_run_id else None,
            occurred_at=recorded_at,
            evidence_refs=(evidence,),
        )
        skill_ref = event.skill_ref
        if skill_ref is None:
            self.fail("failed-run fixture lost its certified Skill identity")
        evidence_document: dict[str, object] = {
            "request_context": plain(_request_context(context)),
            "evidence_ref": {
                "evidence_id": evidence.evidence_id,
                "evidence_type": evidence.evidence_type.value,
                "created_at": plain(evidence.created_at),
                "sha256": evidence.sha256,
            },
            "subject": {"learner_id": context.actor.actor_id},
            "source": {
                "source_type": "SKILL_RUN",
                "source_id": run_id,
                "command_id": context.command_id,
                "world_id": WORLD_ID,
            },
            "occurred_at": plain(evidence.created_at),
            "recorded_at": plain(recorded_at),
            "integrity": {
                "payload_sha256": evidence_sha256,
                "previous_evidence_sha256": None,
            },
            "payload": evidence_payload,
            "related_evidence": [],
            "versions": {
                key: value
                for key, value in cast(
                    dict[str, object],
                    plain(
                        replace(
                            make_versions(),
                            skill_version=skill_ref.skill_version_id,
                            artifact_sha256=skill_ref.artifact_sha256,
                        )
                    ),
                ).items()
                if value is not None
            },
        }
        self.assertNotEqual(
            canonical_json_sha256(evidence_document),
            evidence_sha256,
            "regression fixture must distinguish full Evidence from payload hashing",
        )
        await self._seed_failed_run(
            context,
            event,
            run_id,
            evidence_document,
            recorded_at,
        )
        turns = PostgresAgentTurnRepository(self.database, CONTRACTS_ROOT)
        claim = await turns.claim(event, context)
        await turns.commit(
            event,
            RoleRoute(event.event_type, "teaching_agent", "handled"),
            self._decision(event, learner_revision),
            claim.claim_id or "",
            context,
        )
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                SELECT e.event_json,j.operation_context_json
                FROM yaya_events e
                JOIN yaya_learner_projection_jobs j
                  ON j.tenant_id=e.tenant_id AND j.event_id=e.event_id
                WHERE e.tenant_id=%s AND e.stream_id=%s AND e.sequence=%s
                """,
                (
                    context.actor.tenant_id,
                    f"learner:{context.actor.actor_id}",
                    sequence,
                ),
            )
            row = await cursor.fetchone()
        if row is None:
            self.fail("Agent turn did not commit its learner inference Job")
        return (
            decode_as(row["event_json"], RuntimeEvent),
            decode_as(row["operation_context_json"], OperationContext),
            evidence_document,
        )

    async def _commit_compile_inference(
        self,
        sequence: int,
        learner_revision: int,
    ) -> tuple[RuntimeEvent, OperationContext]:
        context = self._context(sequence)
        turn_id = f"turn_learner_compile_{sequence:08d}"
        build_id = f"build_learner_compile_{sequence:08d}"
        command = await self._seed_command(
            context,
            turn_id,
            sequence,
            test_suite_version="watering-tests-1",
        )
        skill_ref = make_skill(context).ref
        test_suite_version = command.versions.test_suite_version
        if test_suite_version is None:
            self.fail("compile fixture requires one frozen test-suite version")
        evidence_payload: dict[str, object] = {
            "evidence_kind": "BUILD_CERTIFICATION",
            "build_id": build_id,
            "skill_id": skill_ref.skill_id,
            "skill_version_id": skill_ref.skill_version_id,
            "artifact_sha256": skill_ref.artifact_sha256,
            "test_suite_version": test_suite_version,
            "outcome": "REJECTED",
        }
        evidence = EvidenceRef(
            evidence_id=f"evidence_learner_compile_{sequence:08d}",
            evidence_type=EvidenceType.TEST_REPORT,
            created_at=NOW + timedelta(seconds=sequence),
            sha256=canonical_json_sha256(evidence_payload),
        )
        event = replace(
            make_event("compile_failed"),
            event_id=f"evt_learner_compile_source_{sequence:08d}",
            turn_id=turn_id,
            command_id=context.command_id,
            build_id=build_id,
            skill_ref=skill_ref,
            occurred_at=evidence.created_at,
            evidence_refs=(evidence,),
        )
        compile_result = CompileResultSnapshot(
            build_id=build_id,
            skill_ref=skill_ref,
            succeeded=False,
            diagnostics=("LOOP_BOUNDARY_REJECTED",),
            evidence_refs=(evidence,),
            request_context=_request_context(context),
        )
        evidence_document: dict[str, object] = {
            "request_context": plain(compile_result.request_context),
            "evidence_ref": {
                "evidence_id": evidence.evidence_id,
                "evidence_type": evidence.evidence_type.value,
                "created_at": plain(evidence.created_at),
                "sha256": evidence.sha256,
            },
            "subject": {"learner_id": context.actor.actor_id},
            "source": {
                "source_type": "SKILL_BUILD",
                "source_id": build_id,
                "command_id": None,
                "world_id": None,
            },
            "occurred_at": plain(evidence.created_at),
            "recorded_at": plain(evidence.created_at),
            "integrity": {
                "payload_sha256": evidence.sha256,
                "previous_evidence_sha256": None,
            },
            "payload": evidence_payload,
            "related_evidence": [],
            "versions": {
                key: value
                for key, value in cast(
                    dict[str, object],
                    plain(
                        replace(
                            command.versions,
                            skill_version=skill_ref.skill_version_id,
                            artifact_sha256=skill_ref.artifact_sha256,
                        )
                    ),
                ).items()
                if value is not None
            },
        }
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO yaya_compile_results(
                    tenant_id,build_id,actor_id,content_hash,snapshot_json
                ) VALUES (%s,%s,%s,%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    build_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    Jsonb(encode(compile_result)),
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
                    context.actor.tenant_id,
                    evidence.evidence_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    evidence.evidence_type.value,
                    evidence.sha256,
                    Jsonb(evidence_document),
                    evidence.created_at,
                ),
            )
        turns = PostgresAgentTurnRepository(self.database, CONTRACTS_ROOT)
        claim = await turns.claim(event, context)
        await turns.commit(
            event,
            RoleRoute(event.event_type, "teaching_agent", "handled"),
            self._decision(event, learner_revision),
            claim.claim_id or "",
            context,
        )
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                SELECT e.event_json,j.operation_context_json
                FROM yaya_events e
                JOIN yaya_learner_projection_jobs j
                  ON j.tenant_id=e.tenant_id AND j.event_id=e.event_id
                WHERE e.tenant_id=%s AND e.stream_id=%s AND e.sequence=%s
                """,
                (
                    context.actor.tenant_id,
                    f"learner:{context.actor.actor_id}",
                    sequence,
                ),
            )
            row = await cursor.fetchone()
        if row is None:
            self.fail("compile-failed Agent turn did not commit its inference Job")
        return (
            decode_as(row["event_json"], RuntimeEvent),
            decode_as(row["operation_context_json"], OperationContext),
        )

    def _worker(self, worker_id: str = "learner-store-worker") -> LearnerProjectionWorker:
        return LearnerProjectionWorker(
            database=self.database,
            learner=PostgresLearnerStore(self.database),
            worker_id=worker_id,
            lease_seconds=30,
            poll_ms=10,
            retry_delay_seconds=0.01,
        )

    async def _claim(self) -> LearnerProjectionLease:
        lease = await self._worker().claim_one()
        if lease is None:
            self.fail("expected one ready learner projection Job")
        return lease

    async def _make_job_available(self, job_id: str) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                UPDATE yaya_learner_projection_jobs
                SET available_at=clock_timestamp()
                WHERE tenant_id=%s AND job_id=%s
                """,
                (self.base_context.actor.tenant_id, job_id),
            )

    async def _counts(self) -> tuple[int, int, int, int, int]:
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM yaya_learner_models) AS models,
                  (SELECT count(*) FROM yaya_learner_projection_receipts) AS receipts,
                  (SELECT count(*) FROM yaya_events
                   WHERE event_type='learner.model.updated') AS updates,
                  (SELECT count(*) FROM yaya_outbox) AS outbox,
                  (SELECT count(*) FROM yaya_learner_projection_failures) AS failures
                """
            )
            row = await cursor.fetchone()
        if row is None:
            self.fail("PostgreSQL count query returned no row")
        return cast(
            tuple[int, int, int, int, int],
            tuple(row[key] for key in ("models", "receipts", "updates", "outbox", "failures")),
        )

    async def _learner_time_authority_snapshot(
        self,
        context: OperationContext,
    ) -> dict[str, object]:
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                SELECT
                  e.evidence_json->>'occurred_at' AS document_occurred_at,
                  e.evidence_json->>'recorded_at' AS document_recorded_at,
                  e.recorded_at AS row_recorded_at,
                  r.created_at AS run_created_at,
                  t.record_json #>>
                    '{$fields,event,$fields,occurred_at,$datetime}'
                    AS source_event_occurred_at
                FROM yaya_evidence e
                JOIN yaya_runs r
                  ON r.tenant_id=e.tenant_id
                 AND r.run_id=e.evidence_json #>> '{payload,run_id}'
                JOIN yaya_agent_turns t
                  ON t.tenant_id=e.tenant_id AND t.event_id=%s
                WHERE e.tenant_id=%s AND e.evidence_id=%s
                """,
                (
                    "evt_learner_source_00000001",
                    context.actor.tenant_id,
                    "evidence_learner_store_00000001",
                ),
            )
            row = await cursor.fetchone()
        if row is None:
            self.fail("learner time authority fixture is incomplete")
        fields = (
            "document_occurred_at",
            "document_recorded_at",
            "row_recorded_at",
            "run_created_at",
            "source_event_occurred_at",
        )
        return {field: row[field] for field in fields}

    async def _learner_projection_fingerprint(self) -> dict[str, tuple[int, str]]:
        tables = (
            "yaya_learner_models",
            "yaya_learner_projection_receipts",
            "yaya_learner_projection_jobs",
            "yaya_learner_projection_job_evidence",
            "yaya_learner_projection_failures",
            "yaya_learner_projection_terminal_audits",
            "yaya_events",
            "yaya_outbox",
        )
        async with self.database.transaction() as connection:
            result: dict[str, tuple[int, str]] = {}
            for table in tables:
                cursor = await connection.execute(
                    f"""
                    SELECT count(*)::int AS count,
                           md5(COALESCE(
                             string_agg(value::text,'' ORDER BY value::text),''
                           )) AS hash
                    FROM (SELECT to_jsonb(t) AS value FROM {table} t) rows
                    """
                )
                row = await cursor.fetchone()
                if row is None:
                    self.fail(f"learner projection fingerprint failed for {table}")
                result[table] = (cast(int, row["count"]), cast(str, row["hash"]))
        return result

    @staticmethod
    def _shift_time_wire(value: object) -> str:
        if not isinstance(value, str):
            raise AssertionError("learner time authority JSON timestamp is not a string")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise AssertionError("learner time authority JSON timestamp has no timezone")
        shifted = plain(parsed + timedelta(seconds=1))
        if not isinstance(shifted, str):
            raise AssertionError("shifted learner time authority is not JSON text")
        return shifted

    async def _assert_time_authority_mutation_fails_closed(self, target: str) -> None:
        _, context, _ = await self._commit_inference(1, 0)
        self.assertEqual(await self._counts(), (0, 0, 0, 0, 0))
        before = await self._learner_time_authority_snapshot(context)
        evidence_id = "evidence_learner_store_00000001"
        source_event_id = "evt_learner_source_00000001"

        async with self.database.transaction() as connection:
            if target in {"document_occurred_at", "document_recorded_at"}:
                document_field = {
                    "document_occurred_at": "occurred_at",
                    "document_recorded_at": "recorded_at",
                }[target]
                changed = await connection.execute(
                    """
                    UPDATE yaya_evidence SET evidence_json=jsonb_set(
                      evidence_json,%s::text[],%s::jsonb,false
                    ) WHERE tenant_id=%s AND evidence_id=%s
                    """,
                    (
                        [document_field],
                        Jsonb(self._shift_time_wire(before[target])),
                        context.actor.tenant_id,
                        evidence_id,
                    ),
                )
            elif target == "row_recorded_at":
                changed = await connection.execute(
                    """
                    UPDATE yaya_evidence
                    SET recorded_at=recorded_at+interval '1 second'
                    WHERE tenant_id=%s AND evidence_id=%s
                    """,
                    (context.actor.tenant_id, evidence_id),
                )
            elif target == "run_created_at":
                changed = await connection.execute(
                    """
                    UPDATE yaya_runs SET created_at=created_at+interval '1 second'
                    WHERE tenant_id=%s AND run_id=%s
                    """,
                    (context.actor.tenant_id, "run_learner_store_00000001"),
                )
            elif target == "source_event_occurred_at":
                changed = await connection.execute(
                    """
                    UPDATE yaya_agent_turns SET record_json=jsonb_set(
                      record_json,
                      ARRAY['$fields','event','$fields','occurred_at','$datetime'],
                      %s::jsonb,false
                    ) WHERE tenant_id=%s AND event_id=%s
                    """,
                    (
                        Jsonb(self._shift_time_wire(before[target])),
                        context.actor.tenant_id,
                        source_event_id,
                    ),
                )
            else:
                self.fail(f"unsupported learner time authority target: {target}")
        self.assertEqual(changed.rowcount, 1)

        after = await self._learner_time_authority_snapshot(context)
        self.assertEqual(
            {field for field in before if before[field] != after[field]},
            {target},
            "time tamper must change exactly one durable authority field",
        )

        worker = self._worker(f"learner-time-authority-{target.replace('_', '-')}")
        self.assertTrue(await worker.run_once())
        source_graph_corrupt = target == "source_event_occurred_at"
        # An intact source graph can safely emit a permanent-failure Event and
        # outbox.  A corrupt final source event must instead be quarantined
        # without deriving any Event from that untrusted source.  Neither path
        # may create a learner model, success receipt, or model-update Event.
        expected_counts = (0, 0, 0, 0, 1) if source_graph_corrupt else (0, 0, 0, 1, 1)
        expected_classification = "QUARANTINED" if source_graph_corrupt else "PERMANENT"
        self.assertEqual(await self._counts(), expected_counts)
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                SELECT j.state,j.last_error_code,f.classification
                FROM yaya_learner_projection_jobs j
                JOIN yaya_learner_projection_failures f
                  ON f.tenant_id=j.tenant_id AND f.job_id=j.job_id
                WHERE j.tenant_id=%s AND j.source_stream_sequence=1
                """,
                (context.actor.tenant_id,),
            )
            rows = list(await cursor.fetchall())
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (
                rows[0]["state"],
                rows[0]["last_error_code"],
                rows[0]["classification"],
            ),
            ("FAILED", "INVARIANT_VIOLATION", expected_classification),
        )

        before_rebuild = await self._learner_projection_fingerprint()
        rebuilt = await PostgresLearnerStore(self.database).rebuild(
            context.actor.actor_id,
            1,
            context,
        )
        self.assertIsInstance(rebuilt, Failure)
        self.assertEqual(cast(Failure, rebuilt).error.code, "INVARIANT_VIOLATION")
        self.assertEqual(await self._counts(), expected_counts)
        self.assertEqual(await self._learner_projection_fingerprint(), before_rebuild)

    async def _assert_evidence_source_mutation_fails_closed(
        self,
        path: str,
        replacement: str,
    ) -> None:
        _, context, _ = await self._commit_inference(1, 0)
        evidence_id = "evidence_learner_store_00000001"
        async with self.database.transaction() as connection:
            before_cursor = await connection.execute(
                """
                SELECT payload_sha256,evidence_json->'payload' AS payload
                FROM yaya_evidence
                WHERE tenant_id=%s AND evidence_id=%s
                """,
                (context.actor.tenant_id, evidence_id),
            )
            before = await before_cursor.fetchone()
            if before is None:
                self.fail("durable Evidence fixture was not persisted")
            changed = await connection.execute(
                """
                UPDATE yaya_evidence
                SET evidence_json=jsonb_set(
                    evidence_json,string_to_array(%s,'.'),to_jsonb(%s::text),false
                )
                WHERE tenant_id=%s AND evidence_id=%s
                """,
                (path, replacement, context.actor.tenant_id, evidence_id),
            )
            self.assertEqual(changed.rowcount, 1)
            after_cursor = await connection.execute(
                """
                SELECT payload_sha256,evidence_json->'payload' AS payload
                FROM yaya_evidence
                WHERE tenant_id=%s AND evidence_id=%s
                """,
                (context.actor.tenant_id, evidence_id),
            )
            after = await after_cursor.fetchone()
        if after is None:
            self.fail("mutated durable Evidence disappeared")
        self.assertEqual(
            (after["payload_sha256"], after["payload"]),
            (before["payload_sha256"], before["payload"]),
            "source mutation must leave the hash-covered payload unchanged",
        )

        self.assertTrue(await self._worker("learner-source-mutation").run_once())
        self.assertEqual(await self._counts(), (0, 0, 0, 1, 1))
        rebuild = await PostgresLearnerStore(self.database).rebuild(
            context.actor.actor_id,
            1,
            context,
        )
        self.assertIsInstance(rebuild, Failure)
        failure = cast(Failure, rebuild)
        self.assertEqual(failure.error.code, "INVARIANT_VIOLATION")
        self.assertIn("Evidence hash or authority drifted", failure.error.message or "")

    async def test_projection_and_rebuild_reject_evidence_source_id_drift(self) -> None:
        await self._assert_evidence_source_mutation_fails_closed(
            "source.source_id",
            "run_tampered_00000001",
        )

    async def test_projection_and_rebuild_reject_evidence_world_id_drift(self) -> None:
        await self._assert_evidence_source_mutation_fails_closed(
            "source.world_id",
            "world_tampered_00000001",
        )

    async def test_projection_requires_exact_source_event_run_binding(self) -> None:
        _, context, _ = await self._commit_inference(
            1,
            0,
            bind_event_run_id=False,
        )
        self.assertTrue(await self._worker("learner-run-binding").run_once())
        self.assertEqual(await self._counts(), (0, 0, 0, 1, 1))
        rebuild = await PostgresLearnerStore(self.database).rebuild(
            context.actor.actor_id,
            1,
            context,
        )
        self.assertIsInstance(rebuild, Failure)
        failure = cast(Failure, rebuild)
        self.assertEqual(failure.error.code, "INVARIANT_VIOLATION")
        self.assertIn("source turn identity", failure.error.message or "")

    async def test_projection_and_rebuild_close_run_to_durable_skill_artifact(self) -> None:
        _, context, _ = await self._commit_inference(1, 0)
        async with self.database.transaction() as connection:
            changed = await connection.execute(
                """
                UPDATE yaya_skills SET artifact_sha256=%s
                WHERE tenant_id=%s AND skill_version_id=%s
                """,
                (
                    "c" * 64,
                    context.actor.tenant_id,
                    make_skill(context).ref.skill_version_id,
                ),
            )
        self.assertEqual(changed.rowcount, 1)

        self.assertTrue(await self._worker("learner-skill-artifact-binding").run_once())
        self.assertEqual(await self._counts(), (0, 0, 0, 1, 1))
        rebuild = await PostgresLearnerStore(self.database).rebuild(
            context.actor.actor_id,
            1,
            context,
        )
        self.assertIsInstance(rebuild, Failure)
        failure = cast(Failure, rebuild)
        self.assertEqual(failure.error.code, "INVARIANT_VIOLATION")
        self.assertIn("Run or Skill identity drifted", failure.error.message or "")

    async def test_compile_failed_inference_projects_and_rebuilds_without_run(self) -> None:
        event, context = await self._commit_compile_inference(1, 0)
        self.assertIsNone(event.payload["run_id"])
        worker = self._worker("learner-compile-failed")
        self.assertTrue(await worker.run_once())
        self.assertFalse(await worker.run_once())

        store = PostgresLearnerStore(self.database)
        online_result = await store.get_snapshot(context.actor.actor_id, context)
        self.assertIsInstance(online_result, Success)
        online = cast(Success[LearnerModelSnapshot], online_result).value
        self.assertEqual((online.revision, online.projected_through_sequence), (1, 1))
        self.assertEqual(await self._counts(), (1, 1, 1, 1, 0))

        rebuilt_result = await store.rebuild(context.actor.actor_id, 1, context)
        self.assertEqual(rebuilt_result, Success(online))

    async def test_compile_projection_rejects_cross_build_evidence(self) -> None:
        _, context = await self._commit_compile_inference(1, 0)
        async with self.database.transaction() as connection:
            changed = await connection.execute(
                """
                UPDATE yaya_evidence
                SET evidence_json=jsonb_set(
                    evidence_json,'{source,source_id}',
                    to_jsonb('build_crossed_00000001'::text),false
                )
                WHERE tenant_id=%s AND evidence_id=%s
                """,
                (
                    context.actor.tenant_id,
                    "evidence_learner_compile_00000001",
                ),
            )
        self.assertEqual(changed.rowcount, 1)

        self.assertTrue(await self._worker("learner-compile-cross-build").run_once())
        self.assertEqual(await self._counts(), (0, 0, 0, 1, 1))
        rebuild = await PostgresLearnerStore(self.database).rebuild(
            context.actor.actor_id,
            1,
            context,
        )
        self.assertIsInstance(rebuild, Failure)
        self.assertEqual(cast(Failure, rebuild).error.code, "INVARIANT_VIOLATION")

    async def test_sandbox_evidence_occurrence_precedes_durable_recording(self) -> None:
        event, context, evidence_document = await self._commit_inference(1, 0)
        evidence_refs = cast(list[Mapping[str, object]], event.payload["evidence_refs"])
        self.assertEqual(len(evidence_refs), 1)
        self.assertEqual(
            evidence_document["occurred_at"],
            evidence_refs[0]["created_at"],
        )
        self.assertNotEqual(
            evidence_document["occurred_at"],
            evidence_document["recorded_at"],
        )
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                SELECT e.recorded_at,r.created_at
                FROM yaya_evidence e
                JOIN yaya_runs r
                  ON r.tenant_id=e.tenant_id
                 AND r.run_id=e.evidence_json #>> '{payload,run_id}'
                WHERE e.tenant_id=%s AND e.evidence_id=%s
                """,
                (
                    context.actor.tenant_id,
                    evidence_refs[0]["evidence_id"],
                ),
            )
            row = await cursor.fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["recorded_at"], row["created_at"])
        self.assertEqual(evidence_document["recorded_at"], plain(row["recorded_at"]))

        worker = self._worker("learner-distinct-evidence-times")
        self.assertTrue(await worker.run_once())
        self.assertEqual(await self._counts(), (1, 1, 1, 1, 0))
        store = PostgresLearnerStore(self.database)
        online = await store.get_snapshot(context.actor.actor_id, context)
        self.assertIsInstance(online, Success)
        rebuilt = await store.rebuild(context.actor.actor_id, 1, context)
        self.assertEqual(rebuilt, online)

    async def test_projection_rejects_evidence_document_occurred_at_drift(self) -> None:
        await self._assert_time_authority_mutation_fails_closed("document_occurred_at")

    async def test_projection_rejects_evidence_document_recorded_at_drift(self) -> None:
        await self._assert_time_authority_mutation_fails_closed("document_recorded_at")

    async def test_projection_rejects_evidence_row_recorded_at_drift(self) -> None:
        await self._assert_time_authority_mutation_fails_closed("row_recorded_at")

    async def test_projection_rejects_run_row_created_at_drift(self) -> None:
        await self._assert_time_authority_mutation_fails_closed("run_created_at")

    async def test_projection_rejects_final_source_event_occurred_at_drift(self) -> None:
        await self._assert_time_authority_mutation_fails_closed("source_event_occurred_at")

    async def test_worker_projects_strict_revisions_replays_and_rebuilds_exactly(self) -> None:
        first_event, first_context, _ = await self._commit_inference(1, 0)
        worker = self._worker()
        self.assertTrue(await worker.run_once())
        store = PostgresLearnerStore(self.database)
        first_snapshot_result = await store.get_snapshot(
            self.base_context.actor.actor_id,
            first_context,
        )
        self.assertIsInstance(first_snapshot_result, Success)
        first_snapshot = cast(Success[LearnerModelSnapshot], first_snapshot_result).value
        self.assertEqual(
            (first_snapshot.revision, first_snapshot.projected_through_sequence), (1, 1)
        )
        self.assertEqual((await self._counts()), (1, 1, 1, 1, 0))
        self.assertFalse(await worker.run_once())
        self.assertEqual((await self._counts()), (1, 1, 1, 1, 0))

        _, second_context, _ = await self._commit_inference(2, 1)
        self.assertTrue(await worker.run_once())
        online_result = await store.get_snapshot(
            self.base_context.actor.actor_id,
            second_context,
        )
        self.assertIsInstance(online_result, Success)
        online = cast(Success[LearnerModelSnapshot], online_result).value
        self.assertEqual((online.revision, online.projected_through_sequence), (2, 2))
        counts_before = await self._counts()
        rebuilt_result = await store.rebuild(
            self.base_context.actor.actor_id,
            2,
            self.base_context,
        )
        self.assertIsInstance(rebuilt_result, Success)
        rebuilt = cast(Success[LearnerModelSnapshot], rebuilt_result).value
        self.assertEqual(rebuilt, online)
        self.assertEqual(await self._counts(), counts_before)

        public_result = await store.project(first_event, 0, first_context)
        self.assertIsInstance(public_result, Failure)
        self.assertEqual(cast(Failure, public_result).error.code, "INVARIANT_VIOLATION")

    async def test_empty_rebuild_is_byte_deterministic_across_request_times(self) -> None:
        store = PostgresLearnerStore(self.database)
        first_context = replace(
            self.base_context,
            request_id="req_empty_rebuild_00000001",
            requested_at=NOW,
        )
        second_context = replace(
            self.base_context,
            request_id="req_empty_rebuild_00000002",
            requested_at=NOW + timedelta(days=90),
            deadline_at=NOW + timedelta(days=90, minutes=5),
        )
        first_result = await store.rebuild(
            self.base_context.actor.actor_id,
            0,
            first_context,
        )
        second_result = await store.rebuild(
            self.base_context.actor.actor_id,
            0,
            second_context,
        )
        self.assertIsInstance(first_result, Success)
        self.assertIsInstance(second_result, Success)
        first = cast(Success[LearnerModelSnapshot], first_result).value
        second = cast(Success[LearnerModelSnapshot], second_result).value
        self.assertEqual(first, second)
        self.assertEqual(encode(first), encode(second))
        self.assertEqual(first.updated_at.isoformat(), "1970-01-01T00:00:00+00:00")

    async def test_rebuild_row_hash_detects_decodable_tamper_without_receipts(self) -> None:
        store = PostgresLearnerStore(self.database)
        rebuilt_result = await store.rebuild(
            self.base_context.actor.actor_id,
            0,
            self.base_context,
        )
        self.assertIsInstance(rebuilt_result, Success)
        rebuilt = cast(Success[LearnerModelSnapshot], rebuilt_result).value
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                SELECT snapshot_sha256,updated_at,
                  (SELECT count(*) FROM yaya_learner_projection_receipts) AS receipts
                FROM yaya_learner_models
                WHERE tenant_id=%s AND learner_id=%s
                """,
                (
                    self.base_context.actor.tenant_id,
                    self.base_context.actor.actor_id,
                ),
            )
            row = await cursor.fetchone()
        if row is None:
            self.fail("empty rebuild did not persist a learner model row")
        self.assertEqual(row["snapshot_sha256"], internal_record_sha256(rebuilt))
        self.assertEqual(row["updated_at"], rebuilt.updated_at)
        self.assertEqual(row["receipts"], 0)

        tampered = replace(
            rebuilt,
            evidence_refs=(
                EvidenceRef(
                    evidence_id="evidence_rebuild_hash_00000001",
                    evidence_type=EvidenceType.ACTION_LOG,
                    created_at=NOW,
                    sha256="b" * 64,
                ),
            ),
        )
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                UPDATE yaya_learner_models SET snapshot_json=%s
                WHERE tenant_id=%s AND learner_id=%s
                """,
                (
                    Jsonb(encode(tampered)),
                    self.base_context.actor.tenant_id,
                    self.base_context.actor.actor_id,
                ),
            )
        tampered_read = await store.get_snapshot(
            self.base_context.actor.actor_id,
            self.base_context,
        )
        self.assertIsInstance(tampered_read, Failure)
        self.assertEqual(cast(Failure, tampered_read).error.code, "INVARIANT_VIOLATION")
        self.assertIn(
            "snapshot hash drifted",
            cast(Failure, tampered_read).error.message or "",
        )

        repaired_result = await store.rebuild(
            self.base_context.actor.actor_id,
            0,
            self.base_context,
        )
        self.assertEqual(repaired_result, Success(rebuilt))
        repaired_read = await store.get_snapshot(
            self.base_context.actor.actor_id,
            self.base_context,
        )
        self.assertEqual(repaired_read, Success(rebuilt))

    async def test_projection_backlog_uses_current_cas_not_historical_directive_revision(
        self,
    ) -> None:
        _, first_context, _ = await self._commit_inference(1, 0)
        _, second_context, _ = await self._commit_inference(2, 0)
        worker = self._worker()
        self.assertTrue(await worker.run_once())
        self.assertTrue(await worker.run_once())
        self.assertFalse(await worker.run_once())

        store = PostgresLearnerStore(self.database)
        online_result = await store.get_snapshot(
            self.base_context.actor.actor_id,
            second_context,
        )
        self.assertIsInstance(online_result, Success)
        online = cast(Success[LearnerModelSnapshot], online_result).value
        self.assertEqual((online.revision, online.projected_through_sequence), (2, 2))
        self.assertEqual((await self._counts())[-1], 0)

        rebuilt_result = await store.rebuild(
            self.base_context.actor.actor_id,
            2,
            first_context,
        )
        self.assertIsInstance(rebuilt_result, Success)
        rebuilt = cast(Success[LearnerModelSnapshot], rebuilt_result).value
        self.assertEqual(rebuilt, online)

    async def test_projection_compacts_more_than_64_sequential_evidence_refs(self) -> None:
        worker = self._worker("learner-store-evidence-retention")
        last_context = self.base_context
        for sequence in range(1, 67):
            _, last_context, _ = await self._commit_inference(
                sequence,
                sequence - 1,
            )
            self.assertTrue(await worker.run_once(), f"sequence {sequence} was not projected")

        store = PostgresLearnerStore(self.database)
        online_result = await store.get_snapshot(
            self.base_context.actor.actor_id,
            last_context,
        )
        self.assertIsInstance(online_result, Success)
        online = cast(Success[LearnerModelSnapshot], online_result).value
        self.assertEqual((online.revision, online.projected_through_sequence), (66, 66))
        expected_ids = tuple(f"evidence_learner_store_{sequence:08d}" for sequence in range(3, 67))
        self.assertEqual(
            tuple(item.evidence_id for item in online.evidence_refs),
            expected_ids,
        )
        self.assertEqual(set(online.competencies), {"for_loop"})
        for_loop = cast(dict[str, object], online.competencies["for_loop"])
        self.assertEqual(tuple(cast(tuple[object, ...], for_loop["evidence_ids"])), expected_ids)
        self.assertEqual(await self._counts(), (1, 66, 66, 66, 0))

        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                SELECT update_json FROM yaya_learner_projection_receipts
                WHERE tenant_id=%s AND learner_id=%s
                  AND source_stream_sequence=65
                """,
                (
                    self.base_context.actor.tenant_id,
                    self.base_context.actor.actor_id,
                ),
            )
            receipt = await cursor.fetchone()
        if receipt is None:
            self.fail("sequence-65 projection receipt was not durable")
        update = decode_as(receipt["update_json"], LearnerUpdate)
        self.assertEqual(
            update.changed_competency_ids,
            ("for_loop",),
        )

        rebuilt_result = await store.rebuild(
            self.base_context.actor.actor_id,
            66,
            self.base_context,
        )
        self.assertIsInstance(rebuilt_result, Success)
        rebuilt = cast(Success[LearnerModelSnapshot], rebuilt_result).value
        self.assertEqual(rebuilt, online)

    async def test_rebuild_rejects_checkpoint_regression_and_preserves_head(self) -> None:
        worker = self._worker("learner-store-rebuild-regression")
        third_context = self.base_context
        for sequence in range(1, 4):
            _, third_context, _ = await self._commit_inference(
                sequence,
                sequence - 1,
            )
            self.assertTrue(await worker.run_once())

        store = PostgresLearnerStore(self.database)
        before_result = await store.get_snapshot(
            self.base_context.actor.actor_id,
            third_context,
        )
        self.assertIsInstance(before_result, Success)
        before = cast(Success[LearnerModelSnapshot], before_result).value
        self.assertEqual((before.revision, before.projected_through_sequence), (3, 3))

        regression = await store.rebuild(
            self.base_context.actor.actor_id,
            1,
            self.base_context,
        )
        self.assertIsInstance(regression, Failure)
        regression_failure = cast(Failure, regression)
        self.assertEqual(regression_failure.error.code, "INVARIANT_VIOLATION")
        self.assertIn("checkpoint backwards", regression_failure.error.message or "")

        unchanged_result = await store.get_snapshot(
            self.base_context.actor.actor_id,
            third_context,
        )
        self.assertEqual(unchanged_result, Success(before))

        _, fourth_context, _ = await self._commit_inference(4, 3)
        self.assertTrue(await worker.run_once())
        advanced_result = await store.get_snapshot(
            self.base_context.actor.actor_id,
            fourth_context,
        )
        self.assertIsInstance(advanced_result, Success)
        advanced = cast(Success[LearnerModelSnapshot], advanced_result).value
        self.assertEqual((advanced.revision, advanced.projected_through_sequence), (4, 4))

    async def test_rebuild_repairs_untrusted_checkpoint_from_durable_applied_head(self) -> None:
        worker = self._worker("learner-store-rebuild-corrupt-checkpoint")
        third_context = self.base_context
        for sequence in range(1, 4):
            _, third_context, _ = await self._commit_inference(
                sequence,
                sequence - 1,
            )
            self.assertTrue(await worker.run_once())

        store = PostgresLearnerStore(self.database)
        online_result = await store.get_snapshot(
            self.base_context.actor.actor_id,
            third_context,
        )
        self.assertIsInstance(online_result, Success)
        online = cast(Success[LearnerModelSnapshot], online_result).value

        async with self.database.transaction() as connection:
            await connection.execute(
                """
                UPDATE yaya_learner_models
                SET revision=999,projected_through_sequence=999
                WHERE tenant_id=%s AND learner_id=%s
                """,
                (
                    third_context.actor.tenant_id,
                    third_context.actor.actor_id,
                ),
            )

        corrupt_read = await store.get_snapshot(
            self.base_context.actor.actor_id,
            third_context,
        )
        self.assertIsInstance(corrupt_read, Failure)
        self.assertEqual(cast(Failure, corrupt_read).error.code, "INVARIANT_VIOLATION")

        repaired_result = await store.rebuild(
            self.base_context.actor.actor_id,
            3,
            third_context,
        )
        self.assertEqual(repaired_result, Success(online))
        self.assertEqual(
            await store.get_snapshot(
                self.base_context.actor.actor_id,
                third_context,
            ),
            Success(online),
        )

    async def test_fence_loss_and_atomic_projection_fault_leave_no_partial_write(self) -> None:
        event, context, _ = await self._commit_inference(1, 0)
        store = PostgresLearnerStore(self.database)
        stale = await self._claim()
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                UPDATE yaya_learner_projection_jobs
                SET heartbeat_at=clock_timestamp()-interval '2 seconds',
                    lease_expires_at=clock_timestamp()-interval '1 second'
                WHERE tenant_id=%s AND job_id=%s
                """,
                (stale.tenant_id, stale.job_id),
            )
        with self.assertRaises(LearnerProjectionFenceLost):
            await store.project_fenced(event, 0, context, stale.fence)

        lease = await self._claim()
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                CREATE FUNCTION yaya_test_fail_learner_receipt() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION 'injected learner receipt failure'
                        USING ERRCODE = '40001';
                END
                $$
                """
            )
            await connection.execute(
                """
                CREATE TRIGGER yaya_test_fail_learner_receipt
                BEFORE INSERT ON yaya_learner_projection_receipts
                FOR EACH ROW EXECUTE FUNCTION yaya_test_fail_learner_receipt()
                """
            )
        result = await store.project_fenced(event, 0, context, lease.fence)
        self.assertIsInstance(result, Failure)
        self.assertEqual(await self._counts(), (0, 0, 0, 0, 0))
        async with self.database.transaction() as connection:
            await connection.execute(
                "DROP TRIGGER yaya_test_fail_learner_receipt ON yaya_learner_projection_receipts"
            )
            await connection.execute("DROP FUNCTION yaya_test_fail_learner_receipt()")
        recovered = await store.project_fenced(event, 0, context, lease.fence)
        self.assertIsInstance(recovered, Success)
        self.assertEqual(await self._counts(), (1, 1, 1, 1, 0))

    async def test_terminal_failure_is_atomic_and_does_not_create_snapshot(self) -> None:
        event, context, _ = await self._commit_inference(1, 0)
        lease = await self._claim()
        error = ContractError(
            code="AUTHORIZATION_DENIED",
            category=ErrorCategory.AUTHORIZATION,
            retryable=False,
            user_message_key="auth.permission_denied",
            stage="COMPLETE",
            message="Injected permanent learner projection authority failure.",
        )
        result = await PostgresLearnerStore(self.database).fail_fenced(
            event,
            error,
            context,
            lease.fence,
        )
        self.assertEqual(result, Success(None))
        self.assertEqual(await self._counts(), (0, 0, 0, 1, 1))
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                SELECT j.state,j.last_error_code,f.classification,
                       e.event_type,e.stream_id
                FROM yaya_learner_projection_jobs j
                JOIN yaya_learner_projection_failures f
                  ON f.tenant_id=j.tenant_id AND f.job_id=j.job_id
                JOIN yaya_events e
                  ON e.tenant_id=f.tenant_id AND e.event_id=f.failure_event_id
                WHERE j.tenant_id=%s AND j.job_id=%s
                """,
                (lease.tenant_id, lease.job_id),
            )
            row = await cursor.fetchone()
        if row is None:
            self.fail("terminal learner projection failure was not durable")
        self.assertEqual(
            (
                row["state"],
                row["last_error_code"],
                row["classification"],
                row["event_type"],
                row["stream_id"],
            ),
            (
                "FAILED",
                "AUTHORIZATION_DENIED",
                "PERMANENT",
                "learner.projection.failed",
                f"learner-model:{self.base_context.actor.actor_id}",
            ),
        )

    async def test_rebuild_detects_evidence_hash_drift(self) -> None:
        _, context, evidence_document = await self._commit_inference(1, 0)
        self.assertTrue(await self._worker().run_once())
        evidence_id = "evidence_learner_store_00000001"
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                UPDATE yaya_evidence SET evidence_json=%s
                WHERE tenant_id=%s AND evidence_id=%s
                """,
                (Jsonb({"tampered": True}), context.actor.tenant_id, evidence_id),
            )
        result = await PostgresLearnerStore(self.database).rebuild(
            context.actor.actor_id,
            1,
            context,
        )
        self.assertIsInstance(result, Failure)
        self.assertEqual(cast(Failure, result).error.code, "INVARIANT_VIOLATION")
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                UPDATE yaya_evidence SET evidence_json=%s
                WHERE tenant_id=%s AND evidence_id=%s
                """,
                (Jsonb(evidence_document), context.actor.tenant_id, evidence_id),
            )
        repaired = await PostgresLearnerStore(self.database).rebuild(
            context.actor.actor_id,
            1,
            context,
        )
        self.assertIsInstance(repaired, Success)

    async def test_rebuild_rejects_event_json_swapped_between_immutable_rows(self) -> None:
        _, first_context, _ = await self._commit_inference(1, 0)
        await self._commit_inference(2, 0)
        async with self.database.transaction() as connection:
            updated = await connection.execute(
                """
                UPDATE yaya_events AS target
                SET event_json=source.event_json
                FROM yaya_events AS source
                WHERE target.tenant_id=%s AND target.stream_id=%s
                  AND target.sequence=1
                  AND source.tenant_id=target.tenant_id
                  AND source.stream_id=target.stream_id
                  AND source.sequence=2
                """,
                (
                    first_context.actor.tenant_id,
                    f"learner:{first_context.actor.actor_id}",
                ),
            )
        self.assertEqual(updated.rowcount, 1)

        result = await PostgresLearnerStore(self.database).rebuild(
            first_context.actor.actor_id,
            2,
            first_context,
        )
        self.assertIsInstance(result, Failure)
        failure = cast(Failure, result)
        self.assertEqual(failure.error.code, "INVARIANT_VIOLATION")
        self.assertIn("canonical event identity drifted", failure.error.message or "")

    async def _assert_commit_rollback_is_known(self, sqlstate: str) -> None:
        suffix = sqlstate.lower()
        function_name = sql.Identifier(f"yaya_test_learner_commit_{suffix}")
        trigger_name = sql.Identifier(f"yaya_test_learner_commit_{suffix}")
        create_function = sql.SQL(
            """
            CREATE FUNCTION {}() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'injected confirmed rollback at commit'
                    USING ERRCODE = {};
            END
            $$
            """
        ).format(function_name, sql.Literal(sqlstate))
        create_trigger = sql.SQL(
            """
            CREATE CONSTRAINT TRIGGER {}
            AFTER INSERT OR UPDATE ON yaya_learner_models
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION {}()
            """
        ).format(trigger_name, function_name)
        drop_trigger = sql.SQL("DROP TRIGGER {} ON yaya_learner_models").format(trigger_name)
        drop_function = sql.SQL("DROP FUNCTION {}()").format(function_name)
        async with self.database.transaction() as connection:
            await connection.execute(create_function)
            await connection.execute(create_trigger)
        try:
            result = await PostgresLearnerStore(self.database).rebuild(
                self.base_context.actor.actor_id,
                0,
                self.base_context,
            )
            self.assertIsInstance(result, Failure)
            failure = cast(Failure, result)
            self.assertEqual(failure.error.code, "DEPENDENCY_UNAVAILABLE")
            self.assertNotEqual(failure.error.code, "UNKNOWN_COMMIT_STATE")
        finally:
            async with self.database.transaction() as connection:
                await connection.execute(drop_trigger)
                await connection.execute(drop_function)

    async def test_commit_time_serialization_and_deadlock_are_confirmed_rollbacks(
        self,
    ) -> None:
        for sqlstate in ("40001", "40P01"):
            with self.subTest(sqlstate=sqlstate):
                await self._assert_commit_rollback_is_known(sqlstate)

    async def test_successful_retry_resolves_retryable_failure_history(self) -> None:
        await self._commit_inference(1, 0)
        worker = self._worker("learner-store-retry-resolution")
        lease = await worker.claim_one()
        if lease is None:
            self.fail("expected initial learner projection lease")
        await worker._release_for_retry(  # pyright: ignore[reportPrivateUsage]
            lease,
            "DEPENDENCY_UNAVAILABLE",
            {"code": "DEPENDENCY_UNAVAILABLE", "redacted": True},
        )
        await self._make_job_available(lease.job_id)

        self.assertTrue(await worker.run_once())
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                SELECT classification,resolution,resolved_at
                FROM yaya_learner_projection_failures
                WHERE tenant_id=%s AND job_id=%s
                """,
                (lease.tenant_id, lease.job_id),
            )
            failure = await cursor.fetchone()
        if failure is None:
            self.fail("retryable learner projection failure was not retained")
        self.assertEqual(failure["classification"], "RETRYABLE")
        self.assertEqual(failure["resolution"], "RETRIED")
        self.assertIsNotNone(failure["resolved_at"])

    async def test_rebuild_rejects_ready_and_leased_jobs_without_mutation(self) -> None:
        _, context, _ = await self._commit_inference(1, 0)
        store = PostgresLearnerStore(self.database)

        ready_result = await store.rebuild(context.actor.actor_id, 1, context)
        self.assertIsInstance(ready_result, Failure)
        ready_failure = cast(Failure, ready_result)
        self.assertEqual(ready_failure.error.code, "INVARIANT_VIOLATION")
        self.assertIn("active projection Job", ready_failure.error.message or "")

        lease = await self._claim()
        leased_result = await store.rebuild(context.actor.actor_id, 1, context)
        self.assertIsInstance(leased_result, Failure)
        leased_failure = cast(Failure, leased_result)
        self.assertEqual(leased_failure.error.code, "INVARIANT_VIOLATION")
        self.assertIn("active projection Job", leased_failure.error.message or "")
        async with self.database.transaction() as connection:
            model_cursor = await connection.execute(
                """
                SELECT count(*) AS value FROM yaya_learner_models
                WHERE tenant_id=%s AND learner_id=%s
                """,
                (context.actor.tenant_id, context.actor.actor_id),
            )
            model_row = await model_cursor.fetchone()
            job_cursor = await connection.execute(
                """
                SELECT state,worker_id,lease_id,fencing_token
                FROM yaya_learner_projection_jobs
                WHERE tenant_id=%s AND job_id=%s
                """,
                (lease.tenant_id, lease.job_id),
            )
            job_row = await job_cursor.fetchone()
        self.assertIsNotNone(model_row)
        self.assertEqual(None if model_row is None else model_row["value"], 0)
        self.assertIsNotNone(job_row)
        if job_row is not None:
            self.assertEqual(job_row["state"], "LEASED")
            self.assertEqual(job_row["worker_id"], lease.fence.worker_id)
            self.assertEqual(job_row["lease_id"], lease.fence.lease_id)
            self.assertEqual(job_row["fencing_token"], lease.fence.fencing_token)

    async def test_rebuild_resolves_terminal_failures_and_unblocks_next_job(self) -> None:
        first_event, first_context, _ = await self._commit_inference(1, 0)
        _, second_context, _ = await self._commit_inference(2, 0)
        await self._commit_inference(3, 0)
        worker = self._worker("learner-store-rebuild-recovery")
        store = PostgresLearnerStore(self.database)

        first_lease = await worker.claim_one()
        if first_lease is None:
            self.fail("expected sequence-one learner projection lease")
        await worker._release_for_retry(  # pyright: ignore[reportPrivateUsage]
            first_lease,
            "DEPENDENCY_UNAVAILABLE",
            {"code": "DEPENDENCY_UNAVAILABLE", "redacted": True},
        )
        await self._make_job_available(first_lease.job_id)
        permanent_lease = await worker.claim_one()
        if permanent_lease is None:
            self.fail("expected sequence-one retry lease")
        terminal_error = ContractError(
            code="AUTHORIZATION_DENIED",
            category=ErrorCategory.AUTHORIZATION,
            retryable=False,
            user_message_key="auth.permission_denied",
            stage="COMPLETE",
            message="Injected repairable terminal projection failure.",
        )
        terminal_result = await store.fail_fenced(
            first_event,
            terminal_error,
            first_context,
            permanent_lease.fence,
        )
        self.assertEqual(terminal_result, Success(None))

        first_rebuild = await store.rebuild(
            first_context.actor.actor_id,
            1,
            first_context,
        )
        self.assertIsInstance(first_rebuild, Success)
        first_snapshot = cast(Success[LearnerModelSnapshot], first_rebuild).value
        self.assertEqual(first_snapshot.projected_through_sequence, 1)

        self.assertTrue(await worker.run_once())
        second_lease = await worker.claim_one()
        if second_lease is None:
            self.fail("resolved permanent failure did not unblock sequence two")
        self.assertEqual(second_lease.source_stream_sequence, 2)
        await worker._release_for_retry(  # pyright: ignore[reportPrivateUsage]
            second_lease,
            "DEPENDENCY_UNAVAILABLE",
            {"code": "DEPENDENCY_UNAVAILABLE", "redacted": True},
        )
        await self._make_job_available(second_lease.job_id)
        quarantined_lease = await worker.claim_one()
        if quarantined_lease is None:
            self.fail("expected sequence-two retry lease")
        await worker._quarantine(  # pyright: ignore[reportPrivateUsage]
            quarantined_lease,
            "INVARIANT_VIOLATION",
            {
                "code": "INVARIANT_VIOLATION",
                "cause": "InjectedRebuildRecoveryQuarantine",
                "redacted": True,
            },
        )

        second_rebuild = await store.rebuild(
            second_context.actor.actor_id,
            2,
            second_context,
        )
        self.assertIsInstance(second_rebuild, Success)
        second_snapshot = cast(Success[LearnerModelSnapshot], second_rebuild).value
        self.assertEqual(second_snapshot.projected_through_sequence, 2)

        async with self.database.transaction() as connection:
            failure_cursor = await connection.execute(
                """
                SELECT source_stream_sequence,classification,resolution,resolved_at
                FROM yaya_learner_projection_failures
                WHERE tenant_id=%s AND learner_id=%s
                ORDER BY source_stream_sequence,attempt
                """,
                (first_context.actor.tenant_id, first_context.actor.actor_id),
            )
            failures = list(await failure_cursor.fetchall())
            job_cursor = await connection.execute(
                """
                SELECT source_stream_sequence,state
                FROM yaya_learner_projection_jobs
                WHERE tenant_id=%s AND learner_id=%s
                ORDER BY source_stream_sequence
                """,
                (first_context.actor.tenant_id, first_context.actor.actor_id),
            )
            jobs_before_next = list(await job_cursor.fetchall())
        self.assertEqual(
            [
                (row["source_stream_sequence"], row["classification"], row["resolution"])
                for row in failures
            ],
            [
                (1, "RETRYABLE", "REBUILT"),
                (1, "PERMANENT", "REBUILT"),
                (2, "RETRYABLE", "REBUILT"),
                (2, "QUARANTINED", "REBUILT"),
            ],
        )
        self.assertTrue(all(row["resolved_at"] is not None for row in failures))
        self.assertEqual(
            [(row["source_stream_sequence"], row["state"]) for row in jobs_before_next],
            [(1, "FAILED"), (2, "FAILED"), (3, "READY")],
        )

        self.assertTrue(await worker.run_once())
        final_result = await store.get_snapshot(
            first_context.actor.actor_id,
            first_context,
        )
        self.assertIsInstance(final_result, Success)
        final_snapshot = cast(Success[LearnerModelSnapshot], final_result).value
        self.assertEqual(
            (final_snapshot.revision, final_snapshot.projected_through_sequence), (3, 3)
        )


if __name__ == "__main__":
    unittest.main()
