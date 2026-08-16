from __future__ import annotations

import asyncio
import hashlib
import sys
import unittest
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from postgres_test_support import postgres_test_server  # noqa: E402
from psycopg import AsyncConnection  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from yaya_agent_backend.codec import (  # noqa: E402
    agent_turn_commit_sha256,
    decode_as,
    encode,
    internal_record_sha256,
    plain,
)
from yaya_agent_backend.database import PostgresDatabase  # noqa: E402
from yaya_agent_backend.learner_projection import (  # noqa: E402
    LearnerProjectionDurableGraphCorrupt,
    LearnerProjectionFence,
    LearnerProjectionFenceLost,
    LearnerProjectionWorker,
    LearnerProjectionWorkerError,
)
from yaya_agent_contracts import (  # noqa: E402
    ActorRef,
    ActorType,
    ContentRef,
    ContractError,
    ErrorCategory,
    EvidenceRef,
    EvidenceType,
    Failure,
    FrozenJsonValue,
    LearnerModelSnapshot,
    LearnerUpdate,
    OperationContext,
    Result,
    RuntimeEvent,
    RuntimeEventType,
    Success,
    canonical_json_sha256,
    learner_inference_sha256,
)
from yaya_agent_runtime.learner_projection_policy import (  # noqa: E402
    CompetencyProjection,
    EvidenceStage,
)

NOW = datetime(2026, 8, 9, 9, 30, tzinfo=UTC)
TENANT_ID = "tenant_projection"
LEARNER_ID = "student_projection_0001"
TASK_ID = "task_projection_0001"
SESSION_ID = "session_projection_0001"
WORLD_ID = "world_projection_0001"
CONTENT_HASH = "a" * 64


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _evidence_wire(evidence: EvidenceRef) -> dict[str, object]:
    if evidence.sha256 is None:
        raise AssertionError("learner inference fixture Evidence must carry sha256")
    return {
        "evidence_id": evidence.evidence_id,
        "evidence_type": evidence.evidence_type.value,
        "created_at": _iso(evidence.created_at),
        "sha256": evidence.sha256,
    }


def _error_wire(error: ContractError) -> dict[str, object]:
    value: dict[str, object] = {
        "code": error.code,
        "category": error.category.value,
        "retryable": error.retryable,
        "user_message_key": error.user_message_key,
        "stage": error.stage,
    }
    if error.message is not None:
        value["message"] = error.message
    if error.details:
        value["details"] = dict(error.details)
    if error.evidence_ids:
        value["evidence_ids"] = list(error.evidence_ids)
    return value


def _identifier(prefix: str, seed: Mapping[str, object]) -> str:
    return f"{prefix}_{canonical_json_sha256(seed)[:32]}"


def _dependency_error() -> ContractError:
    return ContractError(
        code="DEPENDENCY_UNAVAILABLE",
        category=ErrorCategory.DEPENDENCY,
        retryable=True,
        user_message_key="dependency.temporarily_unavailable",
        stage="COMPLETE",
        message="Injected recoverable projector dependency failure.",
    )


def _authorization_error() -> ContractError:
    return ContractError(
        code="AUTHORIZATION_DENIED",
        category=ErrorCategory.AUTHORIZATION,
        retryable=False,
        user_message_key="auth.permission_denied",
        stage="COMPLETE",
        message="Injected permanent projector authority failure.",
    )


def _unknown_commit_error() -> ContractError:
    return ContractError(
        code="UNKNOWN_COMMIT_STATE",
        category=ErrorCategory.DEPENDENCY,
        retryable=False,
        user_message_key="command.reconciling",
        stage="COMPLETE",
        message="Injected unknown failure COMMIT result.",
    )


