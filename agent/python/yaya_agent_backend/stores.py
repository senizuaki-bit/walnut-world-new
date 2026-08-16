"""PostgreSQL implementations of the public provider-neutral store ports."""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import LiteralString, cast

import psycopg
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from yaya_agent_contracts import (
    ActivateSkillInput,
    ActiveSkill,
    AuditQuery,
    AuditRecord,
    CertificationEvidence,
    CertifiedSkill,
    CommandCreateReceipt,
    CommandRecord,
    CommandTransition,
    CommandType,
    ContractError,
    CursorPage,
    DeliveryReceipt,
    DomainEvent,
    ErrorCategory,
    EventAppendReceipt,
    EvidenceRef,
    EvidenceType,
    Failure,
    FrozenJsonObject,
    LearnerModelSnapshot,
    LearnerUpdate,
    NewCommand,
    OperationContext,
    OutboxMessage,
    OutboxStatus,
    RegistrySnapshot,
    Result,
    RuntimeEvent,
    RuntimeEventType,
    SkillRef,
    Success,
    UncommittedEvent,
    canonical_json_sha256,
    learner_inference_sha256,
)
from yaya_agent_runtime.domain import (
    CommittedAgentTurn,
    CompileResultSnapshot,
    RunResultSnapshot,
    SessionSnapshot,
    SkillSnapshot,
    TaskSnapshot,
)
from yaya_agent_runtime.learner_projection_policy import (
    LEARNER_PROJECTION_POLICY_VERSION,
    REVIEW_POLICY_VERSION,
    CompetencyProjection,
    LearnerProjectionPolicy,
    ProjectionEvidence,
    ProjectionInput,
    ProjectionOutcome,
    TaskRelation,
)

from .codec import (
    agent_turn_commit_sha256,
    decode,
    decode_as,
    encode,
    internal_record_sha256,
    plain,
)
from .database import PostgresCommitStateUnknown, PostgresDatabase
from .learner_model_integrity import (
    validate_persisted_learner_snapshot,
    validated_learner_competencies,
)
from .learner_projection import LearnerProjectionFence, LearnerProjectionFenceLost

type _Connection = AsyncConnection[dict[str, object]]

_LEARNER_EMPTY_UPDATED_AT = datetime(1970, 1, 1, tzinfo=UTC)
_MAX_LEARNER_SNAPSHOT_EVIDENCE_REFS = 64


class _CommitRoundtripUnknown(psycopg.OperationalError):
    """The transaction body completed, but the COMMIT acknowledgement was lost."""


@asynccontextmanager
async def _store_transaction(database: PostgresDatabase) -> AsyncGenerator[_Connection]:
    try:
        async with database.transaction_with_commit_boundary() as connection:
            yield connection
    except PostgresCommitStateUnknown as error:
        # Keep the Store-facing error taxonomy stable while sharing the database
        # boundary's SQLSTATE-first classification.  In particular, PostgreSQL
        # 40001/40P01 confirm rollback even though psycopg models both as
        # OperationalError subclasses; only a lost COMMIT acknowledgement is
        # reconciliation-unknown.
        raise _CommitRoundtripUnknown(str(error)) from error


_ERRORS: dict[str, tuple[ErrorCategory, bool, str]] = {
    "NOT_FOUND": (ErrorCategory.VALIDATION, False, "resource.not_found"),
    "AUTHORIZATION_DENIED": (
        ErrorCategory.AUTHORIZATION,
        False,
        "auth.permission_denied",
    ),
    "CONTENT_VERSION_MISMATCH": (
        ErrorCategory.VALIDATION,
        False,
        "content.version_mismatch",
    ),
    "IDEMPOTENCY_KEY_REUSED": (
        ErrorCategory.CONCURRENCY,
        False,
        "request.idempotency_conflict",
    ),
    "EVENT_SEQUENCE_GAP": (
        ErrorCategory.CONCURRENCY,
        True,
        "event.resync_required",
    ),
    "SKILL_NOT_CERTIFIED": (ErrorCategory.SKILL, False, "skill.not_certified"),
    "SKILL_VERSION_MISMATCH": (ErrorCategory.SKILL, False, "skill.version_mismatch"),
    "ACTIVE_SKILL_ARTIFACT_MISMATCH": (
        ErrorCategory.INVARIANT,
        False,
        "skill.artifact_mismatch",
    ),
    "DEPENDENCY_UNAVAILABLE": (
        ErrorCategory.DEPENDENCY,
        True,
        "dependency.temporarily_unavailable",
    ),
    "UNKNOWN_COMMIT_STATE": (
        ErrorCategory.DEPENDENCY,
        False,
        "command.reconciling",
    ),
    "INVARIANT_VIOLATION": (
        ErrorCategory.INVARIANT,
        False,
        "system.invariant_violation",
    ),
}


def _failure(code: str, stage: str, message: str) -> Failure:
    category, retryable, message_key = _ERRORS[code]
    return Failure(
        ContractError(
            code=code,
            category=category,
            retryable=retryable,
            user_message_key=message_key,
            stage=stage,
            message=message[:512],
        )
    )


def _database_failure(error: BaseException, stage: str) -> Failure:
    commit_roundtrip_unknown = isinstance(error, _CommitRoundtripUnknown)
    if isinstance(error, psycopg.IntegrityError):
        code = "INVARIANT_VIOLATION"
    else:
        code = "UNKNOWN_COMMIT_STATE" if commit_roundtrip_unknown else "DEPENDENCY_UNAVAILABLE"
    return _failure(code, stage, f"PostgreSQL operation failed: {error}")


def _same_authority(record: CommandRecord, context: OperationContext) -> bool:
    stored = record.request_context.actor
    current = context.actor
    return _same_actor(stored, current) and (
        record.request_context.content_ref == context.content_ref
    )


def _same_actor(stored: object, current: object) -> bool:
    from yaya_agent_contracts import ActorRef

    if not isinstance(stored, ActorRef) or not isinstance(current, ActorRef):
        return False
    return (
        stored.tenant_id,
        stored.actor_id,
        stored.actor_type,
    ) == (
        current.tenant_id,
        current.actor_id,
        current.actor_type,
    )


