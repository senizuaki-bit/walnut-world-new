"""Read-only authority closure for Run-derived production Agent events.

The accepted HTTP event is deliberately only ``run_skill_requested``.  After
the internal xiaohutao execution receipt is durable, this module proves the
complete SkillInvocation/Run/Evidence/World graph under the live Command job
fence and derives the one deterministic teaching outcome event.  It never
writes and never calls a provider or Sandbox.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import cast

import psycopg
from psycopg import AsyncConnection
from yaya_agent_contracts import (
    CommandRecord,
    CommandStatus,
    ContentRef,
    EvidenceRef,
    EvidenceType,
    OperationContext,
    RequestContext,
    RuntimeEvent,
    RuntimeEventType,
    canonical_json_sha256,
)
from yaya_agent_runtime import (
    CommittedAgentTurn,
    GameEvent,
    RunResultSnapshot,
    SessionSnapshot,
    SkillInvocationResult,
    TaskSnapshot,
    derive_run_outcome_event,
    side_effect_execution_id,
)
from yaya_agent_runtime.errors import AgentPersistenceError

from .codec import decode_as, plain
from .database import PostgresDatabase
from .wire import ContractSchemaValidator
from .world_uow import world_commit_identifier

type _Connection = AsyncConnection[dict[str, object]]

_NON_TERMINAL = frozenset(
    {
        CommandStatus.ACCEPTED,
        CommandStatus.VALIDATING,
        CommandStatus.RUNNING_SANDBOX,
        CommandStatus.APPLYING_WORLD,
    }
)


def _invariant(message: str, **details: object) -> AgentPersistenceError:
    return AgentPersistenceError(
        "AGENT_OUTCOME_INVARIANT_VIOLATION",
        message,
        details,
    )


def _same_actor(left: object, right: object) -> bool:
    return (
        getattr(left, "tenant_id", None),
        getattr(left, "actor_id", None),
        getattr(left, "actor_type", None),
    ) == (
        getattr(right, "tenant_id", None),
        getattr(right, "actor_id", None),
        getattr(right, "actor_type", None),
    )


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise _invariant(f"{label} is not a JSON object")
    source = cast(Mapping[object, object], value)
    if any(not isinstance(key, str) for key in source):
        raise _invariant(f"{label} contains a non-string key")
    return {cast(str, key): item for key, item in source.items()}


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _context_wire(context: OperationContext | RequestContext) -> dict[str, object]:
    return {
        "schema_version": context.schema_version,
        "request_id": context.request_id,
        "correlation_id": context.correlation_id,
        "trace_id": context.trace_id,
        "requested_at": _iso(context.requested_at),
        "actor": {
            "tenant_id": context.actor.tenant_id,
            "actor_id": context.actor.actor_id,
            "actor_type": context.actor.actor_type.value,
            "roles": list(context.actor.roles),
        },
        "content_ref": {
            "unit_id": context.content_ref.unit_id,
            "version": context.content_ref.version,
            "content_hash": context.content_ref.content_hash,
        },
    }


def _evidence_ref_wire(reference: EvidenceRef) -> dict[str, object]:
    value: dict[str, object] = {
        "evidence_id": reference.evidence_id,
        "evidence_type": reference.evidence_type.value,
        "created_at": _iso(reference.created_at),
    }
    if reference.sha256 is not None:
        value["sha256"] = reference.sha256
    if reference.uri is not None:
        value["uri"] = reference.uri
    return value


def _plain_runtime_event(value: object, label: str) -> RuntimeEvent:
    wire = _mapping(value, label)
    content_ref = _mapping(wire.get("content_ref"), f"{label}.content_ref")
    payload = _mapping(wire.get("payload"), f"{label}.payload")
    occurred_at_wire = wire.get("occurred_at")
    if not isinstance(occurred_at_wire, str):
        raise _invariant(f"{label}.occurred_at is not a timestamp")
    occurred_at = datetime.fromisoformat(occurred_at_wire.replace("Z", "+00:00"))
    if occurred_at.tzinfo is None:
        raise _invariant(f"{label}.occurred_at has no timezone")
    try:
        event = RuntimeEvent(
            event_id=cast(str, wire.get("event_id")),
            event_type=RuntimeEventType(cast(str, wire.get("event_type"))),
            event_version=cast(int, wire.get("event_version")),
            stream_id=cast(str, wire.get("stream_id")),
            sequence=cast(int, wire.get("sequence")),
            occurred_at=occurred_at,
            producer=cast(str, wire.get("producer")),
            trace_id=cast(str, wire.get("trace_id")),
            command_id=cast(str, wire.get("command_id")),
            correlation_id=cast(str, wire.get("correlation_id")),
            causation_id=cast(str | None, wire.get("causation_id")),
            content_ref=ContentRef(
                unit_id=cast(str, content_ref.get("unit_id")),
                version=cast(str, content_ref.get("version")),
                content_hash=cast(str, content_ref.get("content_hash")),
            ),
            payload=payload,
            schema_version=cast(str, wire.get("schema_version")),
        )
    except (TypeError, ValueError) as error:
        raise _invariant(f"{label} is not a RuntimeEvent") from error
    if plain(event) != wire:
        raise _invariant(f"{label} is not canonical plain JSON")
    return event


class PostgresRunOutcomeAuthority:
    """Prove and derive one outcome event under a currently live Worker fence."""

    def __init__(
        self,
        database: PostgresDatabase,
        validator: ContractSchemaValidator,
    ) -> None:
        self._database = database
        self._validator = validator

    async def derive(
        self,
        *,
        worker_id: str,
        lease_id: str,
        root_event: GameEvent,
        context: OperationContext,
    ) -> GameEvent:
        try:
            async with self._database.transaction() as connection:
                await connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                row = await self._load_current(
                    connection,
                    worker_id=worker_id,
                    lease_id=lease_id,
                    root_event=root_event,
                    context=context,
                )
                run, run_wire, run_created_at = self._validate_current_row(
                    row,
                    root_event=root_event,
                    context=context,
                )
                self._validate_root_turn(
                    row,
                    root_event=root_event,
                    context=context,
                    run=run,
                )
                await self._validate_invocation(
                    connection,
                    row,
                    run=run,
                    root_event=root_event,
                    context=context,
                )
                await self._validate_evidence(
                    connection,
                    run=run,
                    run_wire=run_wire,
                    context=context,
                )
                await self._validate_world(
                    connection,
                    run=run,
                    root_event=root_event,
                    context=context,
                )
                failure_count = 0
                if not run.task_success:
                    failure_count = await self._exact_failure_count(
                        connection,
                        current_row=row,
                        current_run=run,
                        context=context,
                    )
                if run_created_at < root_event.occurred_at:
                    raise _invariant("Run durable time precedes its accepted root event")
                task = decode_as(row["task_json"], TaskSnapshot)
                if (
                    task.task_id != root_event.task_id
                    or not task.knowledge_points
                    or not _same_actor(task.request_context.actor, context.actor)
                    or task.request_context.content_ref != context.content_ref
                ):
                    raise _invariant("Task cannot supply a canonical outcome concept")
                derived_event = derive_run_outcome_event(
                    root_event=root_event,
                    run=run,
                    task=task,
                    failure_count=failure_count,
                    occurred_at=run_created_at,
                )
                await self._validate_final_replay(
                    connection,
                    event=derived_event,
                    run=run,
                    run_wire=run_wire,
                    context=context,
                )
                return derived_event
        except AgentPersistenceError:
            raise
        except psycopg.Error as error:
            raise AgentPersistenceError(
                "AGENT_OUTCOME_DEPENDENCY_UNAVAILABLE",
                "PostgreSQL could not validate the canonical Run outcome",
                {"sqlstate": error.sqlstate or "UNKNOWN"},
            ) from error
        except (TypeError, ValueError) as error:
            raise _invariant(
                "Canonical Run outcome contains invalid typed data",
                exception_type=type(error).__name__,
            ) from error

    @staticmethod
    async def _load_current(
        connection: _Connection,
        *,
        worker_id: str,
        lease_id: str,
        root_event: GameEvent,
        context: OperationContext,
    ) -> dict[str, object]:
        cursor = await connection.execute(
            """
            SELECT
              c.actor_id AS command_actor_id,c.content_hash AS command_content_hash,
              c.session_id AS command_session_id,c.turn_id AS command_turn_id,
              c.client_turn_sequence,c.revision AS command_revision,
              c.status AS command_status,c.record_json AS command_json,
              j.actor_id AS job_actor_id,j.content_hash AS job_content_hash,
              j.session_id AS job_session_id,j.turn_id AS job_turn_id,
              j.state AS job_state,j.worker_id,j.lease_id,j.lease_expires_at,
              j.event_json,j.operation_context_json,
              s.world_id AS session_world_id,s.snapshot_json AS session_json,
              t.snapshot_json AS task_json,
              r.actor_id AS run_actor_id,r.content_hash AS run_content_hash,
              r.run_id,r.session_id AS run_session_id,r.turn_id AS run_turn_id,
              r.command_id AS run_command_id,r.world_id AS run_world_id,
              r.skill_version_id,r.failure_key,r.task_success,
              r.snapshot_json AS run_snapshot_json,r.wire_json AS run_wire_json,
              r.created_at AS run_created_at,
              i.invocation_id,i.actor_id AS invocation_actor_id,
              i.content_hash AS invocation_content_hash,
              i.request_sha256 AS invocation_request_sha256,
              i.run_id AS invocation_run_id,i.result_json AS invocation_result_json,
              turn_record.actor_id AS root_turn_actor_id,
              turn_record.content_hash AS root_turn_content_hash,
              turn_record.event_sha256 AS root_turn_event_sha256,
              turn_record.record_json AS root_turn_record_json,
              turn_record.committed_at AS root_turn_committed_at,
              clock_timestamp() AS database_now
            FROM yaya_commands c
            JOIN yaya_command_jobs j
              ON j.tenant_id=c.tenant_id AND j.command_id=c.command_id
            JOIN yaya_agent_sessions s
              ON s.tenant_id=c.tenant_id AND s.session_id=c.session_id
             AND s.actor_id=c.actor_id AND s.content_hash=c.content_hash
            JOIN yaya_tasks t
              ON t.tenant_id=s.tenant_id AND t.task_id=s.task_id
             AND t.actor_id=s.actor_id AND t.content_hash=s.content_hash
            LEFT JOIN yaya_runs r
              ON r.tenant_id=c.tenant_id AND r.command_id=c.command_id
             AND r.actor_id=c.actor_id AND r.content_hash=c.content_hash
            LEFT JOIN yaya_skill_invocations i
              ON i.tenant_id=r.tenant_id AND i.run_id=r.run_id
             AND i.actor_id=r.actor_id AND i.content_hash=r.content_hash
            JOIN yaya_agent_turns turn_record
              ON turn_record.tenant_id=c.tenant_id AND turn_record.event_id=%s
            WHERE c.tenant_id=%s AND c.command_id=%s
            """,
            (root_event.event_id, context.actor.tenant_id, root_event.command_id),
        )
        rows = list(await cursor.fetchall())
        if len(rows) != 1:
            raise _invariant(
                "Leased Command does not resolve exactly one Run and invocation receipt",
                actual=len(rows),
            )
        row = rows[0]
        stored_event = decode_as(row["event_json"], GameEvent)
        stored_context = decode_as(row["operation_context_json"], OperationContext)
        command = decode_as(row["command_json"], CommandRecord)
        session = decode_as(row["session_json"], SessionSnapshot)
        now = cast(datetime, row["database_now"])
        if (
            stored_event != root_event
            or stored_context != context
            or root_event.event_type != "run_skill_requested"
            or command.command_id != root_event.command_id
            or command.revision != row["command_revision"]
            or command.status.value != row["command_status"]
            or command.status not in _NON_TERMINAL
            or command.terminal
            or not _same_actor(command.request_context.actor, context.actor)
            or command.request_context.content_ref != context.content_ref
            or row["command_actor_id"] != context.actor.actor_id
            or row["command_content_hash"] != context.content_ref.content_hash
            or row["command_session_id"] != root_event.session_id
            or row["command_turn_id"] != root_event.turn_id
            or row["job_actor_id"] != context.actor.actor_id
            or row["job_content_hash"] != context.content_ref.content_hash
            or row["job_session_id"] != root_event.session_id
            or row["job_turn_id"] != root_event.turn_id
            or row["job_state"] != "LEASED"
            or row["worker_id"] != worker_id
            or row["lease_id"] != lease_id
            or not isinstance(row["lease_expires_at"], datetime)
            or row["lease_expires_at"] <= now
            or session.session_id != root_event.session_id
            or session.student_id != context.actor.actor_id
            or session.task_id != root_event.task_id
            or session.world_id != row["session_world_id"]
            or not _same_actor(session.request_context.actor, context.actor)
            or session.request_context.content_ref != context.content_ref
        ):
            raise _invariant("Current Command, Job, Session, and live fence are not closed")
        return row

    def _validate_current_row(
        self,
        row: Mapping[str, object],
        *,
        root_event: GameEvent,
        context: OperationContext,
    ) -> tuple[RunResultSnapshot, dict[str, object], datetime]:
        if row.get("run_snapshot_json") is None or row.get("run_wire_json") is None:
            raise _invariant("Internal execution receipt has no durable Run")
        run = decode_as(row["run_snapshot_json"], RunResultSnapshot)
        wire = _mapping(row["run_wire_json"], "Run wire")
        self._validator.validate("schemas/game/run.schema.json", wire)
        created_at = row.get("run_created_at")
        if not isinstance(created_at, datetime):
            raise _invariant("Run durable timestamp is missing")
        if (
            row.get("run_actor_id") != context.actor.actor_id
            or row.get("run_content_hash") != context.content_ref.content_hash
            or row.get("run_id") != run.run_id
            or row.get("run_session_id") != run.session_id
            or row.get("run_turn_id") != run.turn_id
            or row.get("run_command_id") != run.command_id
            or row.get("run_world_id") != run.world_id
            or row.get("skill_version_id") != run.skill_ref.skill_version_id
            or row.get("failure_key") != run.failure_key
            or row.get("task_success") != run.task_success
            or run.session_id != root_event.session_id
            or run.turn_id != root_event.turn_id
            or run.command_id != root_event.command_id
            or run.world_id != row.get("session_world_id")
            or run.skill_ref != root_event.skill_ref
            or run.world_revision_before != root_event.expected_world_revision
            or not _same_actor(run.request_context.actor, context.actor)
            or run.request_context.content_ref != context.content_ref
            or wire.get("run_id") != run.run_id
            or wire.get("session_id") != run.session_id
            or wire.get("turn_id") != run.turn_id
            or wire.get("command_id") != run.command_id
            or wire.get("request_context") != _context_wire(run.request_context)
            or wire.get("skill") != plain(run.skill_ref)
            or wire.get("terminal") is not True
            or wire.get("evidence_refs") != [_evidence_ref_wire(item) for item in run.evidence_refs]
        ):
            raise _invariant("Run row, typed snapshot, wire, and accepted event differ")
        status = wire.get("status")
        world = _mapping(wire.get("world_application"), "Run world_application")
        if run.task_success:
            if status != "SUCCEEDED" or world.get("status") != "COMMITTED":
                raise _invariant("Successful Run wire is not a committed success")
            if run.world_commit is None or world.get("receipt") != plain(run.world_commit):
                raise _invariant("Successful Run wire receipt differs from its typed receipt")
        elif status not in {"REJECTED", "FAILED"} or world.get("status") == "COMMITTED":
            raise _invariant("Failed Run wire has an unsupported terminal outcome")
        return run, wire, created_at

    @staticmethod
    def _validate_root_turn(
        row: Mapping[str, object],
        *,
        root_event: GameEvent,
        context: OperationContext,
        run: RunResultSnapshot,
    ) -> None:
        raw_record = row.get("root_turn_record_json")
        committed_at = row.get("root_turn_committed_at")
        if raw_record is None or not isinstance(committed_at, datetime):
            raise _invariant("Internal root AgentTurn receipt is not committed")
        record = decode_as(raw_record, CommittedAgentTurn)
        invocation = decode_as(row["invocation_result_json"], SkillInvocationResult)
        invoke_calls = tuple(
            call for call in record.decision.tool_calls if call.name == "invoke_skill"
        )
        if len(invoke_calls) != 1:
            raise _invariant("Internal root AgentTurn has no unique SkillInvocation receipt")
        call = invoke_calls[0]
        expected_summary = {
            "run_id": run.run_id,
            "task_success": run.task_success,
            "world_revision_before": run.world_revision_before,
            "world_revision_after": run.world_revision_after,
            "world_difference": plain(run.world_difference),
            "evidence_ids": [item.evidence_id for item in run.evidence_refs],
        }
        expected_arguments = {
            "skill_id": "bound_skill",
            "arguments": plain(invocation.arguments),
        }
        event_hash = canonical_json_sha256(
            _mapping(plain(root_event), "internal root AgentTurn event")
        )
        if (
            row.get("root_turn_actor_id") != context.actor.actor_id
            or row.get("root_turn_content_hash") != context.content_ref.content_hash
            or row.get("root_turn_event_sha256") != event_hash
            or record.event != root_event
            or record.actor != context.actor
            or record.content_ref != context.content_ref
            or record.route.event_type != "run_skill_requested"
            or record.route.role != "xiaohutao"
            or record.decision.role != "xiaohutao"
            or record.decision.evidence_refs != run.evidence_refs
            or call.execution_id
            != side_effect_execution_id(root_event.command_id, root_event.turn_id)
            or plain(call.arguments) != expected_arguments
            or plain(call.result_summary) != expected_summary
            or committed_at < record.decision.completed_at
        ):
            raise _invariant(
                "Internal root AgentTurn does not close its accepted event and Run receipt"
            )

    async def _validate_final_replay(
        self,
        connection: _Connection,
        *,
        event: GameEvent,
        run: RunResultSnapshot,
        run_wire: Mapping[str, object],
        context: OperationContext,
    ) -> None:
        feedback = run_wire.get("agent_feedback")
        cursor = await connection.execute(
            """
            SELECT actor_id,content_hash,event_sha256,record_json,committed_at
            FROM yaya_agent_turns
            WHERE tenant_id=%s AND event_id=%s
            """,
            (context.actor.tenant_id, event.event_id),
        )
        row = await cursor.fetchone()
        if feedback is None:
            if row is not None and row.get("record_json") is not None:
                raise _invariant("Committed final AgentTurn has no exact Run feedback")
            return
        feedback_wire = _mapping(feedback, "Run agent_feedback")
        if row is None or row.get("record_json") is None:
            raise _invariant("Run feedback has no committed final AgentTurn")
        record = decode_as(row["record_json"], CommittedAgentTurn)
        committed_at = row.get("committed_at")
        expected_feedback = {
            "session_id": event.session_id,
            "turn_id": event.turn_id,
            "command_id": event.command_id,
            "run_id": run.run_id,
            "message_key": record.decision.message_key,
            "message": record.decision.message,
            "source": record.decision.source,
            "degraded": record.decision.degraded,
            "fallback_reason": record.decision.fallback_reason,
            "evidence_refs": [_evidence_ref_wire(item) for item in record.decision.evidence_refs],
            "completed_at": plain(record.decision.completed_at),
        }
        event_hash = canonical_json_sha256(_mapping(plain(event), "derived AgentTurn event"))
        if (
            row.get("actor_id") != context.actor.actor_id
            or row.get("content_hash") != context.content_ref.content_hash
            or row.get("event_sha256") != event_hash
            or not isinstance(committed_at, datetime)
            or record.event != event
            or record.actor != context.actor
            or record.content_ref != context.content_ref
            or record.decision.evidence_refs != run.evidence_refs
            or feedback_wire != expected_feedback
        ):
            raise _invariant("Run feedback replay does not close its final AgentTurn")

    @staticmethod
    async def _validate_invocation(
        connection: _Connection,
        row: Mapping[str, object],
        *,
        run: RunResultSnapshot,
        root_event: GameEvent,
        context: OperationContext,
    ) -> None:
        del connection
        expected_id = side_effect_execution_id(root_event.command_id, root_event.turn_id)
        if row.get("invocation_result_json") is None:
            raise _invariant("Run has no durable SkillInvocation receipt")
        result = decode_as(row["invocation_result_json"], SkillInvocationResult)
        if (
            row.get("invocation_id") != expected_id
            or row.get("invocation_actor_id") != context.actor.actor_id
            or row.get("invocation_content_hash") != context.content_ref.content_hash
            or row.get("invocation_run_id") != run.run_id
            or row.get("invocation_request_sha256") != result.request_sha256
            or result.invocation_id != expected_id
            or result.tenant_id != context.actor.tenant_id
            or result.run != run
        ):
            raise _invariant("SkillInvocation receipt is not closed to the canonical Run")

    async def _validate_evidence(
        self,
        connection: _Connection,
        *,
        run: RunResultSnapshot,
        run_wire: Mapping[str, object],
        context: OperationContext,
    ) -> None:
        expected_types = (
            {EvidenceType.SANDBOX_LOG, EvidenceType.WORLD_COMMIT}
            if run.task_success
            else {EvidenceType.SANDBOX_LOG}
        )
        if (
            len(run.evidence_refs) != len(expected_types)
            or {item.evidence_type for item in run.evidence_refs} != expected_types
            or any(item.sha256 is None or item.uri is not None for item in run.evidence_refs)
        ):
            raise _invariant("Run does not contain the exact production Evidence set")
        ids = [item.evidence_id for item in run.evidence_refs]
        cursor = await connection.execute(
            """
            SELECT actor_id,content_hash,evidence_id,evidence_type,payload_sha256,
                   evidence_json,recorded_at
            FROM yaya_evidence
            WHERE tenant_id=%s AND evidence_id=ANY(%s)
            """,
            (context.actor.tenant_id, ids),
        )
        rows = list(await cursor.fetchall())
        by_id = {cast(str, row["evidence_id"]): row for row in rows}
        if len(rows) != len(ids) or len(by_id) != len(ids):
            raise _invariant("Run Evidence does not resolve exactly once")
        sandbox_wire = _mapping(run_wire.get("sandbox"), "Run sandbox")
        world_wire = _mapping(run_wire.get("world_application"), "Run world_application")
        intents = sandbox_wire.get("action_intents")
        if isinstance(intents, (str, bytes, bytearray)) or not isinstance(intents, Sequence):
            raise _invariant("Run Sandbox intents are not an array")
        for reference in run.evidence_refs:
            row = by_id[reference.evidence_id]
            document = _mapping(row["evidence_json"], "Evidence document")
            self._validator.validate("schemas/game/evidence.schema.json", document)
            evidence_ref = _mapping(document.get("evidence_ref"), "Evidence reference")
            origin = _mapping(document.get("request_context"), "Evidence request_context")
            source = _mapping(document.get("source"), "Evidence source")
            subject = _mapping(document.get("subject"), "Evidence subject")
            integrity = _mapping(document.get("integrity"), "Evidence integrity")
            payload = _mapping(document.get("payload"), "Evidence payload")
            recorded_at = row.get("recorded_at")
            if (
                row.get("actor_id") != context.actor.actor_id
                or row.get("content_hash") != context.content_ref.content_hash
                or row.get("evidence_type") != reference.evidence_type.value
                or row.get("payload_sha256") != reference.sha256
                or evidence_ref != _evidence_ref_wire(reference)
                or origin != _context_wire(run.request_context)
                or subject.get("learner_id") != context.actor.actor_id
                or integrity.get("payload_sha256") != reference.sha256
                or canonical_json_sha256(payload) != reference.sha256
                or not isinstance(recorded_at, datetime)
                or document.get("recorded_at") != _iso(recorded_at)
                or document.get("occurred_at") != _iso(reference.created_at)
            ):
                raise _invariant("Evidence row, document, reference, or authority differs")
            if reference.evidence_type is EvidenceType.SANDBOX_LOG:
                if (
                    source.get("source_type") != "SKILL_RUN"
                    or source.get("source_id") != run.run_id
                    or source.get("command_id") != run.command_id
                    or source.get("world_id") != run.world_id
                    or payload.get("evidence_kind") != "SKILL_RUN"
                    or payload.get("run_id") != run.run_id
                    or payload.get("sandbox_status") != sandbox_wire.get("status")
                    or payload.get("world_status") != world_wire.get("status")
                    or payload.get("intent_count") != len(cast(Sequence[object], intents))
                ):
                    raise _invariant("SANDBOX_LOG Evidence differs from its Run")
            else:
                receipt = run.world_commit
                if receipt is None:
                    raise _invariant("WORLD_COMMIT Evidence has no typed receipt")
                if (
                    source.get("source_type") != "WORLD"
                    or source.get("source_id") != run.world_id
                    or source.get("command_id") != run.command_id
                    or source.get("world_id") != run.world_id
                    or payload
                    != {
                        "evidence_kind": "WORLD_COMMIT",
                        "world_id": receipt.world_id,
                        "previous_revision": receipt.previous_revision,
                        "world_revision": receipt.world_revision,
                        "first_event_sequence": receipt.first_event_sequence,
                        "last_event_sequence": receipt.last_event_sequence,
                        "state_hash": receipt.state_hash,
                    }
                ):
                    raise _invariant("WORLD_COMMIT Evidence differs from its receipt")

    async def _validate_world(
        self,
        connection: _Connection,
        *,
        run: RunResultSnapshot,
        root_event: GameEvent,
        context: OperationContext,
    ) -> None:
        cursor = await connection.execute(
            """
            SELECT actor_id,content_hash,stream_id,revision,last_event_sequence,
                   state_hash,state_json,request_context_json
            FROM yaya_worlds
            WHERE tenant_id=%s AND world_id=%s
            """,
            (context.actor.tenant_id, run.world_id),
        )
        rows = list(await cursor.fetchall())
        if len(rows) != 1:
            raise _invariant("Run World does not resolve exactly once")
        row = rows[0]
        origin = decode_as(row["request_context_json"], RequestContext)
        state = _mapping(row["state_json"], "World state")
        expected_revision = run.world_revision_after
        expected_sequence = 0
        if run.world_commit is not None:
            expected_sequence = run.world_commit.last_event_sequence
        elif not isinstance(row.get("last_event_sequence"), int):
            raise _invariant("World event sequence is invalid")
        if (
            row.get("actor_id") != context.actor.actor_id
            or row.get("content_hash") != context.content_ref.content_hash
            or row.get("stream_id") != f"world:{run.world_id}"
            or row.get("revision") != expected_revision
            or row.get("state_hash") != canonical_json_sha256(state)
            or not _same_actor(origin.actor, context.actor)
            or origin.content_ref != context.content_ref
        ):
            raise _invariant("Current World CAS does not close the Run outcome")
        if run.world_commit is None:
            if run.task_success or run.world_revision_after != run.world_revision_before:
                raise _invariant("Uncommitted Run claims a World change")
            return
        receipt = run.world_commit
        if (
            not run.task_success
            or row.get("state_hash") != receipt.state_hash
            or receipt.first_event_sequence != receipt.last_event_sequence
            or row.get("last_event_sequence") != expected_sequence
        ):
            raise _invariant("Successful Run receipt is not one exact World event")
        event_cursor = await connection.execute(
            """
            SELECT event_id,stream_id,sequence,event_type,event_json,occurred_at
            FROM yaya_events
            WHERE tenant_id=%s AND stream_id=%s
              AND sequence BETWEEN %s AND %s
            ORDER BY sequence,event_id
            """,
            (
                context.actor.tenant_id,
                f"world:{run.world_id}",
                receipt.first_event_sequence,
                receipt.last_event_sequence,
            ),
        )
        event_rows = list(await event_cursor.fetchall())
        if len(event_rows) != 1:
            raise _invariant("World receipt does not resolve exactly one canonical event")
        event_row = event_rows[0]
        event_wire = _mapping(event_row["event_json"], "World committed event")
        self._validator.validate("schemas/common/event-envelope.schema.json", event_wire)
        event = _plain_runtime_event(event_wire, "World committed event")
        payload = event.payload
        raw_event_evidence = payload.get("evidence_refs")
        event_evidence = (
            list(cast(Sequence[object], raw_event_evidence))
            if isinstance(raw_event_evidence, Sequence)
            and not isinstance(raw_event_evidence, (str, bytes, bytearray))
            else None
        )
        expected_world_ref = next(
            item for item in run.evidence_refs if item.evidence_type is EvidenceType.WORLD_COMMIT
        )
        if (
            event_row.get("event_id") != event.event_id
            or event_row.get("stream_id") != event.stream_id
            or event_row.get("sequence") != event.sequence
            or event_row.get("event_type") != event.event_type
            or event_row.get("occurred_at") != event.occurred_at
            or event.event_type is not RuntimeEventType.WORLD_COMMITTED
            or event.stream_id != f"world:{run.world_id}"
            or event.sequence != receipt.first_event_sequence
            or event.command_id != run.command_id
            or event.command_id != root_event.command_id
            or event.causation_id != root_event.command_id
            or event.trace_id != context.trace_id
            or event.correlation_id != context.correlation_id
            or event.content_ref != context.content_ref
            or event.occurred_at != receipt.committed_at
            or payload.get("commit_id")
            != world_commit_identifier(
                context.actor.tenant_id,
                event.stream_id,
                run.run_id,
                run.world_revision_before,
            )
            or payload.get("run_id") != run.run_id
            or payload.get("world_id") != run.world_id
            or payload.get("previous_world_revision") != receipt.previous_revision
            or payload.get("world_revision") != receipt.world_revision
            or payload.get("state_hash") != receipt.state_hash
            or payload.get("committed_at") != _iso(receipt.committed_at)
            or event_evidence != [_evidence_ref_wire(expected_world_ref)]
        ):
            raise _invariant("World event identity or payload differs from the Run receipt")

    async def _exact_failure_count(
        self,
        connection: _Connection,
        *,
        current_row: Mapping[str, object],
        current_run: RunResultSnapshot,
        context: OperationContext,
    ) -> int:
        if current_run.failure_key is None or current_run.task_success:
            raise _invariant("Failure streak requested for a non-failed Run")
        current_sequence = current_row.get("client_turn_sequence")
        if isinstance(current_sequence, bool) or not isinstance(current_sequence, int):
            raise _invariant("Current Command has no client turn sequence")
        cursor = await connection.execute(
            """
            SELECT
              c.command_id,c.actor_id AS command_actor_id,
              c.content_hash AS command_content_hash,c.session_id AS command_session_id,
              c.turn_id AS command_turn_id,c.client_turn_sequence,
              c.revision AS command_revision,c.status AS command_status,
              c.record_json AS command_json,
              j.state AS job_state,j.actor_id AS job_actor_id,
              j.content_hash AS job_content_hash,j.session_id AS job_session_id,
              j.turn_id AS job_turn_id,j.event_json,j.operation_context_json,
              s.world_id AS session_world_id,
              r.actor_id AS run_actor_id,r.content_hash AS run_content_hash,
              r.run_id,r.session_id AS run_session_id,r.turn_id AS run_turn_id,
              r.command_id AS run_command_id,r.world_id AS run_world_id,
              r.skill_version_id,r.failure_key,r.task_success,
              r.snapshot_json AS run_snapshot_json,r.wire_json AS run_wire_json,
              r.created_at AS run_created_at,
              i.invocation_id,i.actor_id AS invocation_actor_id,
              i.content_hash AS invocation_content_hash,
              i.request_sha256 AS invocation_request_sha256,
              i.run_id AS invocation_run_id,i.result_json AS invocation_result_json
            FROM yaya_commands c
            JOIN yaya_command_jobs j
              ON j.tenant_id=c.tenant_id AND j.command_id=c.command_id
            JOIN yaya_agent_sessions s
              ON s.tenant_id=c.tenant_id AND s.session_id=c.session_id
             AND s.actor_id=c.actor_id AND s.content_hash=c.content_hash
            LEFT JOIN yaya_runs r
              ON r.tenant_id=c.tenant_id AND r.command_id=c.command_id
             AND r.actor_id=c.actor_id AND r.content_hash=c.content_hash
            LEFT JOIN yaya_skill_invocations i
              ON i.tenant_id=r.tenant_id AND i.run_id=r.run_id
             AND i.actor_id=r.actor_id AND i.content_hash=r.content_hash
            WHERE c.tenant_id=%s AND c.session_id=%s
              AND c.client_turn_sequence<=%s
            ORDER BY c.client_turn_sequence DESC,c.command_id DESC
            """,
            (context.actor.tenant_id, current_run.session_id, current_sequence),
        )
        rows = list(await cursor.fetchall())
        if not rows or rows[0].get("command_id") != current_run.command_id:
            raise _invariant("Failure history does not start at the current Command")
        count = 0
        expected_sequence = current_sequence
        for index, row in enumerate(rows):
            sequence = row.get("client_turn_sequence")
            if sequence != expected_sequence:
                raise _invariant("Failure history client turn sequence contains a gap")
            expected_sequence -= 1
            command = decode_as(row["command_json"], CommandRecord)
            stored_event = decode_as(row["event_json"], GameEvent)
            stored_context = decode_as(row["operation_context_json"], OperationContext)
            if (
                command.command_id != row.get("command_id")
                or command.revision != row.get("command_revision")
                or command.status.value != row.get("command_status")
                or row.get("command_actor_id") != context.actor.actor_id
                or row.get("command_content_hash") != context.content_ref.content_hash
                or row.get("command_session_id") != current_run.session_id
                or row.get("job_actor_id") != context.actor.actor_id
                or row.get("job_content_hash") != context.content_ref.content_hash
                or row.get("job_session_id") != current_run.session_id
                or not _same_actor(command.request_context.actor, context.actor)
                or command.request_context.content_ref != context.content_ref
                or stored_context.actor != command.request_context.actor
                or stored_context.content_ref != context.content_ref
                or stored_event.command_id != command.command_id
                or stored_event.session_id != current_run.session_id
                or stored_event.turn_id != row.get("command_turn_id")
                or stored_event.student_id != context.actor.actor_id
            ):
                raise _invariant("Failure history Command/Job authority drifted")
            is_current = index == 0
            if is_current:
                if command.terminal or command.status not in _NON_TERMINAL:
                    raise _invariant("Current failure Command is unexpectedly terminal")
            elif not command.terminal or row.get("job_state") != "DONE":
                raise _invariant("Prior failure history Command/Job is not terminal and DONE")
            if row.get("run_snapshot_json") is None:
                if is_current:
                    raise _invariant("Current failure history row has no Run")
                break
            run, run_wire, _ = self._validate_current_row(
                row,
                root_event=stored_event,
                context=stored_context,
            )
            if not is_current:
                await self._validate_invocation(
                    connection,
                    row,
                    run=run,
                    root_event=stored_event,
                    context=stored_context,
                )
                await self._validate_evidence(
                    connection,
                    run=run,
                    run_wire=run_wire,
                    context=stored_context,
                )
                await self._validate_world(
                    connection,
                    run=run,
                    root_event=stored_event,
                    context=stored_context,
                )
                self._validate_terminal_run_command(command, run, run_wire)
            same_failure = (
                not run.task_success
                and run.failure_key == current_run.failure_key
                and run.skill_ref == current_run.skill_ref
                and run.world_id == current_run.world_id
            )
            if not same_failure:
                break
            count += 1
        if count < 1:
            raise _invariant("Canonical failure suffix is empty")
        return count

    @staticmethod
    def _validate_terminal_run_command(
        command: CommandRecord,
        run: RunResultSnapshot,
        run_wire: Mapping[str, object],
    ) -> None:
        status = run_wire.get("status")
        if not isinstance(status, str):
            raise _invariant("Prior terminal Run has no canonical status")
        expected = {
            "SUCCEEDED": CommandStatus.APPLIED,
            "REJECTED": CommandStatus.REJECTED,
            "FAILED": CommandStatus.FAILED,
            "UNKNOWN": CommandStatus.UNKNOWN,
        }.get(status)
        if (
            expected is None
            or command.status is not expected
            or command.evidence_refs != run.evidence_refs
            or command.links.get("run") != f"/v1/runs/{run.run_id}"
        ):
            raise _invariant("Prior terminal Command differs from its canonical Run")