class _AtomicFencedProjector:
    """Real-PostgreSQL fault harness for the internal fenced port.

    This is intentionally not an in-memory LearnerPort: every success/failure
    verifies the live database fence and atomically writes the same durable
    surfaces the production adapter is required to own.
    """

    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database
        self.mode = "success"
        self.project_calls = 0
        self.fail_calls = 0
        self.drop_response_once = False
        self.drop_failure_response_once = False
        self.failure_unknown_result_once = False
        self.success_graph_tamper: str | None = None
        self.failure_graph_tamper: str | None = None

    async def project_fenced(
        self,
        event: RuntimeEvent,
        expected_learner_revision: int,
        context: OperationContext,
        fence: LearnerProjectionFence,
    ) -> Result[LearnerUpdate]:
        self.project_calls += 1
        if self.mode == "retry":
            return Failure(_dependency_error())
        if self.mode == "permanent":
            return Failure(_authorization_error())
        payload = event.payload
        evidence = tuple(
            EvidenceRef(
                cast(str, item["evidence_id"]),
                EvidenceType(cast(str, item["evidence_type"])),
                datetime.fromisoformat(cast(str, item["created_at"]).replace("Z", "+00:00")),
                sha256=cast(str, item["sha256"]),
            )
            for item in cast(tuple[Mapping[str, object], ...], payload["evidence_refs"])
        )
        async with self.database.transaction() as connection:
            job_cursor = await connection.execute(
                """
                SELECT learner_id,actor_id,content_hash,source_stream_sequence,
                       event_sha256,inference_sha256
                FROM yaya_learner_projection_jobs
                WHERE tenant_id=%s AND job_id=%s AND state='LEASED'
                  AND worker_id=%s AND lease_id=%s AND fencing_token=%s
                  AND lease_expires_at>clock_timestamp()
                FOR UPDATE
                """,
                (
                    fence.tenant_id,
                    fence.job_id,
                    fence.worker_id,
                    fence.lease_id,
                    fence.fencing_token,
                ),
            )
            job = await job_cursor.fetchone()
            if job is None:
                return Failure(
                    ContractError(
                        code="EVENT_SEQUENCE_GAP",
                        category=ErrorCategory.CONCURRENCY,
                        retryable=True,
                        user_message_key="event.resync_required",
                        stage="COMPLETE",
                        message="Injected stale projection fence.",
                    )
                )
            model_cursor = await connection.execute(
                """
                SELECT revision,projected_through_sequence,snapshot_json
                FROM yaya_learner_models
                WHERE tenant_id=%s AND learner_id=%s FOR UPDATE
                """,
                (fence.tenant_id, job["learner_id"]),
            )
            model_row = await model_cursor.fetchone()
            current_revision = 0 if model_row is None else cast(int, model_row["revision"])
            checkpoint = (
                0 if model_row is None else cast(int, model_row["projected_through_sequence"])
            )
            if (
                current_revision != expected_learner_revision
                or cast(int, job["source_stream_sequence"]) != checkpoint + 1
            ):
                return Failure(
                    ContractError(
                        code="EVENT_SEQUENCE_GAP",
                        category=ErrorCategory.CONCURRENCY,
                        retryable=True,
                        user_message_key="event.resync_required",
                        stage="COMPLETE",
                        message="Injected learner revision CAS conflict.",
                    )
                )
            concept = cast(str, payload["concept"])
            snapshot = LearnerModelSnapshot(
                learner_id=cast(str, job["learner_id"]),
                revision=current_revision + 1,
                model_version="learner-policy-v1",
                projected_through_sequence=event.sequence,
                competencies={
                    concept: cast(
                        FrozenJsonValue,
                        plain(
                            CompetencyProjection(
                                concept=concept,
                                evidence_stage=EvidenceStage.OBSERVED,
                                assistance_level=0,
                                last_observed_at=event.occurred_at,
                                next_review_at=event.occurred_at + timedelta(days=1),
                                evidence_ids=tuple(item.evidence_id for item in evidence),
                            )
                        ),
                    )
                },
                updated_at=event.occurred_at,
                evidence_refs=evidence,
            )
            update = LearnerUpdate(
                learner_id=snapshot.learner_id,
                previous_revision=current_revision,
                revision=snapshot.revision,
                model_version=snapshot.model_version,
                changed_competency_ids=(concept,),
                evidence_refs=evidence,
                updated_at=snapshot.updated_at,
            )
            snapshot_json = cast(Mapping[str, object], encode(snapshot))
            snapshot_sha256 = internal_record_sha256(snapshot)
            await connection.execute(
                """
                INSERT INTO yaya_learner_models(
                    tenant_id,learner_id,actor_id,content_hash,revision,
                    projected_through_sequence,snapshot_json,snapshot_sha256,updated_at,
                    request_context_json,projection_policy_version
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id,learner_id) DO UPDATE SET
                    revision=EXCLUDED.revision,
                    projected_through_sequence=EXCLUDED.projected_through_sequence,
                    snapshot_json=EXCLUDED.snapshot_json,
                    snapshot_sha256=EXCLUDED.snapshot_sha256,
                    updated_at=EXCLUDED.updated_at,
                    request_context_json=EXCLUDED.request_context_json,
                    projection_policy_version=EXCLUDED.projection_policy_version
                """,
                (
                    fence.tenant_id,
                    snapshot.learner_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    snapshot.revision,
                    snapshot.projected_through_sequence,
                    Jsonb(snapshot_json),
                    snapshot_sha256,
                    snapshot.updated_at,
                    Jsonb(encode(context)),
                    "learner-policy-v1",
                ),
            )
            projected_at_cursor = await connection.execute("SELECT clock_timestamp() AS value")
            projected_at_row = await projected_at_cursor.fetchone()
            if projected_at_row is None:
                raise AssertionError("PostgreSQL projection clock query returned no row")
            projected_at = cast(datetime, projected_at_row["value"])
            output_stream = f"learner-model:{snapshot.learner_id}"
            sequence_cursor = await connection.execute(
                """
                SELECT COALESCE(max(sequence),0)+1 AS next_sequence
                FROM yaya_events WHERE tenant_id=%s AND stream_id=%s
                """,
                (fence.tenant_id, output_stream),
            )
            sequence_row = await sequence_cursor.fetchone()
            if sequence_row is None:
                raise AssertionError("PostgreSQL output sequence query returned no row")
            output_sequence = cast(int, sequence_row["next_sequence"])
            identity_seed = {
                "kind": "learner_model_updated_v1",
                "tenant_id": fence.tenant_id,
                "job_id": fence.job_id,
                "event_id": event.event_id,
                "event_sha256": job["event_sha256"],
            }
            output_event = RuntimeEvent(
                event_id=_identifier("evt_learner_model", identity_seed),
                event_type=RuntimeEventType.LEARNER_MODEL_UPDATED,
                event_version=1,
                schema_version="1.0.0",
                stream_id=output_stream,
                sequence=output_sequence,
                occurred_at=projected_at,
                producer="learner_projection_worker",
                trace_id=event.trace_id,
                command_id=event.command_id,
                correlation_id=event.correlation_id,
                causation_id=event.event_id,
                content_ref=event.content_ref,
                payload={
                    "learner_id": snapshot.learner_id,
                    "previous_revision": update.previous_revision,
                    "learner_revision": update.revision,
                    "projected_through_sequence": event.sequence,
                    "changed_competency_ids": [concept],
                    "updated_at": _iso(update.updated_at),
                    "evidence_refs": [_evidence_wire(item) for item in evidence],
                },
            )
            output_json = cast(Mapping[str, object], encode(output_event))
            output_wire = cast(Mapping[str, object], plain(output_event))
            output_hash = internal_record_sha256(output_wire)
            outbox_id = _identifier("learner_model_msg", identity_seed)
            await connection.execute(
                """
                INSERT INTO yaya_events(
                    tenant_id,event_id,stream_id,sequence,event_type,event_json,occurred_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    fence.tenant_id,
                    output_event.event_id,
                    output_event.stream_id,
                    output_event.sequence,
                    output_event.event_type,
                    Jsonb(output_json),
                    output_event.occurred_at,
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_outbox(
                    tenant_id,message_id,destination,idempotency_key,payload_sha256,
                    status,attempt,message_json,created_at
                ) VALUES (%s,%s,'learner_model_events',%s,%s,'PENDING',0,%s,%s)
                """,
                (
                    fence.tenant_id,
                    outbox_id,
                    f"learner-model:{event.event_id}",
                    output_hash,
                    Jsonb(output_wire),
                    projected_at,
                ),
            )
            update_json = cast(Mapping[str, object], encode(update))
            snapshot_sha256 = internal_record_sha256(snapshot)
            receipt_record: dict[str, object] = {
                "tenant_id": fence.tenant_id,
                "event_id": event.event_id,
                "job_id": fence.job_id,
                "source_event_id": event.causation_id,
                "learner_id": snapshot.learner_id,
                "source_stream_sequence": event.sequence,
                "event_sha256": job["event_sha256"],
                "inference_sha256": job["inference_sha256"],
                "previous_learner_revision": update.previous_revision,
                "learner_revision": update.revision,
                "model_version": update.model_version,
                "snapshot_sha256": snapshot_sha256,
                "model_updated_event_id": output_event.event_id,
                "outbox_message_id": outbox_id,
                "update": plain(update),
                "projected_at": plain(projected_at),
            }
            await connection.execute(
                """
                INSERT INTO yaya_learner_projection_receipts(
                    tenant_id,event_id,job_id,source_event_id,learner_id,
                    actor_id,content_hash,source_stream_id,source_stream_sequence,
                    event_sha256,inference_sha256,previous_learner_revision,
                    learner_revision,model_version,snapshot_sha256,
                    model_updated_event_id,outbox_message_id,update_json,receipt_sha256,
                    projected_at
                )
                SELECT tenant_id,event_id,job_id,source_event_id,learner_id,
                       actor_id,content_hash,source_stream_id,source_stream_sequence,
                       event_sha256,inference_sha256,%s,%s,%s,%s,%s,%s,%s,%s,%s
                FROM yaya_learner_projection_jobs
                WHERE tenant_id=%s AND job_id=%s
                """,
                (
                    update.previous_revision,
                    update.revision,
                    update.model_version,
                    snapshot_sha256,
                    output_event.event_id,
                    outbox_id,
                    Jsonb(update_json),
                    internal_record_sha256(receipt_record),
                    projected_at,
                    fence.tenant_id,
                    fence.job_id,
                ),
            )
            finished = await connection.execute(
                """
                UPDATE yaya_learner_projection_jobs
                SET state='SUCCEEDED',worker_id=NULL,lease_id=NULL,
                    claimed_at=NULL,heartbeat_at=NULL,lease_expires_at=NULL,
                    last_error_code=NULL,last_error_json=NULL,
                    succeeded_at=%s,updated_at=clock_timestamp()
                WHERE tenant_id=%s AND job_id=%s AND state='LEASED'
                  AND worker_id=%s AND lease_id=%s AND fencing_token=%s
                  AND lease_expires_at>clock_timestamp()
                """,
                (
                    projected_at,
                    fence.tenant_id,
                    fence.job_id,
                    fence.worker_id,
                    fence.lease_id,
                    fence.fencing_token,
                ),
            )
            if finished.rowcount != 1:
                raise AssertionError("test projector lost its fencing token")
        if self.success_graph_tamper is not None:
            async with self.database.transaction() as connection:
                if self.success_graph_tamper == "receipt_hash":
                    await connection.execute(
                        """
                        UPDATE yaya_learner_projection_receipts
                        SET receipt_sha256=%s
                        WHERE tenant_id=%s AND job_id=%s
                        """,
                        ("f" * 64, fence.tenant_id, fence.job_id),
                    )
                elif self.success_graph_tamper == "derived_event":
                    await connection.execute(
                        """
                        UPDATE yaya_events SET event_json=%s
                        WHERE tenant_id=%s AND event_id=%s
                        """,
                        (Jsonb(encode(event)), fence.tenant_id, output_event.event_id),
                    )
                elif self.success_graph_tamper == "model_snapshot":
                    tampered_snapshot = LearnerModelSnapshot(
                        learner_id=snapshot.learner_id,
                        revision=snapshot.revision,
                        model_version=snapshot.model_version,
                        projected_through_sequence=snapshot.projected_through_sequence,
                        competencies={},
                        updated_at=snapshot.updated_at,
                        evidence_refs=snapshot.evidence_refs,
                    )
                    await connection.execute(
                        """
                        UPDATE yaya_learner_models SET snapshot_json=%s
                        WHERE tenant_id=%s AND learner_id=%s
                        """,
                        (
                            Jsonb(encode(tampered_snapshot)),
                            fence.tenant_id,
                            snapshot.learner_id,
                        ),
                    )
                elif self.success_graph_tamper == "outbox_payload":
                    await connection.execute(
                        """
                        UPDATE yaya_outbox SET message_json='{}'::jsonb
                        WHERE tenant_id=%s AND message_id=%s
                        """,
                        (fence.tenant_id, outbox_id),
                    )
                elif self.success_graph_tamper == "job_event_json":
                    tampered_payload = {
                        **event.payload,
                        "reason": "tampered after COMMIT",
                    }
                    tampered_payload["inference_sha256"] = learner_inference_sha256(
                        tampered_payload
                    )
                    tampered_source = replace(
                        event,
                        payload=tampered_payload,
                    )
                    await connection.execute(
                        """
                        UPDATE yaya_learner_projection_jobs SET event_json=%s
                        WHERE tenant_id=%s AND job_id=%s
                        """,
                        (Jsonb(encode(tampered_source)), fence.tenant_id, fence.job_id),
                    )
                elif self.success_graph_tamper == "job_event_hash":
                    await connection.execute(
                        """
                        UPDATE yaya_learner_projection_jobs SET event_sha256=%s
                        WHERE tenant_id=%s AND job_id=%s
                        """,
                        ("f" * 64, fence.tenant_id, fence.job_id),
                    )
                else:
                    raise AssertionError("unknown success graph tamper mode")
        if self.drop_response_once:
            self.drop_response_once = False
            raise ConnectionError("injected response loss after projection COMMIT")
        return Success(update)

    async def fail_fenced(
        self,
        event: RuntimeEvent,
        error: ContractError,
        context: OperationContext,
        fence: LearnerProjectionFence,
    ) -> Result[None]:
        del context
        self.fail_calls += 1
        async with self.database.transaction() as connection:
            job_cursor = await connection.execute(
                """
                SELECT * FROM yaya_learner_projection_jobs
                WHERE tenant_id=%s AND job_id=%s AND state='LEASED'
                  AND worker_id=%s AND lease_id=%s AND fencing_token=%s
                  AND lease_expires_at>clock_timestamp()
                FOR UPDATE
                """,
                (
                    fence.tenant_id,
                    fence.job_id,
                    fence.worker_id,
                    fence.lease_id,
                    fence.fencing_token,
                ),
            )
            job = await job_cursor.fetchone()
            if job is None:
                return Failure(_dependency_error())
            clock_cursor = await connection.execute("SELECT clock_timestamp() AS value")
            clock_row = await clock_cursor.fetchone()
            if clock_row is None:
                raise AssertionError("PostgreSQL failure clock query returned no row")
            failed_at = cast(datetime, clock_row["value"])
            error_json = _error_wire(error)
            error_sha256 = internal_record_sha256(error_json)
            identity_seed = {
                "kind": "learner_projection_failed_v1",
                "tenant_id": fence.tenant_id,
                "job_id": fence.job_id,
                "event_id": event.event_id,
                "attempt": fence.fencing_token,
                "error_sha256": error_sha256,
            }
            output_stream = f"learner-model:{job['learner_id']}"
            sequence_cursor = await connection.execute(
                """
                SELECT COALESCE(max(sequence),0)+1 AS next_sequence
                FROM yaya_events WHERE tenant_id=%s AND stream_id=%s
                """,
                (fence.tenant_id, output_stream),
            )
            sequence_row = await sequence_cursor.fetchone()
            if sequence_row is None:
                raise AssertionError("PostgreSQL output sequence query returned no row")
            failure_event = RuntimeEvent(
                event_id=_identifier("evt_learner_failed", identity_seed),
                event_type=RuntimeEventType.LEARNER_PROJECTION_FAILED,
                event_version=1,
                schema_version="1.0.0",
                stream_id=output_stream,
                sequence=cast(int, sequence_row["next_sequence"]),
                occurred_at=failed_at,
                producer="learner_projection_worker",
                trace_id=event.trace_id,
                command_id=event.command_id,
                correlation_id=event.correlation_id,
                causation_id=event.event_id,
                content_ref=event.content_ref,
                payload={
                    "learner_id": cast(str, job["learner_id"]),
                    "source_event_id": event.event_id,
                    "failed_at": _iso(failed_at),
                    "error": _error_wire(error),
                },
            )
            failure_json = cast(Mapping[str, object], encode(failure_event))
            failure_wire = cast(Mapping[str, object], plain(failure_event))
            failure_hash = internal_record_sha256(failure_wire)
            outbox_id = _identifier("learner_failed_msg", identity_seed)
            await connection.execute(
                """
                INSERT INTO yaya_events(
                    tenant_id,event_id,stream_id,sequence,event_type,event_json,occurred_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    fence.tenant_id,
                    failure_event.event_id,
                    failure_event.stream_id,
                    failure_event.sequence,
                    failure_event.event_type,
                    Jsonb(failure_json),
                    failure_event.occurred_at,
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_outbox(
                    tenant_id,message_id,destination,idempotency_key,payload_sha256,
                    status,attempt,message_json,created_at
                ) VALUES (%s,%s,'learner_model_events',%s,%s,'PENDING',0,%s,%s)
                """,
                (
                    fence.tenant_id,
                    outbox_id,
                    f"learner-projection-failed:{event.event_id}",
                    failure_hash,
                    Jsonb(failure_wire),
                    failed_at,
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_learner_projection_failures(
                    tenant_id,failure_id,job_id,event_id,source_event_id,
                    learner_id,actor_id,content_hash,source_stream_id,
                    source_stream_sequence,attempt,fencing_token,classification,
                    error_code,error_json,error_sha256,failure_event_id,outbox_message_id,
                    recorded_at
                )
                SELECT tenant_id,%s,job_id,event_id,source_event_id,
                       learner_id,actor_id,content_hash,source_stream_id,
                       source_stream_sequence,attempt,fencing_token,'PERMANENT',
                       %s,%s,%s,%s,%s,%s
                FROM yaya_learner_projection_jobs
                WHERE tenant_id=%s AND job_id=%s
                """,
                (
                    _identifier("learner_failure", identity_seed),
                    error.code,
                    Jsonb(error_json),
                    error_sha256,
                    failure_event.event_id,
                    outbox_id,
                    failed_at,
                    fence.tenant_id,
                    fence.job_id,
                ),
            )
            finished = await connection.execute(
                """
                UPDATE yaya_learner_projection_jobs
                SET state='FAILED',worker_id=NULL,lease_id=NULL,
                    claimed_at=NULL,heartbeat_at=NULL,lease_expires_at=NULL,
                    last_error_code=%s,last_error_json=%s,
                    failed_at=%s,updated_at=clock_timestamp()
                WHERE tenant_id=%s AND job_id=%s AND state='LEASED'
                  AND worker_id=%s AND lease_id=%s AND fencing_token=%s
                  AND lease_expires_at>clock_timestamp()
                """,
                (
                    error.code,
                    Jsonb(error_json),
                    failed_at,
                    fence.tenant_id,
                    fence.job_id,
                    fence.worker_id,
                    fence.lease_id,
                    fence.fencing_token,
                ),
            )
            if finished.rowcount != 1:
                raise AssertionError("test failure projector lost its fencing token")
        if self.failure_graph_tamper is not None:
            async with self.database.transaction() as connection:
                if self.failure_graph_tamper == "failure_event":
                    await connection.execute(
                        """
                        UPDATE yaya_events SET event_json=%s
                        WHERE tenant_id=%s AND event_id=%s
                        """,
                        (Jsonb(encode(event)), fence.tenant_id, failure_event.event_id),
                    )
                elif self.failure_graph_tamper == "outbox_payload":
                    await connection.execute(
                        """
                        UPDATE yaya_outbox SET message_json='{}'::jsonb
                        WHERE tenant_id=%s AND message_id=%s
                        """,
                        (fence.tenant_id, outbox_id),
                    )
                elif self.failure_graph_tamper == "job_event_json":
                    tampered_payload = {
                        **event.payload,
                        "reason": "tampered after COMMIT",
                    }
                    tampered_payload["inference_sha256"] = learner_inference_sha256(
                        tampered_payload
                    )
                    tampered_source = replace(
                        event,
                        payload=tampered_payload,
                    )
                    await connection.execute(
                        """
                        UPDATE yaya_learner_projection_jobs SET event_json=%s
                        WHERE tenant_id=%s AND job_id=%s
                        """,
                        (Jsonb(encode(tampered_source)), fence.tenant_id, fence.job_id),
                    )
                elif self.failure_graph_tamper == "job_event_hash":
                    await connection.execute(
                        """
                        UPDATE yaya_learner_projection_jobs SET event_sha256=%s
                        WHERE tenant_id=%s AND job_id=%s
                        """,
                        ("f" * 64, fence.tenant_id, fence.job_id),
                    )
                else:
                    raise AssertionError("unknown failure graph tamper mode")
        if self.drop_failure_response_once:
            self.drop_failure_response_once = False
            raise ConnectionError("injected response loss after failure COMMIT")
        if self.failure_unknown_result_once:
            self.failure_unknown_result_once = False
            return Failure(_unknown_commit_error())
        return Success(None)


class _BackendInterruptingDatabase(PostgresDatabase):
    """Kill one real PostgreSQL backend immediately after it connects."""

    def __init__(self, dsn: str) -> None:
        super().__init__(dsn)
        self._interrupt_next_transaction = False
        self.interruptions = 0

    def interrupt_next_transaction(self) -> None:
        self._interrupt_next_transaction = True

    async def connect(
        self,
        *,
        autocommit: bool = False,
    ) -> AsyncConnection[dict[str, object]]:
        connection = await super().connect(autocommit=autocommit)
        if not self._interrupt_next_transaction or autocommit:
            return connection
        self._interrupt_next_transaction = False
        pid_cursor = await connection.execute("SELECT pg_backend_pid() AS pid")
        pid_row = await pid_cursor.fetchone()
        if pid_row is None:
            await connection.close()
            raise AssertionError("PostgreSQL did not return the reconciliation backend PID")
        killer = await super().connect(autocommit=True)
        try:
            killed_cursor = await killer.execute(
                "SELECT pg_terminate_backend(%s) AS killed",
                (pid_row["pid"],),
            )
            killed_row = await killed_cursor.fetchone()
        finally:
            await killer.close()
        if killed_row is None or not killed_row["killed"]:
            await connection.close()
            raise AssertionError("PostgreSQL refused to terminate the reconciliation backend")
        self.interruptions += 1
        try:
            await connection.execute("SELECT 1")
        finally:
            await connection.close()
        raise AssertionError("terminated PostgreSQL reconciliation backend remained usable")


class _CommitResponseDroppingDatabase(PostgresDatabase):
    """Raise only after a real PostgreSQL transaction has committed."""

    def __init__(self, dsn: str) -> None:
        super().__init__(dsn)
        self.drop_next_commit_response = False
        self.dropped_responses = 0

    @asynccontextmanager
    async def transaction(
        self,
    ) -> AsyncGenerator[AsyncConnection[dict[str, object]]]:
        async with super().transaction() as connection:
            yield connection
        if self.drop_next_commit_response:
            self.drop_next_commit_response = False
            self.dropped_responses += 1
            raise ConnectionError("injected response loss after real PostgreSQL COMMIT")


class _InterruptAfterTerminalProjector:
    """Arm a real backend interruption only after a terminal COMMIT returns."""

    def __init__(
        self,
        delegate: _AtomicFencedProjector,
        database: _BackendInterruptingDatabase,
    ) -> None:
        self._delegate = delegate
        self._database = database

    async def project_fenced(
        self,
        event: RuntimeEvent,
        expected_learner_revision: int,
        context: OperationContext,
        fence: LearnerProjectionFence,
    ) -> Result[LearnerUpdate]:
        result = await self._delegate.project_fenced(
            event,
            expected_learner_revision,
            context,
            fence,
        )
        if isinstance(result, Success):
            self._database.interrupt_next_transaction()
        return result

    async def fail_fenced(
        self,
        event: RuntimeEvent,
        error: ContractError,
        context: OperationContext,
        fence: LearnerProjectionFence,
    ) -> Result[None]:
        result = await self._delegate.fail_fenced(event, error, context, fence)
        if isinstance(result, Success):
            self._database.interrupt_next_transaction()
        return result


class LearnerProjectionWorkerPostgresTests(unittest.IsolatedAsyncioTestCase):
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
                TRUNCATE yaya_learner_projection_job_evidence,
                  yaya_learner_projection_receipts,yaya_learner_projection_failures,
                  yaya_learner_projection_jobs,yaya_learner_models,yaya_outbox,
                  yaya_events,yaya_agent_turns,yaya_evidence,yaya_command_jobs,
                  yaya_runs,yaya_commands,yaya_agent_sessions,yaya_worlds,yaya_tasks
                CASCADE
                """
            )
        finally:
            await connection.close()
        await self._seed_authority()
        self.projector = _AtomicFencedProjector(self.database)

    async def _seed_authority(self) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO yaya_tasks(
                    tenant_id,task_id,actor_id,content_hash,snapshot_json
                ) VALUES (%s,%s,%s,%s,'{}'::jsonb)
                """,
                (TENANT_ID, TASK_ID, LEARNER_ID, CONTENT_HASH),
            )
            await connection.execute(
                """
                INSERT INTO yaya_worlds(
                    tenant_id,world_id,actor_id,content_hash,stream_id,revision,
                    last_event_sequence,state_hash,world_rules_version,state_json,
                    request_context_json
                ) VALUES (%s,%s,%s,%s,%s,0,0,%s,'rules-1','{}'::jsonb,'{}'::jsonb)
                """,
                (
                    TENANT_ID,
                    WORLD_ID,
                    LEARNER_ID,
                    CONTENT_HASH,
                    f"world:{WORLD_ID}",
                    "b" * 64,
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_agent_sessions(
                    tenant_id,session_id,actor_id,task_id,world_id,
                    content_hash,snapshot_json
                ) VALUES (%s,%s,%s,%s,%s,%s,'{}'::jsonb)
                """,
                (TENANT_ID, SESSION_ID, LEARNER_ID, TASK_ID, WORLD_ID, CONTENT_HASH),
            )

    def _context(self, index: int, source_event_id: str) -> OperationContext:
        return OperationContext(
            request_id=f"req_projection_{index:08d}",
            correlation_id=f"corr_projection_{index:08d}",
            trace_id=f"trace_projection_{index:08d}",
            requested_at=NOW + timedelta(seconds=index),
            actor=ActorRef(
                tenant_id=TENANT_ID,
                actor_id=LEARNER_ID,
                actor_type=ActorType.STUDENT,
                roles=("game:player",),
            ),
            content_ref=ContentRef("YAYA_PROJECTION_001", "1.0.0", CONTENT_HASH),
            command_id=f"cmd_projection_{index:08d}",
            causation_id=source_event_id,
            deadline_at=NOW + timedelta(minutes=5, seconds=index),
        )

    async def _seed_job(
        self,
        sequence: int,
        *,
        concept: str = "loops.for",
    ) -> RuntimeEvent:
        source_event_id = f"evt_source_turn_{sequence:08d}"
        context = self._context(sequence, source_event_id)
        evidence = EvidenceRef(
            evidence_id=f"evidence_projection_{sequence:08d}",
            evidence_type=EvidenceType.TEST_REPORT,
            created_at=NOW + timedelta(seconds=sequence),
            sha256=hashlib.sha256(f"evidence:{sequence}".encode()).hexdigest(),
        )
        source_event_sha256 = hashlib.sha256(source_event_id.encode()).hexdigest()
        turn_record: dict[str, object] = {
            "committed": True,
            "source_event_id": source_event_id,
            "sequence": sequence,
        }
        turn_commit_sha256 = agent_turn_commit_sha256(turn_record)
        inference_payload: dict[str, object] = {
            "actor": plain(context.actor),
            "learner_id": LEARNER_ID,
            "session_id": SESSION_ID,
            "turn_id": f"turn_projection_{sequence:08d}",
            "command_id": context.command_id,
            "run_id": None,
            "source_event_id": source_event_id,
            "source_event_sha256": source_event_sha256,
            "turn_commit_sha256": turn_commit_sha256,
            "task_id": TASK_ID,
            "teaching_spec_version": "teaching-7",
            "role": "teaching_agent",
            "concept": concept,
            "score_delta": 0.2,
            "confidence": 0.875,
            "reason": "The committed turn contains verified test evidence.",
            "evidence_refs": [_evidence_wire(evidence)],
            "inferred_at": _iso(NOW + timedelta(seconds=sequence)),
        }
        inference_payload["inference_sha256"] = learner_inference_sha256(inference_payload)
        event = RuntimeEvent(
            event_id=f"evt_inference_{sequence:08d}",
            event_type=RuntimeEventType.LEARNER_INFERENCE_RECORDED,
            event_version=1,
            schema_version="2.0.0",
            stream_id=f"learner:{LEARNER_ID}",
            sequence=sequence,
            occurred_at=NOW + timedelta(seconds=sequence),
            producer="agent_hub",
            trace_id=context.trace_id,
            command_id=context.command_id,
            correlation_id=context.correlation_id,
            causation_id=source_event_id,
            content_ref=context.content_ref,
            payload=inference_payload,
        )
        event_json = cast(Mapping[str, object], encode(event))
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO yaya_commands(
                    tenant_id,actor_id,operation,idempotency_key,command_id,
                    session_id,turn_id,client_turn_sequence,request_sha256,
                    content_hash,revision,status,updated_at,record_json
                ) VALUES (%s,%s,'EXECUTE_AGENT_TURN',%s,%s,%s,%s,%s,%s,%s,
                          1,'APPLIED',clock_timestamp(),'{}'::jsonb)
                """,
                (
                    TENANT_ID,
                    LEARNER_ID,
                    f"idem_projection_{sequence:08d}",
                    context.command_id,
                    SESSION_ID,
                    event.payload["turn_id"],
                    sequence,
                    hashlib.sha256(f"request:{sequence}".encode()).hexdigest(),
                    CONTENT_HASH,
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_agent_turns(
                    tenant_id,event_id,actor_id,content_hash,event_sha256,
                    record_json,committed_at
                ) VALUES (%s,%s,%s,%s,%s,%s,clock_timestamp())
                """,
                (
                    TENANT_ID,
                    source_event_id,
                    LEARNER_ID,
                    CONTENT_HASH,
                    source_event_sha256,
                    Jsonb(turn_record),
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_evidence(
                    tenant_id,evidence_id,actor_id,content_hash,evidence_type,
                    payload_sha256,evidence_json
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    TENANT_ID,
                    evidence.evidence_id,
                    LEARNER_ID,
                    CONTENT_HASH,
                    evidence.evidence_type,
                    evidence.sha256,
                    Jsonb(_evidence_wire(evidence)),
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_events(
                    tenant_id,event_id,stream_id,sequence,event_type,event_json,occurred_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    TENANT_ID,
                    event.event_id,
                    event.stream_id,
                    event.sequence,
                    event.event_type,
                    Jsonb(event_json),
                    event.occurred_at,
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_learner_projection_jobs(
                    tenant_id,job_id,event_id,source_event_id,learner_id,actor_id,
                    content_hash,task_id,session_id,turn_id,command_id,run_id,
                    source_stream_id,source_stream_sequence,event_sha256,
                    source_event_sha256,turn_commit_sha256,inference_sha256,
                    teaching_spec_version,role,event_json,operation_context_json
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s,
                          %s,%s,%s,%s,%s,%s)
                """,
                (
                    TENANT_ID,
                    f"learner_job_{sequence:08d}",
                    event.event_id,
                    source_event_id,
                    LEARNER_ID,
                    LEARNER_ID,
                    CONTENT_HASH,
                    TASK_ID,
                    SESSION_ID,
                    event.payload["turn_id"],
                    context.command_id,
                    event.stream_id,
                    event.sequence,
                    internal_record_sha256(event_json),
                    source_event_sha256,
                    turn_commit_sha256,
                    event.payload["inference_sha256"],
                    event.payload["teaching_spec_version"],
                    event.payload["role"],
                    Jsonb(event_json),
                    Jsonb(encode(context)),
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_learner_projection_job_evidence(
                    tenant_id,job_id,event_id,source_event_id,learner_id,actor_id,
                    content_hash,source_stream_id,source_stream_sequence,ordinal,
                    evidence_id,evidence_sha256
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s)
                """,
                (
                    TENANT_ID,
                    f"learner_job_{sequence:08d}",
                    event.event_id,
                    source_event_id,
                    LEARNER_ID,
                    LEARNER_ID,
                    CONTENT_HASH,
                    event.stream_id,
                    event.sequence,
                    evidence.evidence_id,
                    evidence.sha256,
                ),
            )
        return event

    def _worker(self, worker_id: str, *, retry_delay: float = 0.01) -> LearnerProjectionWorker:
        return LearnerProjectionWorker(
            database=self.database,
            learner=self.projector,
            worker_id=worker_id,
            lease_seconds=2,
            poll_ms=10,
            retry_delay_seconds=retry_delay,
        )

    async def _job(self, sequence: int) -> Mapping[str, object]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT * FROM yaya_learner_projection_jobs
                WHERE tenant_id=%s AND job_id=%s
                """,
                (TENANT_ID, f"learner_job_{sequence:08d}"),
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            raise AssertionError("learner projection fixture Job disappeared")
        return row

    async def _terminal_audit(self, sequence: int) -> Mapping[str, object]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT * FROM yaya_learner_projection_terminal_audits
                WHERE tenant_id=%s AND job_id=%s
                """,
                (TENANT_ID, f"learner_job_{sequence:08d}"),
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            raise AssertionError("terminal learner projection audit disappeared")
        return row

    async def test_success_is_atomic_and_replay_does_not_advance_again(self) -> None:
        await self._seed_job(1)
        worker = self._worker("learner_worker_success")
        self.assertTrue(await worker.run_once())
        job = await self._job(1)
        self.assertEqual(job["state"], "SUCCEEDED")
        self.assertEqual(self.projector.project_calls, 1)
        self.assertFalse(await worker.run_once())
        self.assertEqual(self.projector.project_calls, 1)
        async with self.database.transaction() as connection:
            counts: dict[str, int] = {}
            for table in (
                "yaya_learner_models",
                "yaya_learner_projection_receipts",
                "yaya_outbox",
            ):
                cursor = await connection.execute(f"SELECT count(*) AS count FROM {table}")
                row = await cursor.fetchone()
                counts[table] = 0 if row is None else cast(int, row["count"])
        self.assertEqual(
            counts,
            {
                "yaya_learner_models": 1,
                "yaya_learner_projection_receipts": 1,
                "yaya_outbox": 1,
            },
        )

    async def test_sequence_gap_pauses_until_prior_source_is_projected(self) -> None:
        await self._seed_job(2)
        worker = self._worker("learner_worker_gap")
        self.assertIsNone(await worker.claim_one())
        await self._seed_job(1)
        self.assertTrue(await worker.run_once())
        next_lease = await worker.claim_one()
        self.assertIsNotNone(next_lease)
        if next_lease is None:
            self.fail("sequence two was not claimable after sequence one")
        self.assertEqual(next_lease.source_stream_sequence, 2)
        await worker._release_for_retry(  # pyright: ignore[reportPrivateUsage]
            next_lease,
            "DEPENDENCY_UNAVAILABLE",
            {"code": "DEPENDENCY_UNAVAILABLE", "redacted": True},
        )

    async def test_expired_lease_takeover_fences_every_old_worker_write(self) -> None:
        await self._seed_job(1)
        old_worker = self._worker("learner_worker_old")
        old_lease = await old_worker.claim_one()
        self.assertIsNotNone(old_lease)
        if old_lease is None:
            self.fail("first learner worker did not claim the Job")
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                UPDATE yaya_learner_projection_jobs
                SET heartbeat_at=clock_timestamp()-interval '2 seconds',
                    lease_expires_at=clock_timestamp()-interval '1 second'
                WHERE tenant_id=%s AND job_id=%s
                """,
                (old_lease.tenant_id, old_lease.job_id),
            )
        new_worker = self._worker("learner_worker_new")
        new_lease = await new_worker.claim_one()
        self.assertIsNotNone(new_lease)
        if new_lease is None:
            self.fail("replacement learner worker did not take over expired Job")
        self.assertNotEqual(old_lease.fence.lease_id, new_lease.fence.lease_id)
        self.assertEqual(new_lease.fence.fencing_token, old_lease.fence.fencing_token + 1)
        with self.assertRaises(LearnerProjectionFenceLost):
            await old_worker._release_for_retry(  # pyright: ignore[reportPrivateUsage]
                old_lease,
                "DEPENDENCY_UNAVAILABLE",
                {"code": "DEPENDENCY_UNAVAILABLE", "redacted": True},
            )
        await new_worker._process(new_lease)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((await self._job(1))["state"], "SUCCEEDED")

    async def test_retryable_and_permanent_failures_have_distinct_terminal_semantics(self) -> None:
        await self._seed_job(1)
        worker = self._worker("learner_worker_failures")
        self.projector.mode = "retry"
        self.assertTrue(await worker.run_once())
        retry_job = await self._job(1)
        self.assertEqual(retry_job["state"], "READY")
        self.assertEqual(retry_job["last_error_code"], "DEPENDENCY_UNAVAILABLE")
        self.projector.mode = "success"
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                UPDATE yaya_learner_projection_jobs SET available_at=clock_timestamp()
                WHERE tenant_id=%s AND job_id=%s
                """,
                (TENANT_ID, "learner_job_00000001"),
            )
        self.assertTrue(await worker.run_once())
        await self._seed_job(2)
        self.projector.mode = "permanent"
        self.assertTrue(await worker.run_once())
        permanent_job = await self._job(2)
        self.assertEqual(permanent_job["state"], "FAILED")
        self.assertEqual(permanent_job["last_error_code"], "AUTHORIZATION_DENIED")
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                SELECT classification,failure_event_id,outbox_message_id
                FROM yaya_learner_projection_failures
                WHERE tenant_id=%s AND job_id=%s AND classification='PERMANENT'
                """,
                (TENANT_ID, "learner_job_00000002"),
            )
            failure = await cursor.fetchone()
        self.assertIsNotNone(failure)
        if failure is not None:
            self.assertIsNotNone(failure["failure_event_id"])
            self.assertIsNotNone(failure["outbox_message_id"])

    async def test_response_loss_reconciles_without_duplicate_projection(self) -> None:
        await self._seed_job(1)
        self.projector.drop_response_once = True
        worker = self._worker("learner_worker_response_loss")
        self.assertTrue(await worker.run_once())
        self.assertEqual((await self._job(1))["state"], "SUCCEEDED")
        self.assertFalse(await worker.run_once())
        self.assertEqual(self.projector.project_calls, 1)

    async def test_restart_audits_success_after_real_postgres_reconciliation_interruption(
        self,
    ) -> None:
        await self._seed_job(1)
        interrupting_database = _BackendInterruptingDatabase(self.server.dsn)
        interrupting_projector = _InterruptAfterTerminalProjector(
            self.projector,
            interrupting_database,
        )
        interrupted_worker = LearnerProjectionWorker(
            database=interrupting_database,
            learner=interrupting_projector,
            worker_id="learner_worker_success_pg_interruption",
            lease_seconds=2,
            poll_ms=10,
            retry_delay_seconds=0.01,
        )

        self.assertTrue(await interrupted_worker.run_once())
        job = await self._job(1)
        audit = await self._terminal_audit(1)
        self.assertEqual(job["state"], "SUCCEEDED")
        self.assertEqual(audit["terminal_state"], "SUCCEEDED")
        self.assertEqual(audit["terminal_kind"], "SUCCESS")
        self.assertEqual(audit["attempt"], job["attempt"])
        self.assertEqual(audit["fencing_token"], job["fencing_token"])
        self.assertIsNone(audit["verified_at"])
        self.assertEqual(interrupting_database.interruptions, 1)
        self.assertEqual(self.projector.project_calls, 1)

        restarted_worker = self._worker("learner_worker_success_pg_restart")
        self.assertTrue(await restarted_worker.run_once())
        verified = await self._terminal_audit(1)
        self.assertIsNotNone(verified["verified_at"])
        self.assertEqual(verified["verified_by"], "learner_worker_success_pg_restart")
        self.assertEqual(self.projector.project_calls, 1)
        self.assertFalse(await restarted_worker.run_once())

    async def test_restart_audits_failure_after_real_postgres_reconciliation_interruption(
        self,
    ) -> None:
        await self._seed_job(1)
        self.projector.mode = "permanent"
        interrupting_database = _BackendInterruptingDatabase(self.server.dsn)
        interrupting_projector = _InterruptAfterTerminalProjector(
            self.projector,
            interrupting_database,
        )
        interrupted_worker = LearnerProjectionWorker(
            database=interrupting_database,
            learner=interrupting_projector,
            worker_id="learner_worker_failure_pg_interruption",
            lease_seconds=2,
            poll_ms=10,
            retry_delay_seconds=0.01,
        )

        self.assertTrue(await interrupted_worker.run_once())
        job = await self._job(1)
        audit = await self._terminal_audit(1)
        self.assertEqual(job["state"], "FAILED")
        self.assertEqual(audit["terminal_state"], "FAILED")
        self.assertEqual(audit["terminal_kind"], "PERMANENT_FAILURE")
        self.assertEqual(audit["attempt"], job["attempt"])
        self.assertEqual(audit["fencing_token"], job["fencing_token"])
        self.assertIsNone(audit["verified_at"])
        self.assertEqual(interrupting_database.interruptions, 1)
        self.assertEqual(self.projector.project_calls, 1)
        self.assertEqual(self.projector.fail_calls, 1)

        restarted_worker = self._worker("learner_worker_failure_pg_restart")
        self.assertTrue(await restarted_worker.run_once())
        verified = await self._terminal_audit(1)
        self.assertIsNotNone(verified["verified_at"])
        self.assertEqual(verified["verified_by"], "learner_worker_failure_pg_restart")
        self.assertEqual(self.projector.project_calls, 1)
        self.assertEqual(self.projector.fail_calls, 1)
        self.assertFalse(await restarted_worker.run_once())

    async def test_restart_audits_quarantine_after_commit_response_loss(self) -> None:
        await self._seed_job(1)
        dropping_database = _CommitResponseDroppingDatabase(self.server.dsn)
        interrupted_worker = LearnerProjectionWorker(
            database=dropping_database,
            learner=self.projector,
            worker_id="learner_worker_quarantine_commit_loss",
            lease_seconds=2,
            poll_ms=10,
            retry_delay_seconds=0.01,
        )
        lease = await interrupted_worker.claim_one()
        self.assertIsNotNone(lease)
        if lease is None:
            self.fail("quarantine response-loss Job was not claimable")
        quarantine_error = {
            "code": "INVARIANT_VIOLATION",
            "cause": "InjectedQuarantineCommitResponseLoss",
            "redacted": True,
        }
        dropping_database.drop_next_commit_response = True
        with self.assertRaises(ConnectionError):
            await interrupted_worker._quarantine(  # pyright: ignore[reportPrivateUsage]
                lease,
                "INVARIANT_VIOLATION",
                quarantine_error,
            )

        job = await self._job(1)
        audit = await self._terminal_audit(1)
        self.assertEqual(job["state"], "FAILED")
        self.assertEqual(audit["terminal_kind"], "QUARANTINE")
        self.assertIsNone(audit["verified_at"])
        self.assertEqual(dropping_database.dropped_responses, 1)

        restarted_worker = self._worker("learner_worker_quarantine_commit_restart")
        self.assertTrue(await restarted_worker.run_once())
        verified = await self._terminal_audit(1)
        self.assertIsNotNone(verified["verified_at"])
        self.assertEqual(
            verified["verified_by"],
            "learner_worker_quarantine_commit_restart",
        )
        self.assertEqual(self.projector.project_calls, 0)
        self.assertEqual(self.projector.fail_calls, 0)
        self.assertFalse(await restarted_worker.run_once())

    async def test_response_loss_reconciles_after_receiptless_rebuild_head(self) -> None:
        first_event = await self._seed_job(1, concept="loops.for")
        second_event = await self._seed_job(2, concept="loops.while")
        first_worker = self._worker("learner_worker_response_loss_before_rebuild")
        first_lease = await first_worker.claim_one()
        self.assertIsNotNone(first_lease)
        if first_lease is None:
            self.fail("sequence-one response-loss Job was not claimable")

        self.projector.drop_response_once = True
        with self.assertRaises(ConnectionError):
            await self.projector.project_fenced(
                first_event,
                first_lease.expected_learner_revision,
                self._context(1, cast(str, first_event.causation_id)),
                first_lease.fence,
            )
        self.assertEqual((await self._job(1))["state"], "SUCCEEDED")

        self.projector.mode = "permanent"
        second_worker = self._worker("learner_worker_terminal_before_rebuild")
        self.assertIsNone(await second_worker.claim_one())
        self.assertTrue(await second_worker.run_once())
        self.assertIsNotNone((await self._terminal_audit(1))["verified_at"])
        self.assertTrue(await second_worker.run_once())
        self.assertEqual((await self._job(2))["state"], "FAILED")

        evidence: list[EvidenceRef] = []
        for event in (first_event, second_event):
            raw_evidence = cast(list[Mapping[str, object]], event.payload["evidence_refs"])
            item = raw_evidence[0]
            evidence.append(
                EvidenceRef(
                    evidence_id=cast(str, item["evidence_id"]),
                    evidence_type=EvidenceType(cast(str, item["evidence_type"])),
                    created_at=datetime.fromisoformat(
                        cast(str, item["created_at"]).replace("Z", "+00:00")
                    ),
                    sha256=cast(str, item["sha256"]),
                )
            )
        rebuilt = LearnerModelSnapshot(
            learner_id=LEARNER_ID,
            revision=2,
            model_version="learner-policy-v1",
            projected_through_sequence=2,
            competencies={
                "loops.while": cast(
                    FrozenJsonValue,
                    plain(
                        CompetencyProjection(
                            concept="loops.while",
                            evidence_stage=EvidenceStage.OBSERVED,
                            assistance_level=0,
                            last_observed_at=second_event.occurred_at,
                            next_review_at=second_event.occurred_at + timedelta(days=1),
                            evidence_ids=tuple(item.evidence_id for item in evidence),
                        )
                    ),
                )
            },
            updated_at=second_event.occurred_at,
            evidence_refs=tuple(evidence),
        )
        rebuild_context = self._context(2, cast(str, second_event.causation_id))
        rebuild_context = replace(
            rebuild_context,
            actor=replace(
                rebuild_context.actor,
                roles=("game:player", "classroom:mentor"),
            ),
        )
        rebuild_context = replace(
            rebuild_context,
            actor=replace(
                rebuild_context.actor,
                roles=("game:player", "learner:read"),
            ),
        )
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                UPDATE yaya_learner_models
                SET revision=%s,projected_through_sequence=%s,snapshot_json=%s,
                    snapshot_sha256=%s,updated_at=%s,request_context_json=%s,
                    projection_policy_version=%s
                WHERE tenant_id=%s AND learner_id=%s
                """,
                (
                    rebuilt.revision,
                    rebuilt.projected_through_sequence,
                    Jsonb(encode(rebuilt)),
                    internal_record_sha256(rebuilt),
                    rebuilt.updated_at,
                    Jsonb(encode(rebuild_context)),
                    rebuilt.model_version,
                    TENANT_ID,
                    LEARNER_ID,
                ),
            )
            await connection.execute(
                """
                UPDATE yaya_learner_projection_failures
                SET resolved_at=clock_timestamp(),resolution='REBUILT'
                WHERE tenant_id=%s AND job_id=%s AND resolved_at IS NULL
                """,
                (TENANT_ID, "learner_job_00000002"),
            )
            receipt_cursor = await connection.execute(
                """
                SELECT count(*) AS count
                FROM yaya_learner_projection_receipts
                WHERE tenant_id=%s AND learner_id=%s
                """,
                (TENANT_ID, LEARNER_ID),
            )
            receipt_row = await receipt_cursor.fetchone()
        self.assertIsNotNone(receipt_row)
        if receipt_row is not None:
            self.assertEqual(receipt_row["count"], 1)

        reconciliation = await first_worker._success_graph_state(  # pyright: ignore[reportPrivateUsage]
            first_lease,
            None,
        )
        self.assertEqual(reconciliation.value, "MATCH")

    async def test_failure_response_loss_reconciles_complete_graph(self) -> None:
        await self._seed_job(1)
        self.projector.mode = "permanent"
        self.projector.drop_failure_response_once = True
        worker = self._worker("learner_worker_failure_response_loss")
        self.assertTrue(await worker.run_once())
        job = await self._job(1)
        self.assertEqual(job["state"], "FAILED")
        self.assertEqual(job["last_error_code"], "AUTHORIZATION_DENIED")
        self.assertEqual(self.projector.fail_calls, 1)
        self.assertFalse(await worker.run_once())
        self.assertEqual(self.projector.fail_calls, 1)

    async def test_failure_unknown_commit_result_reconciles_complete_graph(self) -> None:
        await self._seed_job(1)
        self.projector.mode = "permanent"
        self.projector.failure_unknown_result_once = True
        worker = self._worker("learner_worker_failure_unknown_result")
        self.assertTrue(await worker.run_once())
        job = await self._job(1)
        self.assertEqual(job["state"], "FAILED")
        self.assertEqual(job["last_error_code"], "AUTHORIZATION_DENIED")
        self.assertEqual(self.projector.fail_calls, 1)
        self.assertFalse(await worker.run_once())

    async def test_historical_success_reconciles_after_compaction_removes_changed_id(
        self,
    ) -> None:
        first_event = await self._seed_job(1, concept="loops.for")
        await self._seed_job(2, concept="loops.while")
        first_worker = self._worker("learner_worker_historical_first")
        first_lease = await first_worker.claim_one()
        self.assertIsNotNone(first_lease)
        if first_lease is None:
            self.fail("first historical projection Job was not claimable")
        first_context = self._context(1, "evt_source_turn_00000001")
        first_result = await self.projector.project_fenced(
            first_event,
            first_lease.expected_learner_revision,
            first_context,
            first_lease.fence,
        )
        self.assertIsInstance(first_result, Success)
        if not isinstance(first_result, Success):
            self.fail("first historical projection did not commit")

        second_worker = self._worker("learner_worker_historical_second")
        self.assertTrue(await second_worker.run_once())
        self.assertIsNotNone((await self._terminal_audit(1))["verified_at"])
        self.assertTrue(await second_worker.run_once())
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                SELECT snapshot_json FROM yaya_learner_models
                WHERE tenant_id=%s AND learner_id=%s
                """,
                (TENANT_ID, LEARNER_ID),
            )
            row = await cursor.fetchone()
        self.assertIsNotNone(row)
        if row is not None:
            compacted = decode_as(row["snapshot_json"], LearnerModelSnapshot)
            self.assertNotIn("loops.for", compacted.competencies)
            self.assertIn("loops.while", compacted.competencies)
        reconciliation = await first_worker._success_graph_state(  # pyright: ignore[reportPrivateUsage]
            first_lease,
            first_result.value,
        )
        self.assertEqual(reconciliation.value, "MATCH")

    async def _assert_success_graph_tamper_is_fatal(self, mode: str) -> None:
        await self._seed_job(1)
        self.projector.success_graph_tamper = mode
        if mode.startswith("job_event_"):
            self.projector.drop_response_once = True
        worker = self._worker(f"learner_worker_success_tamper_{mode}")
        # Terminal COMMIT can race the heartbeat: the first pass may audit now
        # or leave the durable audit pending.  The next pass must reject it,
        # and the projector must never be called again in either schedule.
        try:
            await worker.run_once()
        except LearnerProjectionDurableGraphCorrupt:
            pass
        job = await self._job(1)
        self.assertEqual(job["state"], "SUCCEEDED")
        self.assertEqual(job["attempt"], 1)
        self.assertEqual(self.projector.project_calls, 1)
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                SELECT count(*) AS count FROM yaya_learner_projection_failures
                WHERE tenant_id=%s AND job_id=%s
                """,
                (TENANT_ID, "learner_job_00000001"),
            )
            row = await cursor.fetchone()
        self.assertIsNotNone(row)
        if row is not None:
            self.assertEqual(row["count"], 0)
        with self.assertRaises(LearnerProjectionDurableGraphCorrupt):
            await worker.run_once()
        self.assertEqual(self.projector.project_calls, 1)

    async def test_tampered_success_receipt_hash_is_fatal_not_retried(self) -> None:
        await self._assert_success_graph_tamper_is_fatal("receipt_hash")

    async def test_tampered_success_model_snapshot_is_fatal_not_retried(self) -> None:
        await self._assert_success_graph_tamper_is_fatal("model_snapshot")

    async def test_tampered_success_derived_event_is_fatal_not_retried(self) -> None:
        await self._assert_success_graph_tamper_is_fatal("derived_event")

    async def test_tampered_success_outbox_payload_is_fatal_not_retried(self) -> None:
        await self._assert_success_graph_tamper_is_fatal("outbox_payload")

    async def test_tampered_success_job_event_json_is_fatal_after_response_loss(self) -> None:
        await self._assert_success_graph_tamper_is_fatal("job_event_json")

    async def test_tampered_success_job_event_hash_is_fatal_after_response_loss(self) -> None:
        await self._assert_success_graph_tamper_is_fatal("job_event_hash")

    async def _assert_failure_graph_tamper_is_fatal(self, mode: str) -> None:
        await self._seed_job(1)
        self.projector.mode = "permanent"
        self.projector.failure_graph_tamper = mode
        if mode.startswith("job_event_"):
            self.projector.drop_failure_response_once = True
        worker = self._worker(f"learner_worker_failure_tamper_{mode}")
        # As above, a terminal heartbeat race may defer (but never bypass) the
        # mandatory durable-graph audit until the next worker pass.
        try:
            await worker.run_once()
        except LearnerProjectionDurableGraphCorrupt:
            pass
        job = await self._job(1)
        self.assertEqual(job["state"], "FAILED")
        self.assertEqual(job["last_error_code"], "AUTHORIZATION_DENIED")
        self.assertEqual(self.projector.fail_calls, 1)
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                SELECT classification,error_code
                FROM yaya_learner_projection_failures
                WHERE tenant_id=%s AND job_id=%s
                """,
                (TENANT_ID, "learner_job_00000001"),
            )
            rows = list(await cursor.fetchall())
        self.assertEqual(
            [(row["classification"], row["error_code"]) for row in rows],
            [("PERMANENT", "AUTHORIZATION_DENIED")],
        )
        with self.assertRaises(LearnerProjectionDurableGraphCorrupt):
            await worker.run_once()
        self.assertEqual(self.projector.fail_calls, 1)

    async def test_tampered_failure_event_is_fatal_not_retried(self) -> None:
        await self._assert_failure_graph_tamper_is_fatal("failure_event")

    async def test_tampered_failure_outbox_payload_is_fatal_not_retried(self) -> None:
        await self._assert_failure_graph_tamper_is_fatal("outbox_payload")

    async def test_tampered_failure_job_event_json_is_fatal_after_response_loss(self) -> None:
        await self._assert_failure_graph_tamper_is_fatal("job_event_json")

    async def test_tampered_failure_job_event_hash_is_fatal_after_response_loss(self) -> None:
        await self._assert_failure_graph_tamper_is_fatal("job_event_hash")

    async def test_stale_claim_cannot_record_graph_corruption_after_takeover(self) -> None:
        await self._seed_job(1)
        old_worker = self._worker("learner_worker_corruption_old")
        old_lease = await old_worker.claim_one()
        self.assertIsNotNone(old_lease)
        if old_lease is None:
            self.fail("old worker did not claim corruption test Job")
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                UPDATE yaya_learner_projection_jobs
                SET heartbeat_at=clock_timestamp()-interval '2 seconds',
                    lease_expires_at=clock_timestamp()-interval '1 second'
                WHERE tenant_id=%s AND job_id=%s
                """,
                (old_lease.tenant_id, old_lease.job_id),
            )
        new_worker = self._worker("learner_worker_corruption_new")
        new_lease = await new_worker.claim_one()
        self.assertIsNotNone(new_lease)
        if new_lease is None:
            self.fail("new worker did not take over corruption test Job")
        before = await self._job(1)
        with self.assertRaises(LearnerProjectionFenceLost):
            await old_worker._record_graph_corruption(  # pyright: ignore[reportPrivateUsage]
                old_lease,
                "STALE_WORKER_MUST_NOT_WRITE",
            )
        after = await self._job(1)
        for key in ("state", "attempt", "fencing_token", "worker_id", "lease_id"):
            self.assertEqual(after[key], before[key])
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                SELECT count(*) AS count FROM yaya_learner_projection_failures
                WHERE tenant_id=%s AND job_id=%s
                """,
                (TENANT_ID, old_lease.job_id),
            )
            row = await cursor.fetchone()
        self.assertIsNotNone(row)
        if row is not None:
            self.assertEqual(row["count"], 0)
        await new_worker._process(new_lease)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((await self._job(1))["state"], "SUCCEEDED")

    async def test_model_cas_change_after_claim_is_retried_not_quarantined(self) -> None:
        await self._seed_job(1)
        worker = self._worker("learner_worker_cas_reread")
        self.assertTrue(await worker.run_once())
        await self._seed_job(2)
        lease = await worker.claim_one()
        self.assertIsNotNone(lease)
        if lease is None:
            self.fail("sequence two was not claimed for the CAS-race test")

        # A separate connection performs the equivalent of an administrative
        # rebuild to checkpoint zero after claim.  The worker must let the
        # fenced projector reread/CAS and classify this as retryable drift.
        rebuilt = LearnerModelSnapshot(
            learner_id=LEARNER_ID,
            revision=0,
            model_version="learner-policy-v1",
            projected_through_sequence=0,
            competencies={},
            updated_at=NOW,
            evidence_refs=(),
        )
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                UPDATE yaya_learner_models
                SET revision=0,projected_through_sequence=0,snapshot_json=%s,
                    snapshot_sha256=%s,updated_at=%s
                WHERE tenant_id=%s AND learner_id=%s
                """,
                (
                    Jsonb(encode(rebuilt)),
                    internal_record_sha256(rebuilt),
                    NOW,
                    TENANT_ID,
                    LEARNER_ID,
                ),
            )

        with self.assertRaises(LearnerProjectionWorkerError) as raised:
            await worker._process(lease)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(raised.exception.code, "EVENT_SEQUENCE_GAP")
        self.assertTrue(raised.exception.retryable)
        await worker._release_for_retry(  # pyright: ignore[reportPrivateUsage]
            lease,
            raised.exception.code,
            {"code": raised.exception.code, "redacted": True},
        )
        self.assertEqual((await self._job(2))["state"], "READY")
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                SELECT classification FROM yaya_learner_projection_failures
                WHERE tenant_id=%s AND job_id=%s ORDER BY recorded_at
                """,
                (TENANT_ID, lease.job_id),
            )
            classifications = [row["classification"] for row in await cursor.fetchall()]
        self.assertEqual(classifications, ["RETRYABLE"])

    async def test_live_fence_quarantines_model_advance_without_success_graph(self) -> None:
        event = await self._seed_job(1)
        worker = self._worker("learner_worker_missing_success_graph")
        lease = await worker.claim_one()
        self.assertIsNotNone(lease)
        if lease is None:
            self.fail("missing-graph projection Job was not claimed")
        drifted = LearnerModelSnapshot(
            learner_id=LEARNER_ID,
            revision=1,
            model_version="learner-policy-v1",
            projected_through_sequence=1,
            competencies={},
            updated_at=event.occurred_at,
            evidence_refs=(),
        )
        context = self._context(1, "evt_source_turn_00000001")
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO yaya_learner_models(
                    tenant_id,learner_id,actor_id,content_hash,revision,
                    projected_through_sequence,snapshot_json,snapshot_sha256,updated_at,
                    request_context_json,projection_policy_version
                ) VALUES (%s,%s,%s,%s,1,1,%s,%s,%s,%s,'learner-policy-v1')
                """,
                (
                    TENANT_ID,
                    LEARNER_ID,
                    LEARNER_ID,
                    CONTENT_HASH,
                    Jsonb(encode(drifted)),
                    internal_record_sha256(drifted),
                    drifted.updated_at,
                    Jsonb(encode(context)),
                ),
            )
        await worker._process(lease)  # pyright: ignore[reportPrivateUsage]
        job = await self._job(1)
        self.assertEqual(job["state"], "FAILED")
        self.assertEqual(job["last_error_code"], "INVARIANT_VIOLATION")
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                SELECT classification,error_code
                FROM yaya_learner_projection_failures
                WHERE tenant_id=%s AND job_id=%s
                """,
                (TENANT_ID, lease.job_id),
            )
            failure = await cursor.fetchone()
        self.assertIsNotNone(failure)
        if failure is not None:
            self.assertEqual(failure["classification"], "QUARANTINED")
            self.assertEqual(failure["error_code"], "INVARIANT_VIOLATION")

    async def test_source_turn_authority_drift_is_quarantined_before_projection(self) -> None:
        await self._seed_job(1)
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                UPDATE yaya_agent_turns SET actor_id='student_other_0001'
                WHERE tenant_id=%s AND event_id=%s
                """,
                (TENANT_ID, "evt_source_turn_00000001"),
            )
        worker = self._worker("learner_worker_authority")
        self.assertTrue(await worker.run_once())
        self.assertEqual((await self._job(1))["state"], "FAILED")
        self.assertEqual(self.projector.project_calls, 0)
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                SELECT classification,error_code
                FROM yaya_learner_projection_failures
                WHERE tenant_id=%s AND job_id=%s
                """,
                (TENANT_ID, "learner_job_00000001"),
            )
            failure = await cursor.fetchone()
        self.assertIsNotNone(failure)
        if failure is not None:
            self.assertEqual(failure["classification"], "QUARANTINED")
            self.assertEqual(failure["error_code"], "INVARIANT_VIOLATION")

    async def test_run_forever_stops_gracefully_while_idle(self) -> None:
        worker = self._worker("learner_worker_stop")
        stop = asyncio.Event()
        running = asyncio.create_task(worker.run_forever(stop))
        await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(running, timeout=1)


if __name__ == "__main__":
    unittest.main()