def _encode_cursor(occurred_at: datetime, identifier: str) -> str:
    raw = json.dumps(
        [occurred_at.isoformat(), identifier],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = cast(object, json.loads(base64.urlsafe_b64decode(padded).decode("utf-8")))
        if not isinstance(decoded, list):
            raise ValueError
        items = cast(list[object], decoded)
        if len(items) != 2 or not isinstance(items[0], str) or not isinstance(items[1], str):
            raise ValueError
        timestamp = datetime.fromisoformat(items[0])
        if timestamp.utcoffset() is None:
            raise ValueError
        return timestamp, items[1]
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("cursor is not a valid opaque PostgreSQL cursor") from error


def _runtime_evidence(value: object) -> tuple[EvidenceRef, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("event evidence_refs must be an array")
    items: list[EvidenceRef] = []
    for raw_value in cast(Sequence[object], value):
        if not isinstance(raw_value, Mapping):
            raise ValueError("event evidence_refs items must be objects")
        raw = cast(Mapping[str, object], raw_value)
        created_at = raw.get("created_at")
        if not isinstance(created_at, str):
            raise ValueError("event Evidence created_at must be a string")
        items.append(
            EvidenceRef(
                evidence_id=cast(str, raw.get("evidence_id")),
                evidence_type=EvidenceType(cast(str, raw.get("evidence_type"))),
                created_at=datetime.fromisoformat(created_at.replace("Z", "+00:00")),
                sha256=cast(str | None, raw.get("sha256")),
                uri=cast(str | None, raw.get("uri")),
            )
        )
    return tuple(items)


class PostgresCommandStore:
    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    async def get(
        self,
        command_id: str,
        context: OperationContext,
    ) -> Result[CommandRecord]:
        try:
            connection = await self._database.connect(autocommit=True)
            try:
                cursor = await connection.execute(
                    """
                    SELECT record_json FROM yaya_commands
                    WHERE tenant_id=%s AND command_id=%s AND actor_id=%s AND content_hash=%s
                    """,
                    (
                        context.actor.tenant_id,
                        command_id,
                        context.actor.actor_id,
                        context.content_ref.content_hash,
                    ),
                )
                row = await cursor.fetchone()
            finally:
                await connection.close()
            if row is None:
                return _failure("NOT_FOUND", "ACCEPT", "Command not found")
            record = decode_as(row["record_json"], CommandRecord)
            if not _same_authority(record, context):
                return _failure("NOT_FOUND", "ACCEPT", "Command not found")
            return Success(record)
        except psycopg.Error as error:
            return _database_failure(error, "ACCEPT")

    async def get_by_idempotency_key(
        self,
        operation: CommandType,
        idempotency_key: str,
        context: OperationContext,
    ) -> Result[CommandRecord]:
        try:
            connection = await self._database.connect(autocommit=True)
            try:
                cursor = await connection.execute(
                    """
                    SELECT record_json FROM yaya_commands
                    WHERE tenant_id=%s AND actor_id=%s AND operation=%s AND idempotency_key=%s
                    """,
                    (
                        context.actor.tenant_id,
                        context.actor.actor_id,
                        operation,
                        idempotency_key,
                    ),
                )
                row = await cursor.fetchone()
            finally:
                await connection.close()
            if row is None:
                return _failure("NOT_FOUND", "ACCEPT", "Command idempotency key not found")
            record = decode_as(row["record_json"], CommandRecord)
            if not _same_actor(record.request_context.actor, context.actor):
                return _failure("NOT_FOUND", "ACCEPT", "Command not found")
            return Success(record)
        except psycopg.Error as error:
            return _database_failure(error, "ACCEPT")

    async def accept_once(
        self,
        command: NewCommand,
        context: OperationContext,
    ) -> Result[CommandCreateReceipt]:
        try:
            async with _store_transaction(self._database) as connection:
                cursor = await connection.execute(
                    """
                    SELECT request_sha256,record_json FROM yaya_commands
                    WHERE tenant_id=%s AND actor_id=%s AND operation=%s AND idempotency_key=%s
                    FOR UPDATE
                    """,
                    command.idempotency_scope(context),
                )
                existing = await cursor.fetchone()
                if existing is not None:
                    record = decode_as(existing["record_json"], CommandRecord)
                    if not _same_actor(record.request_context.actor, context.actor):
                        return _failure(
                            "AUTHORIZATION_DENIED", "ACCEPT", "Command authority mismatch"
                        )
                    if existing["request_sha256"] != command.request_sha256:
                        return _failure(
                            "IDEMPOTENCY_KEY_REUSED",
                            "ACCEPT",
                            "Idempotency key was used for a different request",
                        )
                    return Success(CommandCreateReceipt(record, False))
                clock = await connection.execute("SELECT clock_timestamp() AS value")
                clock_row = await clock.fetchone()
                if clock_row is None:
                    raise RuntimeError("PostgreSQL clock query returned no row")
                record = command.initial_record(context, cast(datetime, clock_row["value"]))
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
                return Success(CommandCreateReceipt(record, True))
        except psycopg.errors.UniqueViolation:
            connection = await self._database.connect(autocommit=True)
            try:
                cursor = await connection.execute(
                    """
                    SELECT request_sha256,record_json FROM yaya_commands
                    WHERE tenant_id=%s AND actor_id=%s AND operation=%s AND idempotency_key=%s
                    """,
                    command.idempotency_scope(context),
                )
                row = await cursor.fetchone()
            finally:
                await connection.close()
            if row is None or row["request_sha256"] != command.request_sha256:
                return _failure(
                    "IDEMPOTENCY_KEY_REUSED", "ACCEPT", "Concurrent request hash differs"
                )
            replay = decode_as(row["record_json"], CommandRecord)
            if not _same_actor(replay.request_context.actor, context.actor):
                return _failure("AUTHORIZATION_DENIED", "ACCEPT", "Command authority mismatch")
            return Success(CommandCreateReceipt(replay, False))
        except psycopg.Error as error:
            return _database_failure(error, "ACCEPT")

    async def transition(
        self,
        transition: CommandTransition,
        context: OperationContext,
    ) -> Result[CommandRecord]:
        if not _same_authority(transition.previous_record, context):
            return _failure("AUTHORIZATION_DENIED", "VALIDATE", "Command authority mismatch")
        try:
            async with _store_transaction(self._database) as connection:
                cursor = await connection.execute(
                    """
                    SELECT record_json FROM yaya_commands
                    WHERE tenant_id=%s AND command_id=%s AND actor_id=%s
                      AND content_hash=%s FOR UPDATE
                    """,
                    (
                        context.actor.tenant_id,
                        transition.command_id,
                        context.actor.actor_id,
                        context.content_ref.content_hash,
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    return _failure("NOT_FOUND", "VALIDATE", "Command not found")
                current = decode_as(row["record_json"], CommandRecord)
                if not _same_authority(current, context):
                    return _failure("NOT_FOUND", "VALIDATE", "Command not found")
                if current != transition.previous_record:
                    return _failure(
                        "EVENT_SEQUENCE_GAP",
                        "VALIDATE",
                        "Command CAS revision or status no longer matches",
                    )
                result = await connection.execute(
                    """
                    UPDATE yaya_commands
                    SET revision=%s,status=%s,updated_at=%s,record_json=%s
                    WHERE tenant_id=%s AND command_id=%s AND actor_id=%s
                      AND content_hash=%s AND revision=%s AND status=%s
                    """,
                    (
                        transition.next_record.revision,
                        transition.next_record.status.value,
                        transition.next_record.updated_at,
                        Jsonb(encode(transition.next_record)),
                        context.actor.tenant_id,
                        transition.command_id,
                        context.actor.actor_id,
                        context.content_ref.content_hash,
                        transition.expected_revision,
                        transition.expected_status.value,
                    ),
                )
                if result.rowcount != 1:
                    return _failure("EVENT_SEQUENCE_GAP", "VALIDATE", "Command CAS was lost")
                return Success(transition.next_record)
        except psycopg.Error as error:
            return _database_failure(error, "VALIDATE")

    async def find_non_terminal_before(
        self,
        updated_before: datetime,
        cursor: str | None,
        limit: int,
        context: OperationContext,
    ) -> Result[CursorPage[CommandRecord]]:
        if not 1 <= limit <= 1000:
            return _failure("INVARIANT_VIOLATION", "VALIDATE", "limit is outside 1..1000")
        try:
            after = _decode_cursor(cursor) if cursor is not None else None
        except ValueError as error:
            return _failure("INVARIANT_VIOLATION", "VALIDATE", str(error))
        try:
            connection = await self._database.connect(autocommit=True)
            try:
                parameters: list[object] = [
                    context.actor.tenant_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    updated_before,
                ]
                cursor_clause = ""
                if after is not None:
                    cursor_clause = "AND (updated_at,command_id) > (%s,%s)"
                    parameters.extend(after)
                parameters.append(limit + 1)
                query_text: LiteralString = f"""
                    SELECT updated_at,command_id,record_json FROM yaya_commands
                    WHERE tenant_id=%s AND actor_id=%s AND content_hash=%s AND updated_at < %s
                      AND status IN ('ACCEPTED','VALIDATING','RUNNING_SANDBOX','APPLYING_WORLD')
                      {cursor_clause}
                    ORDER BY updated_at,command_id LIMIT %s
                    """
                result = await connection.execute(query_text, tuple(parameters))
                rows = list(await result.fetchall())
            finally:
                await connection.close()
            page_rows = rows[:limit]
            records: list[CommandRecord] = []
            for row in page_rows:
                record = decode_as(row["record_json"], CommandRecord)
                if not _same_authority(record, context):
                    return _failure("AUTHORIZATION_DENIED", "VALIDATE", "Command authority drift")
                records.append(record)
            next_cursor = None
            if len(rows) > limit and page_rows:
                final = page_rows[-1]
                next_cursor = _encode_cursor(
                    cast(datetime, final["updated_at"]), cast(str, final["command_id"])
                )
            return Success(CursorPage(tuple(records), next_cursor))
        except psycopg.Error as error:
            return _database_failure(error, "VALIDATE")


class PostgresEventStore:
    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    @staticmethod
    def _authorized_event(event: UncommittedEvent, context: OperationContext) -> bool:
        return (
            event.content_ref == context.content_ref
            and event.command_id == context.command_id
            and event.trace_id == context.trace_id
            and event.correlation_id == context.correlation_id
        )

    async def _command_is_authorized(
        self,
        command_id: str,
        context: OperationContext,
    ) -> bool:
        connection = await self._database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT record_json FROM yaya_commands
                WHERE tenant_id=%s AND command_id=%s AND actor_id=%s AND content_hash=%s
                """,
                (
                    context.actor.tenant_id,
                    command_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                ),
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            return False
        record = decode_as(row["record_json"], CommandRecord)
        return record.command_id == command_id and _same_authority(record, context)

    async def append(
        self,
        stream_id: str,
        expected_sequence: int | str,
        events: tuple[UncommittedEvent, ...],
        context: OperationContext,
    ) -> Result[EventAppendReceipt]:
        if stream_id.startswith(("learner:", "learner-model:")):
            return _failure(
                "INVARIANT_VIOLATION",
                "WORLD_COMMIT",
                "Learner source and derived streams may only be written through "
                "AgentTurnCommitPort or the fenced LearnerPort projector",
            )
        if any(not self._authorized_event(event, context) for event in events):
            return _failure("AUTHORIZATION_DENIED", "WORLD_COMMIT", "Event authority mismatch")
        try:
            if not await self._command_is_authorized(context.command_id, context):
                return _failure(
                    "AUTHORIZATION_DENIED",
                    "WORLD_COMMIT",
                    "Event Command is outside actor/content authority",
                )
        except psycopg.Error as error:
            return _database_failure(error, "WORLD_COMMIT")
        try:
            async with _store_transaction(self._database) as connection:
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"{context.actor.tenant_id}:{stream_id}",),
                )
                owned = await connection.execute(
                    """
                    SELECT 1 FROM yaya_worlds
                    WHERE tenant_id=%s AND stream_id=%s FOR KEY SHARE
                    """,
                    (context.actor.tenant_id, stream_id),
                )
                if await owned.fetchone() is not None:
                    return _failure(
                        "INVARIANT_VIOLATION",
                        "WORLD_COMMIT",
                        "World-owned streams may only be written through WorldUnitOfWorkPort",
                    )
                cursor = await connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence),0) AS value FROM yaya_events
                    WHERE tenant_id=%s AND stream_id=%s
                    """,
                    (context.actor.tenant_id, stream_id),
                )
                row = await cursor.fetchone()
                current = 0 if row is None else cast(int, row["value"])
                expected = 0 if expected_sequence == "NO_STREAM" else expected_sequence
                if (
                    not isinstance(expected, int)
                    or isinstance(expected, bool)
                    or expected != current
                ):
                    return _failure(
                        "EVENT_SEQUENCE_GAP",
                        "WORLD_COMMIT",
                        f"Expected stream sequence {expected!r}, found {current}",
                    )
                committed: list[DomainEvent[Mapping[str, object]]] = []
                for offset, item in enumerate(events, start=1):
                    clock = await connection.execute("SELECT clock_timestamp() AS value")
                    clock_row = await clock.fetchone()
                    if clock_row is None:
                        raise RuntimeError("PostgreSQL clock query returned no row")
                    event = DomainEvent(
                        event_id=f"evt_{uuid.uuid4().hex}",
                        event_type=item.event_type,
                        event_version=item.event_version,
                        stream_id=stream_id,
                        sequence=current + offset,
                        occurred_at=cast(datetime, clock_row["value"]),
                        producer=item.producer,
                        trace_id=item.trace_id,
                        command_id=item.command_id,
                        correlation_id=item.correlation_id,
                        causation_id=item.causation_id,
                        content_ref=item.content_ref,
                        payload=item.payload,
                        schema_version=item.schema_version,
                    )
                    await connection.execute(
                        """
                        INSERT INTO yaya_events(
                            tenant_id,event_id,stream_id,sequence,event_type,event_json,occurred_at
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            context.actor.tenant_id,
                            event.event_id,
                            stream_id,
                            event.sequence,
                            event.event_type,
                            Jsonb(encode(event)),
                            event.occurred_at,
                        ),
                    )
                    committed.append(event)
                return Success(
                    EventAppendReceipt(
                        stream_id=stream_id,
                        previous_sequence=current,
                        next_sequence=current + len(committed),
                        events=tuple(committed),
                    )
                )
        except psycopg.Error as error:
            return _database_failure(error, "WORLD_COMMIT")

    async def read_stream(
        self,
        stream_id: str,
        after_sequence: int,
        limit: int,
        context: OperationContext,
    ) -> Result[CursorPage[DomainEvent[Mapping[str, object]]]]:
        if after_sequence < 0 or not 1 <= limit <= 1000:
            return _failure("INVARIANT_VIOLATION", "VALIDATE", "invalid stream page bounds")
        try:
            connection = await self._database.connect(autocommit=True)
            try:
                cursor = await connection.execute(
                    """
                    SELECT event_json FROM yaya_events
                    WHERE tenant_id=%s AND stream_id=%s AND sequence>%s
                    ORDER BY sequence LIMIT %s
                    """,
                    (context.actor.tenant_id, stream_id, after_sequence, limit + 1),
                )
                rows = list(await cursor.fetchall())
            finally:
                await connection.close()
            items: list[DomainEvent[Mapping[str, object]]] = []
            for row in rows[:limit]:
                value = decode(row["event_json"])
                if not isinstance(value, DomainEvent):
                    return _failure("INVARIANT_VIOLATION", "VALIDATE", "Stored event is invalid")
                event = cast(DomainEvent[Mapping[str, object]], value)
                if event.content_ref != context.content_ref:
                    return _failure("CONTENT_VERSION_MISMATCH", "VALIDATE", "Event content differs")
                if not await self._command_is_authorized(event.command_id, context):
                    return _failure("NOT_FOUND", "VALIDATE", "Event not found")
                items.append(event)
            next_cursor = str(items[-1].sequence) if len(rows) > limit and items else None
            return Success(CursorPage(tuple(items), next_cursor))
        except psycopg.Error as error:
            return _database_failure(error, "VALIDATE")

    async def get_by_id(
        self,
        event_id: str,
        context: OperationContext,
    ) -> Result[DomainEvent[Mapping[str, object]]]:
        try:
            connection = await self._database.connect(autocommit=True)
            try:
                cursor = await connection.execute(
                    "SELECT event_json FROM yaya_events WHERE tenant_id=%s AND event_id=%s",
                    (context.actor.tenant_id, event_id),
                )
                row = await cursor.fetchone()
            finally:
                await connection.close()
            if row is None:
                return _failure("NOT_FOUND", "VALIDATE", "Event not found")
            value = decode(row["event_json"])
            if not isinstance(value, DomainEvent):
                return _failure("INVARIANT_VIOLATION", "VALIDATE", "Stored event is invalid")
            event = cast(DomainEvent[Mapping[str, object]], value)
            if event.content_ref != context.content_ref:
                return _failure("NOT_FOUND", "VALIDATE", "Event not found")
            if not await self._command_is_authorized(event.command_id, context):
                return _failure("NOT_FOUND", "VALIDATE", "Event not found")
            return Success(event)
        except psycopg.Error as error:
            return _database_failure(error, "VALIDATE")


class PostgresOutboxStore:
    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    async def enqueue(
        self,
        message: OutboxMessage,
        context: OperationContext,
    ) -> Result[OutboxMessage]:
        if not isinstance(message, OutboxMessage):
            return _failure(
                "INVARIANT_VIOLATION",
                "COMPLETE",
                "Outbox writes require the closed contract DTO",
            )
        if message.operation_context != context:
            return _failure("AUTHORIZATION_DENIED", "COMPLETE", "Outbox origin was spoofed")
        try:
            async with _store_transaction(self._database) as connection:
                cursor = await connection.execute(
                    """
                    SELECT payload_sha256,message_json FROM yaya_outbox
                    WHERE tenant_id=%s AND destination=%s AND idempotency_key=%s FOR UPDATE
                    """,
                    message.idempotency_scope,
                )
                row = await cursor.fetchone()
                if row is not None:
                    if row["payload_sha256"] != message.payload_sha256:
                        return _failure(
                            "IDEMPOTENCY_KEY_REUSED",
                            "COMPLETE",
                            "Outbox key was used for a different payload",
                        )
                    return Success(decode_as(row["message_json"], OutboxMessage))
                await connection.execute(
                    """
                    INSERT INTO yaya_outbox(
                        tenant_id,message_id,destination,idempotency_key,payload_sha256,
                        status,attempt,message_json,created_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        context.actor.tenant_id,
                        message.message_id,
                        message.destination,
                        message.idempotency_key,
                        message.payload_sha256,
                        message.status.value,
                        message.attempt,
                        Jsonb(encode(message)),
                        message.created_at,
                    ),
                )
                return Success(message)
        except psycopg.errors.UniqueViolation:
            return _failure("IDEMPOTENCY_KEY_REUSED", "COMPLETE", "Outbox identity conflicts")
        except psycopg.Error as error:
            return _database_failure(error, "COMPLETE")

    async def claim_ready(
        self,
        worker_id: str,
        limit: int,
        lease_seconds: int,
        context: OperationContext,
    ) -> Result[tuple[OutboxMessage, ...]]:
        if not worker_id or not 1 <= limit <= 1000 or lease_seconds < 1:
            return _failure("INVARIANT_VIOLATION", "COMPLETE", "invalid outbox lease request")
        try:
            async with _store_transaction(self._database) as connection:
                cursor = await connection.execute(
                    """
                    SELECT message_id,message_json FROM yaya_outbox
                    WHERE tenant_id=%s AND destination='FEISHU_REPORT_DRAFT' AND (
                      status='PENDING'
                      OR (status='RETRYING' AND next_attempt_at<=clock_timestamp())
                      OR (status='SENDING' AND lease_expires_at<=clock_timestamp())
                    )
                    ORDER BY created_at,message_id FOR UPDATE SKIP LOCKED LIMIT %s
                    """,
                    (context.actor.tenant_id, limit),
                )
                rows = list(await cursor.fetchall())
                claimed: list[OutboxMessage] = []
                for row in rows:
                    current = decode_as(row["message_json"], OutboxMessage)
                    attempt = current.attempt + 1
                    lease_id = f"lease_{uuid.uuid4().hex}"
                    clock = await connection.execute(
                        """
                        SELECT clock_timestamp() AS now,
                               clock_timestamp() + %s * interval '1 second' AS expires
                        """,
                        (lease_seconds,),
                    )
                    clock_row = await clock.fetchone()
                    if clock_row is None:
                        raise RuntimeError("PostgreSQL clock query returned no row")
                    payload = replace(current.payload, attempt=attempt)
                    next_message = replace(
                        current,
                        payload=payload,
                        status=OutboxStatus.SENDING,
                        attempt=attempt,
                        next_attempt_at=None,
                        lease_id=lease_id,
                        lease_expires_at=cast(datetime, clock_row["expires"]),
                        last_error=None,
                        delivery_receipt=None,
                        dead_lettered_at=None,
                    )
                    await self._update_outbox(connection, next_message)
                    claimed.append(next_message)
                return Success(tuple(claimed))
        except psycopg.Error as error:
            return _database_failure(error, "COMPLETE")

    async def mark_sent(
        self,
        message_id: str,
        lease_id: str,
        receipt: DeliveryReceipt,
        context: OperationContext,
    ) -> Result[OutboxMessage]:
        return await self._finish(
            message_id,
            lease_id,
            context,
            lambda current: replace(
                current,
                status=OutboxStatus.SENT,
                lease_id=None,
                lease_expires_at=None,
                delivery_receipt=receipt,
            ),
        )

    async def mark_retry(
        self,
        message_id: str,
        lease_id: str,
        error: ContractError,
        next_attempt_at: datetime,
        context: OperationContext,
    ) -> Result[OutboxMessage]:
        return await self._finish(
            message_id,
            lease_id,
            context,
            lambda current: replace(
                current,
                status=OutboxStatus.RETRYING,
                next_attempt_at=next_attempt_at,
                lease_id=None,
                lease_expires_at=None,
                last_error=error,
            ),
        )

    async def mark_dead_letter(
        self,
        message_id: str,
        lease_id: str,
        error: ContractError,
        dead_lettered_at: datetime,
        context: OperationContext,
    ) -> Result[OutboxMessage]:
        return await self._finish(
            message_id,
            lease_id,
            context,
            lambda current: replace(
                current,
                status=OutboxStatus.DEAD_LETTER,
                lease_id=None,
                lease_expires_at=None,
                last_error=error,
                dead_lettered_at=dead_lettered_at,
            ),
        )

    async def _finish(
        self,
        message_id: str,
        lease_id: str,
        context: OperationContext,
        transition: Callable[[OutboxMessage], OutboxMessage],
    ) -> Result[OutboxMessage]:
        try:
            async with _store_transaction(self._database) as connection:
                cursor = await connection.execute(
                    """
                    SELECT message_json,clock_timestamp() AS now FROM yaya_outbox
                    WHERE tenant_id=%s AND message_id=%s FOR UPDATE
                    """,
                    (context.actor.tenant_id, message_id),
                )
                row = await cursor.fetchone()
                if row is None:
                    return _failure("NOT_FOUND", "COMPLETE", "Outbox message not found")
                current = decode_as(row["message_json"], OutboxMessage)
                if (
                    current.status is not OutboxStatus.SENDING
                    or current.lease_id != lease_id
                    or current.lease_expires_at is None
                    or current.lease_expires_at <= cast(datetime, row["now"])
                ):
                    return _failure("EVENT_SEQUENCE_GAP", "COMPLETE", "Outbox lease was lost")
                next_message = transition(current)
                await self._update_outbox(connection, next_message)
                return Success(next_message)
        except (TypeError, ValueError) as error:
            return _failure("INVARIANT_VIOLATION", "COMPLETE", str(error))
        except psycopg.Error as error:
            return _database_failure(error, "COMPLETE")

    @staticmethod
    async def _update_outbox(connection: _Connection, message: OutboxMessage) -> None:
        await connection.execute(
            """
            UPDATE yaya_outbox SET status=%s,attempt=%s,lease_id=%s,lease_expires_at=%s,
                next_attempt_at=%s,last_error_json=%s,receipt_json=%s,message_json=%s,
                updated_at=clock_timestamp()
            WHERE tenant_id=%s AND message_id=%s
            """,
            (
                message.status.value,
                message.attempt,
                message.lease_id,
                message.lease_expires_at,
                message.next_attempt_at,
                Jsonb(encode(message.last_error)) if message.last_error is not None else None,
                (
                    Jsonb(encode(message.delivery_receipt))
                    if message.delivery_receipt is not None
                    else None
                ),
                Jsonb(encode(message)),
                message.operation_context.actor.tenant_id,
                message.message_id,
            ),
        )


class PostgresAuditStore:
    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    async def append(
        self,
        record: AuditRecord,
        context: OperationContext,
    ) -> Result[AuditRecord]:
        if not _same_actor(record.actor, context.actor):
            return _failure("AUTHORIZATION_DENIED", "COMPLETE", "Audit actor was spoofed")
        try:
            async with _store_transaction(self._database) as connection:
                cursor = await connection.execute(
                    "SELECT record_json FROM yaya_audit WHERE tenant_id=%s AND audit_id=%s",
                    (context.actor.tenant_id, record.audit_id),
                )
                row = await cursor.fetchone()
                if row is not None:
                    existing = decode_as(row["record_json"], AuditRecord)
                    if existing == record:
                        return Success(existing)
                    return _failure("INVARIANT_VIOLATION", "COMPLETE", "Audit ID was reused")
                await connection.execute(
                    """
                    INSERT INTO yaya_audit(
                        tenant_id,audit_id,actor_id,operation,outcome,occurred_at,record_json
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        context.actor.tenant_id,
                        record.audit_id,
                        context.actor.actor_id,
                        record.operation,
                        record.outcome,
                        record.occurred_at,
                        Jsonb(encode(record)),
                    ),
                )
                return Success(record)
        except psycopg.Error as error:
            return _database_failure(error, "COMPLETE")

    async def query(
        self,
        query: AuditQuery,
        context: OperationContext,
    ) -> Result[CursorPage[AuditRecord]]:
        try:
            after = _decode_cursor(query.cursor) if query.cursor else None
        except ValueError as error:
            return _failure("INVARIANT_VIOLATION", "VALIDATE", str(error))
        clauses = ["tenant_id=%s", "actor_id=%s"]
        parameters: list[object] = [context.actor.tenant_id, context.actor.actor_id]
        if query.operations:
            clauses.append("operation=ANY(%s)")
            parameters.append(list(query.operations))
        if query.outcomes:
            clauses.append("outcome=ANY(%s)")
            parameters.append(list(query.outcomes))
        if query.occurred_after is not None:
            clauses.append("occurred_at>%s")
            parameters.append(query.occurred_after)
        if query.occurred_before is not None:
            clauses.append("occurred_at<%s")
            parameters.append(query.occurred_before)
        if after is not None:
            clauses.append("(occurred_at,audit_id)>(%s,%s)")
            parameters.extend(after)
        parameters.append(query.limit + 1)
        try:
            connection = await self._database.connect(autocommit=True)
            try:
                query_text = cast(
                    LiteralString,
                    f"""
                    SELECT occurred_at,audit_id,record_json FROM yaya_audit
                    WHERE {" AND ".join(clauses)}
                    ORDER BY occurred_at,audit_id LIMIT %s
                    """,
                )
                cursor = await connection.execute(query_text, tuple(parameters))
                rows = list(await cursor.fetchall())
            finally:
                await connection.close()
            page = rows[: query.limit]
            records = tuple(decode_as(row["record_json"], AuditRecord) for row in page)
            if any(not _same_actor(record.actor, context.actor) for record in records):
                return _failure("AUTHORIZATION_DENIED", "VALIDATE", "Audit authority drift")
            next_cursor = None
            if len(rows) > query.limit and page:
                final = page[-1]
                next_cursor = _encode_cursor(
                    cast(datetime, final["occurred_at"]), cast(str, final["audit_id"])
                )
            return Success(CursorPage(records, next_cursor))
        except psycopg.Error as error:
            return _database_failure(error, "VALIDATE")


class PostgresRegistryStore:
    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    async def _resolve_build(
        self,
        evidence: CertificationEvidence,
        context: OperationContext,
    ) -> Result[tuple[CompileResultSnapshot, SkillSnapshot]]:
        try:
            connection = await self._database.connect(autocommit=True)
            try:
                cursor = await connection.execute(
                    """
                    SELECT snapshot_json FROM yaya_compile_results
                    WHERE tenant_id=%s AND build_id=%s AND actor_id=%s AND content_hash=%s
                    """,
                    (
                        context.actor.tenant_id,
                        evidence.build_id,
                        context.actor.actor_id,
                        context.content_ref.content_hash,
                    ),
                )
                compile_row = await cursor.fetchone()
                if compile_row is None:
                    return _failure("NOT_FOUND", "REGISTRY", "Compile result not found")
                result = decode_as(compile_row["snapshot_json"], CompileResultSnapshot)
                cursor = await connection.execute(
                    """
                    SELECT snapshot_json FROM yaya_skills
                    WHERE tenant_id=%s AND skill_version_id=%s AND actor_id=%s
                      AND content_hash=%s AND artifact_sha256=%s
                    """,
                    (
                        context.actor.tenant_id,
                        result.skill_ref.skill_version_id,
                        context.actor.actor_id,
                        context.content_ref.content_hash,
                        evidence.artifact.artifact_sha256,
                    ),
                )
                skill_row = await cursor.fetchone()
            finally:
                await connection.close()
            if skill_row is None:
                return _failure("NOT_FOUND", "REGISTRY", "Skill build binding not found")
            return Success((result, decode_as(skill_row["snapshot_json"], SkillSnapshot)))
        except psycopg.Error as error:
            return _database_failure(error, "REGISTRY")

    async def certify(
        self,
        evidence: CertificationEvidence,
        context: OperationContext,
    ) -> Result[CertifiedSkill]:
        if not evidence.all_required_tests_passed:
            return _failure("SKILL_NOT_CERTIFIED", "REGISTRY", "Required tests did not pass")
        resolved = await self._resolve_build(evidence, context)
        if isinstance(resolved, Failure):
            return resolved
        compile_result, skill = resolved.value
        if (
            not compile_result.succeeded
            or compile_result.skill_ref != skill.ref
            or skill.ref.artifact_sha256 != evidence.artifact.artifact_sha256
        ):
            return _failure(
                "ACTIVE_SKILL_ARTIFACT_MISMATCH", "REGISTRY", "Certification binding drift"
            )
        raw_metadata = skill.parameter_schema.get("x-yaya-certification")
        if not isinstance(raw_metadata, Mapping):
            return _failure(
                "INVARIANT_VIOLATION",
                "REGISTRY",
                "Skill snapshot lacks x-yaya-certification metadata",
            )
        metadata = cast(Mapping[str, object], raw_metadata)
        semantic_version = metadata.get("semantic_version")
        raw_capabilities = metadata.get("capabilities")
        if (
            not isinstance(semantic_version, str)
            or not isinstance(raw_capabilities, Sequence)
            or isinstance(raw_capabilities, (str, bytes, bytearray))
        ):
            return _failure(
                "INVARIANT_VIOLATION",
                "REGISTRY",
                "Skill certification metadata is incomplete",
            )
        capability_values = cast(Sequence[object], raw_capabilities)
        if any(not isinstance(item, str) for item in capability_values):
            return _failure(
                "INVARIANT_VIOLATION",
                "REGISTRY",
                "Skill certification capabilities must be strings",
            )
        capabilities = tuple(cast(str, item) for item in capability_values)
        evidence_sha256 = canonical_json_sha256(cast(Mapping[str, object], plain(evidence)))
        try:
            certified = CertifiedSkill(
                certification_id=skill.ref.certification_id,
                skill_id=skill.ref.skill_id,
                skill_version_id=skill.ref.skill_version_id,
                semantic_version=semantic_version,
                artifact=evidence.artifact,
                capabilities=capabilities,
                certified_at=max(item.created_at for item in evidence.evidence_refs)
                if evidence.evidence_refs
                else context.requested_at,
                revoked_at=None,
                metadata={
                    "build_id": evidence.build_id,
                    "certification_evidence_sha256": evidence_sha256,
                },
            )
        except (TypeError, ValueError) as error:
            return _failure("INVARIANT_VIOLATION", "REGISTRY", str(error))
        try:
            async with _store_transaction(self._database) as connection:
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"certification:{context.actor.tenant_id}:{certified.certification_id}",),
                )
                cursor = await connection.execute(
                    """
                    SELECT record_json,rejected FROM yaya_registry_certifications
                    WHERE tenant_id=%s AND certification_id=%s FOR UPDATE
                    """,
                    (context.actor.tenant_id, certified.certification_id),
                )
                row = await cursor.fetchone()
                if row is not None:
                    if cast(bool, row["rejected"]):
                        return _failure("SKILL_NOT_CERTIFIED", "REGISTRY", "Build was rejected")
                    existing = decode_as(row["record_json"], CertifiedSkill)
                    if (
                        existing.skill_id != certified.skill_id
                        or existing.skill_version_id != certified.skill_version_id
                        or existing.artifact != certified.artifact
                        or existing.metadata.get("certification_evidence_sha256") != evidence_sha256
                    ):
                        return _failure(
                            "INVARIANT_VIOLATION", "REGISTRY", "Certification ID was reused"
                        )
                    return Success(existing)
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
                return Success(certified)
        except psycopg.Error as error:
            return _database_failure(error, "REGISTRY")

    async def reject_certification(
        self,
        evidence: CertificationEvidence,
        reason: ContractError,
        context: OperationContext,
    ) -> Result[None]:
        resolved = await self._resolve_build(evidence, context)
        if isinstance(resolved, Failure):
            return resolved
        _, skill = resolved.value
        rejection_sha256 = canonical_json_sha256(
            {
                "evidence": plain(evidence),
                "reason": plain(reason),
            }
        )
        payload = {
            "evidence": encode(evidence),
            "reason": encode(reason),
            "rejection_sha256": rejection_sha256,
        }
        try:
            async with _store_transaction(self._database) as connection:
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"certification:{context.actor.tenant_id}:{skill.ref.certification_id}",),
                )
                cursor = await connection.execute(
                    """
                    SELECT rejected,record_json FROM yaya_registry_certifications
                    WHERE tenant_id=%s AND certification_id=%s FOR UPDATE
                    """,
                    (context.actor.tenant_id, skill.ref.certification_id),
                )
                existing_row = await cursor.fetchone()
                if existing_row is not None:
                    if not cast(bool, existing_row["rejected"]):
                        return _failure(
                            "INVARIANT_VIOLATION",
                            "REGISTRY",
                            "A granted certification cannot be overwritten by rejection",
                        )
                    existing = decode(existing_row["record_json"])
                    if not isinstance(existing, Mapping) or (
                        cast(Mapping[str, object], existing).get("rejection_sha256")
                        != rejection_sha256
                    ):
                        return _failure(
                            "IDEMPOTENCY_KEY_REUSED",
                            "REGISTRY",
                            "Certification rejection identity was reused with different payload",
                        )
                    return Success(None)
                await connection.execute(
                    """
                    INSERT INTO yaya_registry_certifications(
                        tenant_id,certification_id,skill_id,skill_version_id,
                        artifact_sha256,record_json,rejected
                    ) VALUES (%s,%s,%s,%s,%s,%s,TRUE)
                    """,
                    (
                        context.actor.tenant_id,
                        skill.ref.certification_id,
                        skill.ref.skill_id,
                        skill.ref.skill_version_id,
                        skill.ref.artifact_sha256,
                        Jsonb(payload),
                    ),
                )
                return Success(None)
        except psycopg.Error as error:
            return _database_failure(error, "REGISTRY")

    async def get_certified_version(
        self,
        ref: SkillRef,
        context: OperationContext,
    ) -> Result[CertifiedSkill]:
        try:
            connection = await self._database.connect(autocommit=True)
            try:
                cursor = await connection.execute(
                    """
                    SELECT record_json,rejected FROM yaya_registry_certifications
                    WHERE tenant_id=%s AND certification_id=%s AND skill_id=%s
                      AND skill_version_id=%s AND artifact_sha256=%s
                    """,
                    (
                        context.actor.tenant_id,
                        ref.certification_id,
                        ref.skill_id,
                        ref.skill_version_id,
                        ref.artifact_sha256,
                    ),
                )
                row = await cursor.fetchone()
            finally:
                await connection.close()
            if row is None or cast(bool, row["rejected"]):
                return _failure("SKILL_NOT_CERTIFIED", "REGISTRY", "Skill is not certified")
            return Success(decode_as(row["record_json"], CertifiedSkill))
        except psycopg.Error as error:
            return _database_failure(error, "REGISTRY")

    async def get_active_skill(
        self,
        skill_id: str,
        context: OperationContext,
    ) -> Result[ActiveSkill]:
        try:
            connection = await self._database.connect(autocommit=True)
            try:
                cursor = await connection.execute(
                    """
                    SELECT record_json FROM yaya_registry_active
                    WHERE tenant_id=%s AND actor_id=%s AND skill_id=%s
                    """,
                    (context.actor.tenant_id, context.actor.actor_id, skill_id),
                )
                row = await cursor.fetchone()
            finally:
                await connection.close()
            if row is None:
                return _failure("NOT_FOUND", "REGISTRY", "Active Skill not found")
            return Success(decode_as(row["record_json"], ActiveSkill))
        except psycopg.Error as error:
            return _database_failure(error, "REGISTRY")

    async def activate(
        self,
        request: ActivateSkillInput,
        context: OperationContext,
    ) -> Result[ActiveSkill]:
        try:
            async with _store_transaction(self._database) as connection:
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"registry:{context.actor.tenant_id}:{context.actor.actor_id}",),
                )
                cursor = await connection.execute(
                    """
                    SELECT record_json,rejected FROM yaya_registry_certifications
                    WHERE tenant_id=%s AND certification_id=%s AND skill_version_id=%s
                      AND artifact_sha256=%s FOR UPDATE
                    """,
                    (
                        context.actor.tenant_id,
                        request.certification_id,
                        request.skill_version_id,
                        request.artifact_sha256,
                    ),
                )
                certification = await cursor.fetchone()
                if certification is None or cast(bool, certification["rejected"]):
                    return _failure("SKILL_NOT_CERTIFIED", "REGISTRY", "Skill is not certified")
                skill = decode_as(certification["record_json"], CertifiedSkill)
                cursor = await connection.execute(
                    """
                    SELECT COALESCE(MAX(revision),0) AS value FROM yaya_registry_active
                    WHERE tenant_id=%s AND actor_id=%s
                    """,
                    (context.actor.tenant_id, context.actor.actor_id),
                )
                revision_row = await cursor.fetchone()
                current = 0 if revision_row is None else cast(int, revision_row["value"])
                if current != request.expected_registry_revision:
                    return _failure(
                        "EVENT_SEQUENCE_GAP", "REGISTRY", "Registry CAS revision has changed"
                    )
                clock = await connection.execute("SELECT clock_timestamp() AS value")
                clock_row = await clock.fetchone()
                if clock_row is None:
                    raise RuntimeError("PostgreSQL clock query returned no row")
                active = ActiveSkill(
                    skill=skill,
                    registry_revision=current + 1,
                    activated_at=cast(datetime, clock_row["value"]),
                )
                await connection.execute(
                    """
                    INSERT INTO yaya_registry_active(
                        tenant_id,actor_id,skill_id,record_json,revision
                    ) VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT (tenant_id,actor_id,skill_id) DO UPDATE
                    SET record_json=EXCLUDED.record_json,revision=EXCLUDED.revision
                    """,
                    (
                        context.actor.tenant_id,
                        context.actor.actor_id,
                        skill.skill_id,
                        Jsonb(encode(active)),
                        active.registry_revision,
                    ),
                )
                return Success(active)
        except psycopg.Error as error:
            return _database_failure(error, "REGISTRY")

    async def snapshot(self, context: OperationContext) -> Result[RegistrySnapshot]:
        try:
            connection = await self._database.connect(autocommit=True)
            try:
                cursor = await connection.execute(
                    """
                    SELECT revision,record_json FROM yaya_registry_active
                    WHERE tenant_id=%s AND actor_id=%s ORDER BY skill_id
                    """,
                    (context.actor.tenant_id, context.actor.actor_id),
                )
                rows = list(await cursor.fetchall())
            finally:
                await connection.close()
            skills = tuple(decode_as(row["record_json"], ActiveSkill) for row in rows)
            revision = max((skill.registry_revision for skill in skills), default=0)
            return Success(RegistrySnapshot(revision=revision, skills=skills))
        except psycopg.Error as error:
            return _database_failure(error, "REGISTRY")


@dataclass(frozen=True, slots=True)
class _LearnerProjectionFacts:
    record: CommittedAgentTurn
    task: TaskSnapshot
    run: RunResultSnapshot | None
    evidence_refs: tuple[EvidenceRef, ...]


def _request_authority_matches(value: object, context: OperationContext) -> bool:
    actor = getattr(value, "actor", None)
    content_ref = getattr(value, "content_ref", None)
    return _same_actor(actor, context.actor) and content_ref == context.content_ref


def _learner_evidence_wire(evidence: EvidenceRef) -> dict[str, object]:
    value: dict[str, object] = {
        "evidence_id": evidence.evidence_id,
        "evidence_type": evidence.evidence_type.value,
        "created_at": plain(evidence.created_at),
    }
    if evidence.sha256 is not None:
        value["sha256"] = evidence.sha256
    if evidence.uri is not None:
        value["uri"] = evidence.uri
    return value


def _contract_error_wire(error: ContractError) -> dict[str, object]:
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


def _learner_identifier(prefix: str, seed: Mapping[str, object]) -> str:
    return f"{prefix}_{canonical_json_sha256(seed)[:32]}"


class PostgresLearnerStore:
    """Fenced, source-event-only learner projection adapter.

    The public ``LearnerPort.project`` method deliberately cannot mutate the
    model without the durable Job lease.  Production callers use
    ``project_fenced`` through ``LearnerProjectionWorker``; ``rebuild`` is the
    separate administrative replay path and never emits receipts or Outbox.
    """

    _MODEL_VERSION = LEARNER_PROJECTION_POLICY_VERSION

    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database
        self._policy = LearnerProjectionPolicy()

    @staticmethod
    def _source_stream_id(learner_id: str) -> str:
        return f"learner:{learner_id}"

    @staticmethod
    def _derived_stream_id(learner_id: str) -> str:
        return f"learner-model:{learner_id}"

    @staticmethod
    async def _lock_learner(
        connection: _Connection,
        tenant_id: str,
        learner_id: str,
    ) -> None:
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"learner:{tenant_id}:{learner_id}",),
        )

    @staticmethod
    async def _next_derived_sequence(
        connection: _Connection,
        tenant_id: str,
        learner_id: str,
    ) -> int:
        stream_id = PostgresLearnerStore._derived_stream_id(learner_id)
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"{tenant_id}:{stream_id}",),
        )
        cursor = await connection.execute(
            """
            SELECT COALESCE(MAX(sequence),0)+1 AS value
            FROM yaya_events WHERE tenant_id=%s AND stream_id=%s
            """,
            (tenant_id, stream_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("learner derived stream sequence query returned no row")
        return cast(int, row["value"])

    @staticmethod
    def _runtime_event(value: object) -> RuntimeEvent:
        if isinstance(value, RuntimeEvent):
            return value
        if not isinstance(value, DomainEvent):
            raise ValueError("learner stream contains a non-event record")
        domain_event = cast(DomainEvent[Mapping[str, object]], value)
        return RuntimeEvent(
            event_id=domain_event.event_id,
            event_type=RuntimeEventType(domain_event.event_type),
            event_version=domain_event.event_version,
            stream_id=domain_event.stream_id,
            sequence=domain_event.sequence,
            occurred_at=domain_event.occurred_at,
            producer=domain_event.producer,
            trace_id=domain_event.trace_id,
            command_id=domain_event.command_id,
            correlation_id=domain_event.correlation_id,
            causation_id=domain_event.causation_id,
            content_ref=domain_event.content_ref,
            payload=domain_event.payload,
            schema_version=domain_event.schema_version,
        )

    @staticmethod
    def _empty_snapshot(learner_id: str, at: datetime) -> LearnerModelSnapshot:
        return LearnerModelSnapshot(
            learner_id=learner_id,
            revision=0,
            model_version=LEARNER_PROJECTION_POLICY_VERSION,
            projected_through_sequence=0,
            competencies={},
            updated_at=at,
            evidence_refs=(),
        )

    @staticmethod
    def _competencies(
        snapshot: LearnerModelSnapshot,
    ) -> tuple[dict[str, CompetencyProjection], dict[str, object]]:
        return validated_learner_competencies(snapshot)

    @staticmethod
    def _model_snapshot_from_row(
        row: Mapping[str, object] | None,
        learner_id: str,
        context: OperationContext,
        *,
        empty_at: datetime,
        allow_legacy: bool = False,
    ) -> LearnerModelSnapshot:
        if row is None:
            return PostgresLearnerStore._empty_snapshot(learner_id, empty_at)
        if (
            row["actor_id"] != context.actor.actor_id
            or row["content_hash"] != context.content_ref.content_hash
        ):
            raise PermissionError("learner model crossed actor/content authority")
        request_context_json = row["request_context_json"]
        projection_policy_version = row["projection_policy_version"]
        snapshot_sha256 = row["snapshot_sha256"]
        if (
            request_context_json is None
            or projection_policy_version is None
            or snapshot_sha256 is None
        ):
            if not allow_legacy:
                raise ValueError("legacy learner model requires deterministic rebuild")
        else:
            stored_context = decode_as(request_context_json, OperationContext)
            if not _request_authority_matches(stored_context, context):
                raise PermissionError("learner model provenance crossed authority")
            if projection_policy_version != LEARNER_PROJECTION_POLICY_VERSION:
                raise ValueError("learner model projection policy version is unsupported")
        snapshot = decode_as(row["snapshot_json"], LearnerModelSnapshot)
        validate_persisted_learner_snapshot(
            snapshot,
            learner_id=learner_id,
            revision=row["revision"],
            projected_through_sequence=row["projected_through_sequence"],
            model_version=LEARNER_PROJECTION_POLICY_VERSION,
            snapshot_sha256=snapshot_sha256,
            updated_at=row["updated_at"],
        )
        return snapshot

    @staticmethod
    def _validate_fence(
        row: Mapping[str, object],
        fence: LearnerProjectionFence,
    ) -> None:
        database_now = cast(datetime, row["database_now"])
        lease_expires_at = cast(datetime | None, row["lease_expires_at"])
        if (
            row["tenant_id"] != fence.tenant_id
            or row["job_id"] != fence.job_id
            or row["state"] != "LEASED"
            or row["worker_id"] != fence.worker_id
            or row["lease_id"] != fence.lease_id
            or row["fencing_token"] != fence.fencing_token
            or lease_expires_at is None
            or lease_expires_at <= database_now
        ):
            raise LearnerProjectionFenceLost()

    async def _load_fenced_job(
        self,
        connection: _Connection,
        fence: LearnerProjectionFence,
    ) -> Mapping[str, object]:
        cursor = await connection.execute(
            """
            SELECT j.*,clock_timestamp() AS database_now
            FROM yaya_learner_projection_jobs j
            WHERE tenant_id=%s AND job_id=%s FOR UPDATE
            """,
            (fence.tenant_id, fence.job_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise LearnerProjectionFenceLost()
        self._validate_fence(row, fence)
        return row

    @staticmethod
    def _validate_job_envelope(
        job: Mapping[str, object],
        event: RuntimeEvent,
        context: OperationContext,
    ) -> None:
        persisted_event = decode_as(job["event_json"], RuntimeEvent)
        persisted_context = decode_as(job["operation_context_json"], OperationContext)
        payload = event.payload
        if not isinstance(payload["actor"], Mapping):
            raise ValueError("learner inference actor must be an object")
        if (
            persisted_event != event
            or persisted_context != context
            or event.event_type is not RuntimeEventType.LEARNER_INFERENCE_RECORDED
            or event.schema_version != "2.0.0"
            or internal_record_sha256(event) != job["event_sha256"]
            or learner_inference_sha256(payload) != payload["inference_sha256"]
            or payload["inference_sha256"] != job["inference_sha256"]
            or event.event_id != job["event_id"]
            or event.stream_id != job["source_stream_id"]
            or event.sequence != job["source_stream_sequence"]
            or event.stream_id != f"learner:{payload['learner_id']}"
            or event.command_id != job["command_id"]
            or event.command_id != context.command_id
            or event.causation_id != job["source_event_id"]
            or event.causation_id != context.causation_id
            or event.content_ref != context.content_ref
            or event.content_ref.content_hash != job["content_hash"]
            or plain(cast(Mapping[str, object], payload["actor"])) != plain(context.actor)
            or payload["learner_id"] != context.actor.actor_id
            or payload["learner_id"] != job["learner_id"]
            or payload["session_id"] != job["session_id"]
            or payload["turn_id"] != job["turn_id"]
            or payload["command_id"] != job["command_id"]
            or payload["run_id"] != job["run_id"]
            or payload["source_event_id"] != job["source_event_id"]
            or payload["source_event_sha256"] != job["source_event_sha256"]
            or payload["turn_commit_sha256"] != job["turn_commit_sha256"]
            or payload["task_id"] != job["task_id"]
            or payload["teaching_spec_version"] != job["teaching_spec_version"]
            or payload["role"] != job["role"]
        ):
            raise ValueError("learner projection Job envelope identity or hash drifted")

    async def _load_source_facts(
        self,
        connection: _Connection,
        job: Mapping[str, object],
        event: RuntimeEvent,
        context: OperationContext,
        expected_learner_revision: int,
    ) -> _LearnerProjectionFacts:
        self._validate_job_envelope(job, event, context)
        cursor = await connection.execute(
            """
            SELECT e.event_type AS durable_event_type,
                   e.event_json AS durable_event_json,
                   e.occurred_at AS durable_event_occurred_at,
                   t.actor_id AS turn_actor_id,
                   t.content_hash AS turn_content_hash,
                   t.event_sha256 AS durable_source_event_sha256,
                   t.record_json AS turn_record_json,
                   c.record_json AS command_record_json,
                   s.snapshot_json AS session_snapshot_json,
                   s.actor_id AS session_actor_id,
                   s.content_hash AS session_content_hash,
                   s.task_id AS session_task_id,s.world_id AS session_world_id,
                   p.session_id AS public_session_id,
                   p.actor_id AS public_session_actor_id,
                   p.content_hash AS public_session_content_hash,
                   p.task_id AS public_session_task_id,
                   p.world_id AS public_session_world_id,
                   q.snapshot_json AS task_snapshot_json,
                   r.actor_id AS run_actor_id,
                   r.content_hash AS run_content_hash,
                   r.session_id AS run_session_id,
                   r.turn_id AS run_turn_id,
                   r.command_id AS run_command_id,
                   r.world_id AS run_world_id,
                   r.skill_version_id AS run_skill_version_id,
                    r.failure_key AS run_failure_key,
                    r.task_success AS run_task_success,
                    r.created_at AS run_created_at,
                    r.snapshot_json AS run_snapshot_json,
                    k.skill_id AS durable_skill_id,
                    k.skill_version_id AS durable_skill_version_id,
                    k.certification_id AS durable_skill_certification_id,
                    k.actor_id AS durable_skill_actor_id,
                     k.session_id AS durable_skill_session_id,
                     k.content_hash AS durable_skill_content_hash,
                     k.artifact_sha256 AS durable_skill_artifact_sha256,
                     k.snapshot_json AS durable_skill_snapshot_json,
                     b.binding_id AS session_skill_binding_id,
                     b.session_id AS session_skill_binding_session_id,
                     b.skill_id AS session_skill_binding_skill_id,
                     b.skill_version_id AS session_skill_binding_version_id,
                     b.certification_id AS session_skill_binding_certification_id,
                     b.artifact_sha256 AS session_skill_binding_artifact_sha256,
                     b.actor_id AS session_skill_binding_actor_id,
                     b.content_hash AS session_skill_binding_content_hash,
                     b.binding_sha256 AS session_skill_binding_sha256
            FROM yaya_events e
            JOIN yaya_agent_turns t
              ON t.tenant_id=e.tenant_id AND t.event_id=%s
            JOIN yaya_commands c
              ON c.tenant_id=e.tenant_id AND c.command_id=%s
            JOIN yaya_agent_sessions s
              ON s.tenant_id=e.tenant_id AND s.session_id=%s
            LEFT JOIN yaya_public_agent_sessions p
              ON p.tenant_id=s.tenant_id AND p.session_id=s.session_id
            JOIN yaya_tasks q
              ON q.tenant_id=e.tenant_id AND q.task_id=%s
            LEFT JOIN yaya_runs r
              ON r.tenant_id=e.tenant_id AND r.run_id=%s
            LEFT JOIN yaya_skills k
              ON k.tenant_id=r.tenant_id
             AND k.skill_version_id=r.skill_version_id
             AND k.actor_id=r.actor_id
             AND k.content_hash=r.content_hash
            LEFT JOIN yaya_session_skill_versions b
              ON b.tenant_id=k.tenant_id
             AND b.session_id=r.session_id
             AND b.skill_id=k.skill_id
             AND b.skill_version_id=k.skill_version_id
             AND b.certification_id=k.certification_id
             AND b.artifact_sha256=k.artifact_sha256
             AND b.actor_id=k.actor_id
             AND b.content_hash=k.content_hash
            WHERE e.tenant_id=%s AND e.event_id=%s
              AND e.stream_id=%s AND e.sequence=%s
            """,
            (
                job["source_event_id"],
                job["command_id"],
                job["session_id"],
                job["task_id"],
                job["run_id"],
                job["tenant_id"],
                job["event_id"],
                job["source_stream_id"],
                job["source_stream_sequence"],
            ),
        )
        source = await cursor.fetchone()
        if source is None:
            raise ValueError("learner projection durable source graph is incomplete")
        durable_event = decode_as(source["durable_event_json"], RuntimeEvent)
        turn_record_json = source["turn_record_json"]
        if not isinstance(turn_record_json, Mapping):
            raise ValueError("learner projection source turn is not committed")
        turn_record_mapping = cast(Mapping[str, object], turn_record_json)
        record = decode_as(turn_record_mapping, CommittedAgentTurn)
        command = decode_as(source["command_record_json"], CommandRecord)
        session = decode_as(source["session_snapshot_json"], SessionSnapshot)
        task = decode_as(source["task_snapshot_json"], TaskSnapshot)
        run = (
            None
            if source["run_snapshot_json"] is None
            else decode_as(source["run_snapshot_json"], RunResultSnapshot)
        )
        skill = (
            None
            if source["durable_skill_snapshot_json"] is None
            else decode_as(source["durable_skill_snapshot_json"], SkillSnapshot)
        )
        compile_row: Mapping[str, object] | None = None
        compile_result: CompileResultSnapshot | None = None
        compile_skill_row: Mapping[str, object] | None = None
        compile_skill: SkillSnapshot | None = None
        if run is None and record.event.event_type == "compile_failed":
            if record.event.build_id is None or record.event.skill_ref is None:
                raise ValueError("compile-failed source lacks Build or Skill identity")
            compile_cursor = await connection.execute(
                """
                SELECT actor_id,content_hash,snapshot_json
                FROM yaya_compile_results
                WHERE tenant_id=%s AND build_id=%s
                """,
                (job["tenant_id"], record.event.build_id),
            )
            compile_row = await compile_cursor.fetchone()
            if compile_row is not None:
                compile_result = decode_as(compile_row["snapshot_json"], CompileResultSnapshot)
                compile_skill_cursor = await connection.execute(
                    """
                    SELECT skill_id,skill_version_id,certification_id,actor_id,
                           session_id,content_hash,artifact_sha256,snapshot_json
                    FROM yaya_skills
                    WHERE tenant_id=%s AND skill_version_id=%s
                    """,
                    (job["tenant_id"], compile_result.skill_ref.skill_version_id),
                )
                compile_skill_row = await compile_skill_cursor.fetchone()
                if compile_skill_row is not None:
                    compile_skill = decode_as(compile_skill_row["snapshot_json"], SkillSnapshot)
        payload = event.payload
        source_event_wire = plain(record.event)
        if not isinstance(source_event_wire, Mapping):
            raise TypeError("source Agent event is not an object")
        source_event_sha256 = canonical_json_sha256(cast(Mapping[str, object], source_event_wire))
        directive = record.decision.teaching_directive
        inference = record.decision.draft.learner_inference
        if directive is None or inference is None:
            raise ValueError("learner inference source turn lacks committed policy data")
        if (
            durable_event != event
            or source["durable_event_type"] != RuntimeEventType.LEARNER_INFERENCE_RECORDED.value
            or source["durable_event_occurred_at"] != event.occurred_at
            or source["turn_actor_id"] != context.actor.actor_id
            or source["turn_content_hash"] != context.content_ref.content_hash
            or source["durable_source_event_sha256"] != source_event_sha256
            or source_event_sha256 != payload["source_event_sha256"]
            or agent_turn_commit_sha256(record) != payload["turn_commit_sha256"]
            or agent_turn_commit_sha256(turn_record_mapping) != payload["turn_commit_sha256"]
            or record.actor != context.actor
            or record.content_ref != context.content_ref
            or record.event.event_id != payload["source_event_id"]
            or record.event.student_id != payload["learner_id"]
            or record.event.session_id != payload["session_id"]
            or record.event.turn_id != payload["turn_id"]
            or record.event.command_id != payload["command_id"]
            or record.event.task_id != payload["task_id"]
            or record.event.run_id != payload["run_id"]
            or record.decision.role != payload["role"]
            or record.decision.completed_at != event.occurred_at
            or plain(record.decision.completed_at) != payload["inferred_at"]
            or directive.target_concept != payload["concept"]
            or directive.teaching_spec_version != payload["teaching_spec_version"]
            or set(directive.required_evidence_ids) != set(inference.evidence_ids)
            or inference.concept != payload["concept"]
            or inference.score_delta != payload["score_delta"]
            or inference.confidence != payload["confidence"]
            or inference.reason != payload["reason"]
            or command.command_id != event.command_id
            or command.versions.teaching_spec_version != job["teaching_spec_version"]
            or not _same_authority(command, context)
            or session.session_id != payload["session_id"]
            or session.student_id != payload["learner_id"]
            or session.task_id != payload["task_id"]
            or source["session_actor_id"] != context.actor.actor_id
            or source["session_content_hash"] != context.content_ref.content_hash
            or source["session_task_id"] != session.task_id
            or source["session_world_id"] != session.world_id
            or not _request_authority_matches(session.request_context, context)
            or task.task_id != payload["task_id"]
            or payload["concept"] not in task.knowledge_points
            or not _request_authority_matches(task.request_context, context)
        ):
            raise ValueError("learner inference source turn identity or policy drifted")
        if run is None:
            if job["run_id"] is not None:
                raise ValueError("learner inference Run binding is missing")
            if (
                compile_row is None
                or compile_result is None
                or compile_skill_row is None
                or compile_skill is None
                or record.event.event_type != "compile_failed"
                or record.event.build_id != compile_result.build_id
                or record.event.skill_ref != compile_result.skill_ref
                or compile_result.succeeded
                or compile_result.evidence_refs != record.event.evidence_refs
                or compile_row["actor_id"] != context.actor.actor_id
                or compile_row["content_hash"] != context.content_ref.content_hash
                or not _request_authority_matches(compile_result.request_context, context)
                or compile_skill_row["skill_id"] != compile_result.skill_ref.skill_id
                or compile_skill_row["skill_version_id"]
                != compile_result.skill_ref.skill_version_id
                or compile_skill_row["certification_id"]
                != compile_result.skill_ref.certification_id
                or compile_skill_row["actor_id"] != context.actor.actor_id
                or compile_skill_row["session_id"] != payload["session_id"]
                or compile_skill_row["content_hash"] != context.content_ref.content_hash
                or compile_skill_row["artifact_sha256"] != compile_result.skill_ref.artifact_sha256
                or compile_skill.ref != compile_result.skill_ref
                or not _request_authority_matches(compile_skill.request_context, context)
            ):
                raise ValueError("learner inference Compile result or Skill identity drifted")
            if command.versions.skill_version not in (
                None,
                compile_result.skill_ref.skill_version_id,
            ) or command.versions.artifact_sha256 not in (
                None,
                compile_result.skill_ref.artifact_sha256,
            ):
                raise ValueError("learner inference Command and Compile Skill versions drifted")
        elif skill is None:
            raise ValueError("learner inference Run has no durable Skill authority")
        else:
            binding_projection: dict[str, object] = {
                "binding_id": source["session_skill_binding_id"],
                "session_id": source["session_skill_binding_session_id"],
                "skill_id": source["session_skill_binding_skill_id"],
                "skill_version_id": source["session_skill_binding_version_id"],
                "certification_id": source["session_skill_binding_certification_id"],
                "artifact_sha256": source["session_skill_binding_artifact_sha256"],
                "actor_id": source["session_skill_binding_actor_id"],
                "content_hash": source["session_skill_binding_content_hash"],
            }
            public_skill_binding_valid = (
                source["durable_skill_session_id"] is None
                and source["public_session_id"] == payload["session_id"]
                and source["public_session_actor_id"] == context.actor.actor_id
                and source["public_session_content_hash"] == context.content_ref.content_hash
                and source["public_session_task_id"] == source["session_task_id"]
                and source["public_session_world_id"] == source["session_world_id"]
                and source["session_skill_binding_id"] is not None
                and source["session_skill_binding_session_id"] == payload["session_id"]
                and source["session_skill_binding_skill_id"] == run.skill_ref.skill_id
                and source["session_skill_binding_version_id"] == run.skill_ref.skill_version_id
                and source["session_skill_binding_certification_id"]
                == run.skill_ref.certification_id
                and source["session_skill_binding_artifact_sha256"] == run.skill_ref.artifact_sha256
                and source["session_skill_binding_actor_id"] == context.actor.actor_id
                and source["session_skill_binding_content_hash"] == context.content_ref.content_hash
                and source["session_skill_binding_sha256"]
                == canonical_json_sha256(binding_projection)
            )
            legacy_skill_binding_valid = (
                source["durable_skill_session_id"] == payload["session_id"]
                and source["public_session_id"] is None
                and source["session_skill_binding_id"] is None
            )
            if (
                run.run_id != job["run_id"]
                or run.session_id != payload["session_id"]
                or run.turn_id != payload["turn_id"]
                or run.command_id != payload["command_id"]
                or run.world_id != session.world_id
                or run.skill_ref != record.event.skill_ref
                or run.world_revision_before != record.event.expected_world_revision
                or run.failure_key != record.event.failure_key
                or source["run_actor_id"] != context.actor.actor_id
                or source["run_content_hash"] != context.content_ref.content_hash
                or source["run_session_id"] != payload["session_id"]
                or source["run_turn_id"] != payload["turn_id"]
                or source["run_command_id"] != payload["command_id"]
                or source["run_world_id"] != run.world_id
                or source["run_skill_version_id"] != run.skill_ref.skill_version_id
                or source["run_failure_key"] != run.failure_key
                or source["run_task_success"] != run.task_success
                or source["durable_skill_id"] != run.skill_ref.skill_id
                or source["durable_skill_version_id"] != run.skill_ref.skill_version_id
                or source["durable_skill_certification_id"] != run.skill_ref.certification_id
                or source["durable_skill_actor_id"] != context.actor.actor_id
                or not (legacy_skill_binding_valid or public_skill_binding_valid)
                or source["durable_skill_content_hash"] != context.content_ref.content_hash
                or source["durable_skill_artifact_sha256"] != run.skill_ref.artifact_sha256
                or skill.ref != run.skill_ref
                or not _request_authority_matches(skill.request_context, context)
                or not _request_authority_matches(run.request_context, context)
            ):
                raise ValueError("learner inference Run or Skill identity drifted")
        if run is not None and (
            command.versions.skill_version not in (None, run.skill_ref.skill_version_id)
            or command.versions.artifact_sha256 not in (None, run.skill_ref.artifact_sha256)
        ):
            raise ValueError("learner inference Command and Run Skill versions drifted")

        evidence_cursor = await connection.execute(
            """
            SELECT je.ordinal,je.evidence_id,je.evidence_sha256,
                   e.actor_id,e.content_hash,e.evidence_type,
                   e.payload_sha256,e.evidence_json,e.recorded_at
            FROM yaya_learner_projection_job_evidence je
            JOIN yaya_evidence e
              ON e.tenant_id=je.tenant_id AND e.evidence_id=je.evidence_id
            WHERE je.tenant_id=%s AND je.job_id=%s
            ORDER BY je.ordinal
            """,
            (job["tenant_id"], job["job_id"]),
        )
        evidence_rows = list(await evidence_cursor.fetchall())
        evidence_refs = _runtime_evidence(payload["evidence_refs"])
        if len(evidence_rows) != len(evidence_refs):
            raise ValueError("learner inference Evidence count drifted")
        decision_evidence = {item.evidence_id: item for item in record.decision.evidence_refs}
        inference_ids = tuple(item.evidence_id for item in evidence_refs)
        if set(inference_ids) != set(inference.evidence_ids):
            raise ValueError("learner inference Evidence identity drifted")
        for ordinal, (evidence_row, evidence_ref) in enumerate(
            zip(evidence_rows, evidence_refs, strict=True)
        ):
            evidence_json = evidence_row["evidence_json"]
            if not isinstance(evidence_json, Mapping):
                raise ValueError("durable Evidence document must be an object")
            evidence_document = cast(Mapping[str, object], evidence_json)
            evidence_payload = evidence_document.get("payload")
            evidence_integrity = evidence_document.get("integrity")
            evidence_ref_value = evidence_document.get("evidence_ref")
            evidence_subject = evidence_document.get("subject")
            evidence_source = evidence_document.get("source")
            evidence_context = evidence_document.get("request_context")
            evidence_versions = evidence_document.get("versions")
            if any(
                not isinstance(value, Mapping)
                for value in (
                    evidence_payload,
                    evidence_integrity,
                    evidence_ref_value,
                    evidence_subject,
                    evidence_source,
                    evidence_context,
                    evidence_versions,
                )
            ):
                raise ValueError("durable Evidence document lacks closed provenance")
            payload_mapping = cast(Mapping[str, object], evidence_payload)
            integrity_mapping = cast(Mapping[str, object], evidence_integrity)
            evidence_ref_mapping = cast(Mapping[str, object], evidence_ref_value)
            subject_mapping = cast(Mapping[str, object], evidence_subject)
            source_mapping = cast(Mapping[str, object], evidence_source)
            evidence_context_mapping = cast(Mapping[str, object], evidence_context)
            evidence_versions_mapping = cast(Mapping[str, object], evidence_versions)
            document_refs = _runtime_evidence((evidence_ref_mapping,))
            evidence_hash = canonical_json_sha256(payload_mapping)
            expected_source: dict[str, object]
            expected_evidence_context: object
            expected_source_created_at: object
            if run is not None:
                authority_skill_ref = run.skill_ref
                expected_evidence_context = plain(command.request_context)
                expected_source_created_at = source["run_created_at"]
            elif compile_result is not None:
                authority_skill_ref = compile_result.skill_ref
                expected_evidence_context = plain(compile_result.request_context)
                expected_source_created_at = evidence_ref.created_at
            else:
                raise ValueError(
                    "learner projection Evidence has no immutable Run or Compile source"
                )
            expected_versions_value = plain(
                replace(
                    command.versions,
                    skill_version=authority_skill_ref.skill_version_id,
                    artifact_sha256=authority_skill_ref.artifact_sha256,
                )
            )
            if not isinstance(expected_versions_value, Mapping):
                raise TypeError("canonical Evidence versions must be an object")
            expected_versions_mapping = cast(Mapping[str, object], expected_versions_value)
            expected_versions: dict[str, object] = {
                key: value for key, value in expected_versions_mapping.items() if value is not None
            }
            if run is not None and evidence_ref.evidence_type is EvidenceType.SANDBOX_LOG:
                expected_source = {
                    "source_type": "SKILL_RUN",
                    "source_id": run.run_id,
                    "command_id": event.command_id,
                    "world_id": run.world_id,
                }
                if (
                    set(payload_mapping)
                    != {
                        "evidence_kind",
                        "run_id",
                        "sandbox_status",
                        "world_status",
                        "intent_count",
                    }
                    or payload_mapping.get("evidence_kind") != "SKILL_RUN"
                    or payload_mapping.get("run_id") != run.run_id
                ):
                    raise ValueError("SANDBOX_LOG Evidence crossed its immutable Run")
            elif run is not None and evidence_ref.evidence_type is EvidenceType.WORLD_COMMIT:
                receipt = run.world_commit
                if receipt is None:
                    raise ValueError("WORLD_COMMIT Evidence has no typed Run receipt")
                expected_source = {
                    "source_type": "WORLD",
                    "source_id": run.world_id,
                    "command_id": event.command_id,
                    "world_id": run.world_id,
                }
                expected_payload = {
                    "evidence_kind": "WORLD_COMMIT",
                    "world_id": receipt.world_id,
                    "previous_revision": receipt.previous_revision,
                    "world_revision": receipt.world_revision,
                    "first_event_sequence": receipt.first_event_sequence,
                    "last_event_sequence": receipt.last_event_sequence,
                    "state_hash": receipt.state_hash,
                }
                if payload_mapping != expected_payload or evidence_document.get(
                    "occurred_at"
                ) != plain(receipt.committed_at):
                    raise ValueError("WORLD_COMMIT Evidence crossed its typed Run receipt")
            elif (
                compile_result is not None
                and evidence_ref.evidence_type is EvidenceType.TEST_REPORT
            ):
                test_suite_version = command.versions.test_suite_version
                if test_suite_version is None:
                    raise ValueError("Compile Evidence lacks its frozen test-suite version")
                expected_source = {
                    "source_type": "SKILL_BUILD",
                    "source_id": compile_result.build_id,
                    "command_id": None,
                    "world_id": None,
                }
                expected_payload = {
                    "evidence_kind": "BUILD_CERTIFICATION",
                    "build_id": compile_result.build_id,
                    "skill_id": compile_result.skill_ref.skill_id,
                    "skill_version_id": compile_result.skill_ref.skill_version_id,
                    "artifact_sha256": compile_result.skill_ref.artifact_sha256,
                    "test_suite_version": test_suite_version,
                    "outcome": "REJECTED",
                }
                if payload_mapping != expected_payload or evidence_document.get(
                    "occurred_at"
                ) != plain(evidence_ref.created_at):
                    raise ValueError("TEST_REPORT Evidence crossed its immutable Compile result")
            else:
                raise ValueError(
                    "learner projection Evidence type is not emitted by its source path"
                )
            durable_recorded_at = evidence_row["recorded_at"]
            if not isinstance(durable_recorded_at, datetime) or not isinstance(
                expected_source_created_at, datetime
            ):
                raise ValueError("learner projection Evidence recording time is invalid")
            if (
                set(evidence_document)
                != {
                    "request_context",
                    "evidence_ref",
                    "subject",
                    "source",
                    "occurred_at",
                    "recorded_at",
                    "integrity",
                    "payload",
                    "related_evidence",
                    "versions",
                }
                or evidence_row["ordinal"] != ordinal
                or evidence_row["evidence_id"] != evidence_ref.evidence_id
                or evidence_row["evidence_sha256"] != evidence_ref.sha256
                or evidence_row["payload_sha256"] != evidence_ref.sha256
                or evidence_hash != evidence_ref.sha256
                or integrity_mapping.get("payload_sha256") != evidence_ref.sha256
                or integrity_mapping
                != {
                    "payload_sha256": evidence_ref.sha256,
                    "previous_evidence_sha256": None,
                }
                or document_refs != (evidence_ref,)
                or subject_mapping != {"learner_id": context.actor.actor_id}
                or source_mapping != expected_source
                or evidence_context_mapping != expected_evidence_context
                or evidence_document.get("occurred_at") != plain(evidence_ref.created_at)
                or evidence_document.get("recorded_at") != plain(durable_recorded_at)
                or durable_recorded_at != expected_source_created_at
                or durable_recorded_at != record.event.occurred_at
                or durable_recorded_at < evidence_ref.created_at
                or evidence_document.get("related_evidence") != []
                or evidence_versions_mapping != expected_versions
                or evidence_row["actor_id"] != context.actor.actor_id
                or evidence_row["content_hash"] != context.content_ref.content_hash
                or evidence_row["evidence_type"] != evidence_ref.evidence_type.value
                or decision_evidence.get(evidence_ref.evidence_id) != evidence_ref
            ):
                raise ValueError("learner inference Evidence hash or authority drifted")
        if run is not None and set(run.evidence_refs) != set(record.decision.evidence_refs):
            raise ValueError("learner inference Run and decision Evidence differ")
        if compile_result is not None and set(compile_result.evidence_refs) != set(
            record.decision.evidence_refs
        ):
            raise ValueError("learner inference Compile and decision Evidence differ")
        return _LearnerProjectionFacts(
            record=record,
            task=task,
            run=run,
            evidence_refs=evidence_refs,
        )

    @staticmethod
    def _projection_outcome(facts: _LearnerProjectionFacts) -> ProjectionOutcome:
        event_type = facts.record.event.event_type
        if facts.run is not None:
            return ProjectionOutcome.SUCCESS if facts.run.task_success else ProjectionOutcome.FAILED
        if event_type == "compile_failed":
            return ProjectionOutcome.FAILED
        return ProjectionOutcome.PARTIAL

    @staticmethod
    def _task_relation(facts: _LearnerProjectionFacts) -> TaskRelation:
        raw_relation = facts.record.event.payload.get("task_relation", "STANDARD")
        if not isinstance(raw_relation, str):
            raise ValueError("task_relation must be a trusted string enum")
        return TaskRelation(raw_relation)

    def _apply_policy(
        self,
        snapshot: LearnerModelSnapshot,
        event: RuntimeEvent,
        facts: _LearnerProjectionFacts,
    ) -> tuple[LearnerModelSnapshot, LearnerUpdate]:
        competencies, _ = self._competencies(snapshot)
        concept = cast(str, event.payload["concept"])
        directive = facts.record.decision.teaching_directive
        if directive is None:
            raise ValueError("learner projection lost its TeachingDirective")
        policy_result = self._policy.project(
            ProjectionInput(
                learner_revision=snapshot.revision,
                learner_projection_policy_version=LEARNER_PROJECTION_POLICY_VERSION,
                review_policy_version=REVIEW_POLICY_VERSION,
                evidence=ProjectionEvidence(
                    evidence_ids=tuple(item.evidence_id for item in facts.evidence_refs),
                    concept=concept,
                    outcome=self._projection_outcome(facts),
                    task_relation=self._task_relation(facts),
                    assistance_level=directive.hint_level,
                    occurred_at=event.occurred_at,
                    source_sequence=event.sequence,
                    used_full_solution=directive.full_solution_eligible,
                    used_skill_patch=facts.record.decision.draft.skill_patch is not None,
                ),
                current=competencies.get(concept),
            )
        )
        projected_competencies = dict(competencies)
        if policy_result.applied:
            projected_competencies[concept] = policy_result.competency

        # Snapshot Evidence is a bounded, ordered working set.  Preserve the
        # source-stream insertion order instead of sorting identities: rebuild
        # consumes the same immutable stream and therefore performs exactly the
        # same deterministic compaction as the online worker.
        evidence_by_id = {item.evidence_id: item for item in snapshot.evidence_refs}
        for evidence in facts.evidence_refs:
            current = evidence_by_id.get(evidence.evidence_id)
            if current is not None and current != evidence:
                raise ValueError("learner Evidence identity was reused with different metadata")
            evidence_by_id[evidence.evidence_id] = evidence
        retained_evidence_refs = tuple(evidence_by_id.values())[
            -_MAX_LEARNER_SNAPSHOT_EVIDENCE_REFS:
        ]
        retained_evidence_ids = {item.evidence_id for item in retained_evidence_refs}

        # Competencies may each retain 64 references while the canonical
        # LearnerModelSnapshot has one global 64-reference budget.  Close every
        # competency over that global retained set.  A competency whose final
        # reference ages out carries no support and is removed rather than
        # leaving an invalid or misleading projection behind.
        compacted_competencies: dict[str, CompetencyProjection] = {}
        for competency_id, competency in projected_competencies.items():
            retained_competency_ids = tuple(
                evidence_id
                for evidence_id in competency.evidence_ids
                if evidence_id in retained_evidence_ids
            )
            if not retained_competency_ids:
                continue
            compacted_competencies[competency_id] = (
                competency
                if retained_competency_ids == competency.evidence_ids
                else replace(competency, evidence_ids=retained_competency_ids)
            )
        changed_competency_ids = tuple(
            sorted(
                competency_id
                for competency_id in competencies.keys() | compacted_competencies.keys()
                if competencies.get(competency_id) != compacted_competencies.get(competency_id)
            )
        )
        next_snapshot = LearnerModelSnapshot(
            learner_id=snapshot.learner_id,
            revision=snapshot.revision + 1,
            model_version=LEARNER_PROJECTION_POLICY_VERSION,
            projected_through_sequence=event.sequence,
            competencies=cast(
                FrozenJsonObject,
                {
                    competency_id: plain(competency)
                    for competency_id, competency in compacted_competencies.items()
                },
            ),
            updated_at=event.occurred_at,
            evidence_refs=retained_evidence_refs,
        )
        update = LearnerUpdate(
            learner_id=snapshot.learner_id,
            previous_revision=snapshot.revision,
            revision=next_snapshot.revision,
            model_version=next_snapshot.model_version,
            changed_competency_ids=changed_competency_ids,
            evidence_refs=facts.evidence_refs,
            updated_at=next_snapshot.updated_at,
        )
        return next_snapshot, update

    async def project(
        self,
        event: RuntimeEvent,
        expected_learner_revision: int,
        context: OperationContext,
    ) -> Result[LearnerUpdate]:
        del event, expected_learner_revision, context
        return _failure(
            "INVARIANT_VIOLATION",
            "COMPLETE",
            "Production learner projection requires a durable Job lease and fencing token",
        )

    async def project_fenced(
        self,
        event: RuntimeEvent,
        expected_learner_revision: int,
        context: OperationContext,
        fence: LearnerProjectionFence,
    ) -> Result[LearnerUpdate]:
        if fence.tenant_id != context.actor.tenant_id:
            raise LearnerProjectionFenceLost()
        try:
            async with _store_transaction(self._database) as connection:
                job = await self._load_fenced_job(connection, fence)
                learner_id = cast(str, job["learner_id"])
                await self._lock_learner(connection, fence.tenant_id, learner_id)
                facts = await self._load_source_facts(
                    connection,
                    job,
                    event,
                    context,
                    expected_learner_revision,
                )
                model_cursor = await connection.execute(
                    """
                    SELECT actor_id,content_hash,revision,projected_through_sequence,
                           snapshot_json,snapshot_sha256,request_context_json,
                           projection_policy_version,updated_at
                    FROM yaya_learner_models
                    WHERE tenant_id=%s AND learner_id=%s FOR UPDATE
                    """,
                    (fence.tenant_id, learner_id),
                )
                model_row = await model_cursor.fetchone()
                current = self._model_snapshot_from_row(
                    model_row,
                    learner_id,
                    context,
                    empty_at=event.occurred_at,
                )
                if (
                    current.revision != expected_learner_revision
                    or current.projected_through_sequence != event.sequence - 1
                ):
                    return _failure(
                        "EVENT_SEQUENCE_GAP",
                        "COMPLETE",
                        "Learner projection revision or source checkpoint changed",
                    )
                next_snapshot, update = self._apply_policy(current, event, facts)
                snapshot_sha256 = internal_record_sha256(next_snapshot)
                persisted = await connection.execute(
                    """
                    INSERT INTO yaya_learner_models(
                        tenant_id,learner_id,actor_id,content_hash,revision,
                        projected_through_sequence,snapshot_json,snapshot_sha256,
                        request_context_json,projection_policy_version,updated_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (tenant_id,learner_id) DO UPDATE SET
                      revision=EXCLUDED.revision,
                      projected_through_sequence=EXCLUDED.projected_through_sequence,
                      snapshot_json=EXCLUDED.snapshot_json,
                      snapshot_sha256=EXCLUDED.snapshot_sha256,
                      request_context_json=EXCLUDED.request_context_json,
                      projection_policy_version=EXCLUDED.projection_policy_version,
                      updated_at=EXCLUDED.updated_at
                    WHERE yaya_learner_models.actor_id=%s
                      AND yaya_learner_models.content_hash=%s
                      AND yaya_learner_models.revision=%s
                      AND yaya_learner_models.projected_through_sequence=%s
                    RETURNING revision,projected_through_sequence
                    """,
                    (
                        fence.tenant_id,
                        learner_id,
                        context.actor.actor_id,
                        context.content_ref.content_hash,
                        next_snapshot.revision,
                        next_snapshot.projected_through_sequence,
                        Jsonb(encode(next_snapshot)),
                        snapshot_sha256,
                        Jsonb(encode(context)),
                        LEARNER_PROJECTION_POLICY_VERSION,
                        next_snapshot.updated_at,
                        context.actor.actor_id,
                        context.content_ref.content_hash,
                        expected_learner_revision,
                        event.sequence - 1,
                    ),
                )
                persisted_row = await persisted.fetchone()
                if persisted_row is None:
                    return _failure(
                        "EVENT_SEQUENCE_GAP",
                        "COMPLETE",
                        "Learner projection lost its snapshot CAS",
                    )
                projected_at_cursor = await connection.execute("SELECT clock_timestamp() AS value")
                projected_at_row = await projected_at_cursor.fetchone()
                if projected_at_row is None:
                    raise RuntimeError("PostgreSQL clock query returned no row")
                projected_at = cast(datetime, projected_at_row["value"])
                derived_sequence = await self._next_derived_sequence(
                    connection,
                    fence.tenant_id,
                    learner_id,
                )
                identity_seed = {
                    "kind": "learner_model_updated_v1",
                    "tenant_id": fence.tenant_id,
                    "job_id": fence.job_id,
                    "event_id": event.event_id,
                    "event_sha256": job["event_sha256"],
                }
                derived_event_id = _learner_identifier("evt_learner_model", identity_seed)
                outbox_message_id = _learner_identifier("learner_model_msg", identity_seed)
                derived_event = RuntimeEvent(
                    event_id=derived_event_id,
                    event_type=RuntimeEventType.LEARNER_MODEL_UPDATED,
                    event_version=1,
                    stream_id=self._derived_stream_id(learner_id),
                    sequence=derived_sequence,
                    occurred_at=projected_at,
                    producer="learner_projection_worker",
                    trace_id=context.trace_id,
                    command_id=event.command_id,
                    correlation_id=context.correlation_id,
                    causation_id=event.event_id,
                    content_ref=context.content_ref,
                    payload={
                        "learner_id": learner_id,
                        "previous_revision": update.previous_revision,
                        "learner_revision": update.revision,
                        "projected_through_sequence": event.sequence,
                        "changed_competency_ids": list(update.changed_competency_ids),
                        "updated_at": plain(update.updated_at),
                        "evidence_refs": [
                            _learner_evidence_wire(item) for item in update.evidence_refs
                        ],
                    },
                )
                derived_wire = cast(Mapping[str, object], plain(derived_event))
                await connection.execute(
                    """
                    INSERT INTO yaya_events(
                        tenant_id,event_id,stream_id,sequence,event_type,
                        event_json,occurred_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        fence.tenant_id,
                        derived_event.event_id,
                        derived_event.stream_id,
                        derived_event.sequence,
                        derived_event.event_type,
                        Jsonb(encode(derived_event)),
                        derived_event.occurred_at,
                    ),
                )
                await connection.execute(
                    """
                    INSERT INTO yaya_outbox(
                        tenant_id,message_id,destination,idempotency_key,
                        payload_sha256,status,attempt,message_json,created_at
                    ) VALUES (%s,%s,'learner_model_events',%s,%s,'PENDING',0,%s,%s)
                    """,
                    (
                        fence.tenant_id,
                        outbox_message_id,
                        f"learner-model:{event.event_id}",
                        internal_record_sha256(derived_wire),
                        Jsonb(derived_wire),
                        projected_at,
                    ),
                )
                receipt_record: dict[str, object] = {
                    "tenant_id": fence.tenant_id,
                    "event_id": event.event_id,
                    "job_id": fence.job_id,
                    "source_event_id": job["source_event_id"],
                    "learner_id": learner_id,
                    "source_stream_sequence": event.sequence,
                    "event_sha256": job["event_sha256"],
                    "inference_sha256": job["inference_sha256"],
                    "previous_learner_revision": update.previous_revision,
                    "learner_revision": update.revision,
                    "model_version": update.model_version,
                    "snapshot_sha256": snapshot_sha256,
                    "model_updated_event_id": derived_event_id,
                    "outbox_message_id": outbox_message_id,
                    "update": plain(update),
                    "projected_at": plain(projected_at),
                }
                receipt_sha256 = internal_record_sha256(receipt_record)
                await connection.execute(
                    """
                    INSERT INTO yaya_learner_projection_receipts(
                        tenant_id,event_id,job_id,source_event_id,learner_id,
                        actor_id,content_hash,source_stream_id,
                        source_stream_sequence,event_sha256,inference_sha256,
                        previous_learner_revision,learner_revision,model_version,
                        snapshot_sha256,model_updated_event_id,outbox_message_id,
                        update_json,receipt_sha256,projected_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                              %s,%s,%s,%s,%s,%s)
                    """,
                    (
                        fence.tenant_id,
                        event.event_id,
                        fence.job_id,
                        job["source_event_id"],
                        learner_id,
                        context.actor.actor_id,
                        context.content_ref.content_hash,
                        event.stream_id,
                        event.sequence,
                        job["event_sha256"],
                        job["inference_sha256"],
                        update.previous_revision,
                        update.revision,
                        update.model_version,
                        snapshot_sha256,
                        derived_event_id,
                        outbox_message_id,
                        Jsonb(encode(update)),
                        receipt_sha256,
                        projected_at,
                    ),
                )
                finalized = await connection.execute(
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
                if finalized.rowcount != 1:
                    raise LearnerProjectionFenceLost()
                await connection.execute(
                    """
                    UPDATE yaya_learner_projection_failures
                    SET resolved_at=%s,resolution='RETRIED'
                    WHERE tenant_id=%s AND job_id=%s
                      AND classification='RETRYABLE' AND resolved_at IS NULL
                    """,
                    (projected_at, fence.tenant_id, fence.job_id),
                )
                return Success(update)
        except LearnerProjectionFenceLost:
            raise
        except PermissionError as error:
            return _failure("AUTHORIZATION_DENIED", "COMPLETE", str(error))
        except (KeyError, TypeError, ValueError) as error:
            return _failure("INVARIANT_VIOLATION", "COMPLETE", str(error))
        except psycopg.Error as error:
            return _database_failure(error, "COMPLETE")

    async def fail_fenced(
        self,
        event: RuntimeEvent,
        error: ContractError,
        context: OperationContext,
        fence: LearnerProjectionFence,
    ) -> Result[None]:
        if fence.tenant_id != context.actor.tenant_id:
            raise LearnerProjectionFenceLost()
        if error.retryable:
            return _failure(
                "INVARIANT_VIOLATION",
                "COMPLETE",
                "Retryable projection errors cannot use the terminal failure path",
            )
        try:
            async with _store_transaction(self._database) as connection:
                job = await self._load_fenced_job(connection, fence)
                self._validate_job_envelope(job, event, context)
                learner_id = cast(str, job["learner_id"])
                await self._lock_learner(connection, fence.tenant_id, learner_id)
                clock_cursor = await connection.execute("SELECT clock_timestamp() AS value")
                clock_row = await clock_cursor.fetchone()
                if clock_row is None:
                    raise RuntimeError("PostgreSQL clock query returned no row")
                failed_at = cast(datetime, clock_row["value"])
                error_wire = _contract_error_wire(error)
                error_sha256 = internal_record_sha256(error_wire)
                identity_seed = {
                    "kind": "learner_projection_failed_v1",
                    "tenant_id": fence.tenant_id,
                    "job_id": fence.job_id,
                    "event_id": event.event_id,
                    "attempt": fence.fencing_token,
                    "error_sha256": error_sha256,
                }
                failure_id = _learner_identifier("learner_failure", identity_seed)
                failure_event_id = _learner_identifier("evt_learner_failed", identity_seed)
                outbox_message_id = _learner_identifier("learner_failed_msg", identity_seed)
                derived_sequence = await self._next_derived_sequence(
                    connection,
                    fence.tenant_id,
                    learner_id,
                )
                failure_event = RuntimeEvent(
                    event_id=failure_event_id,
                    event_type=RuntimeEventType.LEARNER_PROJECTION_FAILED,
                    event_version=1,
                    stream_id=self._derived_stream_id(learner_id),
                    sequence=derived_sequence,
                    occurred_at=failed_at,
                    producer="learner_projection_worker",
                    trace_id=context.trace_id,
                    command_id=event.command_id,
                    correlation_id=context.correlation_id,
                    causation_id=event.event_id,
                    content_ref=context.content_ref,
                    payload={
                        "learner_id": learner_id,
                        "source_event_id": event.event_id,
                        "failed_at": plain(failed_at),
                        "error": error_wire,
                    },
                )
                failure_wire = cast(Mapping[str, object], plain(failure_event))
                await connection.execute(
                    """
                    INSERT INTO yaya_events(
                        tenant_id,event_id,stream_id,sequence,event_type,
                        event_json,occurred_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        fence.tenant_id,
                        failure_event.event_id,
                        failure_event.stream_id,
                        failure_event.sequence,
                        failure_event.event_type,
                        Jsonb(encode(failure_event)),
                        failed_at,
                    ),
                )
                await connection.execute(
                    """
                    INSERT INTO yaya_outbox(
                        tenant_id,message_id,destination,idempotency_key,
                        payload_sha256,status,attempt,message_json,created_at
                    ) VALUES (%s,%s,'learner_model_events',%s,%s,'PENDING',0,%s,%s)
                    """,
                    (
                        fence.tenant_id,
                        outbox_message_id,
                        f"learner-projection-failed:{event.event_id}",
                        internal_record_sha256(failure_wire),
                        Jsonb(failure_wire),
                        failed_at,
                    ),
                )
                await connection.execute(
                    """
                    INSERT INTO yaya_learner_projection_failures(
                        tenant_id,failure_id,job_id,event_id,source_event_id,
                        learner_id,actor_id,content_hash,source_stream_id,
                        source_stream_sequence,attempt,fencing_token,
                        classification,error_code,error_json,error_sha256,
                        failure_event_id,outbox_message_id,recorded_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                              'PERMANENT',%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        fence.tenant_id,
                        failure_id,
                        fence.job_id,
                        event.event_id,
                        job["source_event_id"],
                        learner_id,
                        context.actor.actor_id,
                        context.content_ref.content_hash,
                        event.stream_id,
                        event.sequence,
                        fence.fencing_token,
                        fence.fencing_token,
                        error.code,
                        Jsonb(error_wire),
                        error_sha256,
                        failure_event_id,
                        outbox_message_id,
                        failed_at,
                    ),
                )
                finalized = await connection.execute(
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
                        Jsonb(error_wire),
                        failed_at,
                        fence.tenant_id,
                        fence.job_id,
                        fence.worker_id,
                        fence.lease_id,
                        fence.fencing_token,
                    ),
                )
                if finalized.rowcount != 1:
                    raise LearnerProjectionFenceLost()
                return Success(None)
        except LearnerProjectionFenceLost:
            raise
        except PermissionError as caught:
            return _failure("AUTHORIZATION_DENIED", "COMPLETE", str(caught))
        except (KeyError, TypeError, ValueError) as caught:
            return _failure("INVARIANT_VIOLATION", "COMPLETE", str(caught))
        except psycopg.Error as caught:
            return _database_failure(caught, "COMPLETE")

    async def get_snapshot(
        self,
        learner_id: str,
        context: OperationContext,
    ) -> Result[LearnerModelSnapshot]:
        if learner_id != context.actor.actor_id:
            return _failure("AUTHORIZATION_DENIED", "VALIDATE", "Learner model authority mismatch")
        try:
            connection = await self._database.connect(autocommit=True)
            try:
                cursor = await connection.execute(
                    """
                    SELECT actor_id,content_hash,revision,projected_through_sequence,
                           snapshot_json,snapshot_sha256,request_context_json,
                           projection_policy_version,updated_at
                    FROM yaya_learner_models
                    WHERE tenant_id=%s AND learner_id=%s
                    """,
                    (context.actor.tenant_id, learner_id),
                )
                row = await cursor.fetchone()
            finally:
                await connection.close()
            if row is None:
                return _failure("NOT_FOUND", "VALIDATE", "Learner model not found")
            return Success(
                self._model_snapshot_from_row(
                    row,
                    learner_id,
                    context,
                    empty_at=context.requested_at,
                )
            )
        except PermissionError as error:
            return _failure("AUTHORIZATION_DENIED", "VALIDATE", str(error))
        except (KeyError, TypeError, ValueError) as error:
            return _failure("INVARIANT_VIOLATION", "VALIDATE", str(error))
        except psycopg.Error as error:
            return _database_failure(error, "VALIDATE")

    async def rebuild(
        self,
        learner_id: str,
        through_sequence: int,
        context: OperationContext,
    ) -> Result[LearnerModelSnapshot]:
        if (
            learner_id != context.actor.actor_id
            or isinstance(through_sequence, bool)
            or not isinstance(through_sequence, int)
            or through_sequence < 0
        ):
            return _failure("AUTHORIZATION_DENIED", "COMPLETE", "Invalid learner rebuild scope")
        try:
            async with _store_transaction(self._database) as connection:
                await self._lock_learner(
                    connection,
                    context.actor.tenant_id,
                    learner_id,
                )
                model_cursor = await connection.execute(
                    """
                    SELECT actor_id,content_hash,revision,projected_through_sequence,
                           snapshot_json,snapshot_sha256,request_context_json,
                           projection_policy_version,updated_at
                    FROM yaya_learner_models
                    WHERE tenant_id=%s AND learner_id=%s FOR UPDATE
                    """,
                    (context.actor.tenant_id, learner_id),
                )
                current_row = await model_cursor.fetchone()
                if current_row is not None and (
                    current_row["actor_id"] != context.actor.actor_id
                    or current_row["content_hash"] != context.content_ref.content_hash
                ):
                    raise PermissionError("learner rebuild crossed actor/content authority")
                applied_cursor = await connection.execute(
                    """
                    SELECT COALESCE(MAX(source_stream_sequence),0) AS sequence
                    FROM (
                      SELECT source_stream_sequence
                      FROM yaya_learner_projection_receipts
                      WHERE tenant_id=%s AND learner_id=%s
                      UNION ALL
                      SELECT source_stream_sequence
                      FROM yaya_learner_projection_failures
                      WHERE tenant_id=%s AND learner_id=%s
                        AND resolved_at IS NOT NULL AND resolution='REBUILT'
                    ) AS applied
                    """,
                    (
                        context.actor.tenant_id,
                        learner_id,
                        context.actor.tenant_id,
                        learner_id,
                    ),
                )
                applied_row = await applied_cursor.fetchone()
                if applied_row is None:
                    raise RuntimeError("learner rebuild applied checkpoint query returned no row")
                applied_checkpoint = cast(int, applied_row["sequence"])
                if through_sequence < applied_checkpoint:
                    raise ValueError(
                        "learner rebuild cannot move the durable applied checkpoint backwards"
                    )

                current_model_is_trusted = False
                if current_row is not None:
                    try:
                        stored_context_json = current_row["request_context_json"]
                        stored_policy_version = current_row["projection_policy_version"]
                        stored_snapshot_sha256 = current_row["snapshot_sha256"]
                        if (
                            stored_context_json is None
                            or not isinstance(stored_policy_version, str)
                            or stored_snapshot_sha256 is None
                        ):
                            raise ValueError("learner model provenance is incomplete")
                        stored_context = decode_as(
                            stored_context_json,
                            OperationContext,
                        )
                        if not _request_authority_matches(stored_context, context):
                            raise ValueError("learner model provenance authority drifted")
                        stored_snapshot = decode_as(
                            current_row["snapshot_json"],
                            LearnerModelSnapshot,
                        )
                        validate_persisted_learner_snapshot(
                            stored_snapshot,
                            learner_id=learner_id,
                            revision=current_row["revision"],
                            projected_through_sequence=current_row["projected_through_sequence"],
                            model_version=stored_policy_version,
                            snapshot_sha256=stored_snapshot_sha256,
                            updated_at=current_row["updated_at"],
                        )
                        current_model_is_trusted = (
                            current_row["revision"]
                            == current_row["projected_through_sequence"]
                            == applied_checkpoint
                        )
                    except (KeyError, TypeError, ValueError):
                        current_model_is_trusted = False
                if (
                    current_model_is_trusted
                    and current_row is not None
                    and through_sequence < cast(int, current_row["projected_through_sequence"])
                ):
                    raise ValueError(
                        "learner rebuild cannot move the canonical checkpoint backwards"
                    )
                expected_database_revision = (
                    None if current_row is None else cast(int, current_row["revision"])
                )
                expected_database_checkpoint = (
                    None
                    if current_row is None
                    else cast(int, current_row["projected_through_sequence"])
                )
                events_cursor = await connection.execute(
                    """
                    SELECT event_id,stream_id,sequence,event_type,event_json
                    FROM yaya_events
                    WHERE tenant_id=%s AND stream_id=%s AND sequence<=%s
                    ORDER BY sequence,event_id
                    """,
                    (
                        context.actor.tenant_id,
                        self._source_stream_id(learner_id),
                        through_sequence,
                    ),
                )
                rows = list(await events_cursor.fetchall())
                if len(rows) != through_sequence:
                    raise ValueError("learner rebuild source stream contains a sequence gap")
                snapshot = self._empty_snapshot(
                    learner_id,
                    _LEARNER_EMPTY_UPDATED_AT,
                )
                for expected_sequence, source_row in enumerate(rows, start=1):
                    if (
                        source_row["sequence"] != expected_sequence
                        or source_row["event_type"]
                        != RuntimeEventType.LEARNER_INFERENCE_RECORDED.value
                    ):
                        raise ValueError(
                            "learner rebuild source stream is not contiguous immutable inference"
                        )
                    event = decode_as(source_row["event_json"], RuntimeEvent)
                    if (
                        event.event_id != source_row["event_id"]
                        or event.stream_id != source_row["stream_id"]
                        or event.sequence != source_row["sequence"]
                        or event.event_type != source_row["event_type"]
                    ):
                        raise ValueError(
                            "learner rebuild source row and canonical event identity drifted"
                        )
                    job_cursor = await connection.execute(
                        """
                        SELECT j.* FROM yaya_learner_projection_jobs j
                        WHERE tenant_id=%s AND event_id=%s
                        """,
                        (context.actor.tenant_id, source_row["event_id"]),
                    )
                    job = await job_cursor.fetchone()
                    if job is None:
                        raise ValueError("learner rebuild source event has no durable Job")
                    if job["state"] in {"READY", "LEASED"}:
                        raise ValueError("learner rebuild cannot cross an active projection Job")
                    if job["state"] not in {"SUCCEEDED", "FAILED"}:
                        raise ValueError("learner rebuild source Job state is unsupported")
                    stored_context = decode_as(job["operation_context_json"], OperationContext)
                    if not _request_authority_matches(stored_context, context):
                        raise PermissionError(
                            "learner rebuild source event crossed actor/content authority"
                        )
                    facts = await self._load_source_facts(
                        connection,
                        job,
                        event,
                        stored_context,
                        snapshot.revision,
                    )
                    snapshot, _ = self._apply_policy(snapshot, event, facts)
                if snapshot.projected_through_sequence != through_sequence:
                    raise ValueError("learner rebuild checkpoint does not equal requested sequence")
                snapshot_sha256 = internal_record_sha256(snapshot)
                persisted = await connection.execute(
                    """
                    INSERT INTO yaya_learner_models(
                        tenant_id,learner_id,actor_id,content_hash,revision,
                        projected_through_sequence,snapshot_json,snapshot_sha256,
                        request_context_json,projection_policy_version,updated_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (tenant_id,learner_id) DO UPDATE SET
                      revision=EXCLUDED.revision,
                      projected_through_sequence=EXCLUDED.projected_through_sequence,
                      snapshot_json=EXCLUDED.snapshot_json,
                      snapshot_sha256=EXCLUDED.snapshot_sha256,
                      request_context_json=EXCLUDED.request_context_json,
                      projection_policy_version=EXCLUDED.projection_policy_version,
                      updated_at=EXCLUDED.updated_at
                    WHERE yaya_learner_models.actor_id=%s
                      AND yaya_learner_models.content_hash=%s
                      AND yaya_learner_models.revision=%s
                      AND yaya_learner_models.projected_through_sequence=%s
                    RETURNING revision,projected_through_sequence
                    """,
                    (
                        context.actor.tenant_id,
                        learner_id,
                        context.actor.actor_id,
                        context.content_ref.content_hash,
                        snapshot.revision,
                        snapshot.projected_through_sequence,
                        Jsonb(encode(snapshot)),
                        snapshot_sha256,
                        Jsonb(encode(context)),
                        LEARNER_PROJECTION_POLICY_VERSION,
                        snapshot.updated_at,
                        context.actor.actor_id,
                        context.content_ref.content_hash,
                        expected_database_revision,
                        expected_database_checkpoint,
                    ),
                )
                if await persisted.fetchone() is None:
                    return _failure(
                        "EVENT_SEQUENCE_GAP",
                        "COMPLETE",
                        "Learner rebuild lost its snapshot CAS",
                    )
                await connection.execute(
                    """
                    UPDATE yaya_learner_projection_failures
                    SET resolved_at=clock_timestamp(),resolution='REBUILT'
                    WHERE tenant_id=%s AND learner_id=%s
                      AND source_stream_sequence<=%s AND resolved_at IS NULL
                    """,
                    (
                        context.actor.tenant_id,
                        learner_id,
                        through_sequence,
                    ),
                )
                return Success(snapshot)
        except PermissionError as error:
            return _failure("AUTHORIZATION_DENIED", "COMPLETE", str(error))
        except (KeyError, TypeError, ValueError) as error:
            return _failure("INVARIANT_VIOLATION", "COMPLETE", str(error))
        except psycopg.Error as error:
            return _database_failure(error, "COMPLETE")


__all__ = [
    "PostgresAuditStore",
    "PostgresCommandStore",
    "PostgresEventStore",
    "PostgresLearnerStore",
    "PostgresOutboxStore",
    "PostgresRegistryStore",
]
