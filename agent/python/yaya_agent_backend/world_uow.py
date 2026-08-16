"""Transaction-aware PostgreSQL WorldUnitOfWorkPort implementation.

This module is the only production owner of the World CAS and of the SQL that
publishes World events with their projection-outbox records.  The public
adapter owns a transaction.  Application services that already own a larger
transaction use ``participant.commit_on`` so World, Run, Evidence and their
idempotency receipt can share one PostgreSQL COMMIT.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

import psycopg
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from yaya_agent_contracts import (
    ActorRef,
    CommandRecord,
    CommandStatus,
    ContractError,
    ErrorCategory,
    EventAppendReceipt,
    Failure,
    FrozenJsonObject,
    OperationContext,
    OutboxMessage,
    RequestContext,
    Result,
    RuntimeEvent,
    Success,
    WaterIntent,
    WorldAtomicCommit,
    WorldAtomicCommitReceipt,
    WorldCommitReceipt,
    WorldSnapshot,
    canonical_json_sha256,
)

from .codec import decode_as, encode, plain
from .database import PostgresCommitStateUnknown, PostgresDatabase
from .world import WateringWorldEngine, WorldRuleViolation

type _Connection = AsyncConnection[dict[str, object]]
type _ErrorMetadata = tuple[ErrorCategory, bool, str]

_ERRORS: Mapping[str, _ErrorMetadata] = {
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
    "WORLD_REVISION_CONFLICT": (
        ErrorCategory.CONCURRENCY,
        True,
        "world.changed_retry",
    ),
    "EVENT_SEQUENCE_GAP": (
        ErrorCategory.CONCURRENCY,
        True,
        "event.resync_required",
    ),
    "WORLD_RULE_REJECTED": (
        ErrorCategory.WORLD_RULE,
        False,
        "world.rule_rejected",
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


def _failure(
    code: str,
    message: str,
    details: Mapping[str, object] | None = None,
) -> Failure:
    category, retryable, message_key = _ERRORS[code]
    return Failure(
        ContractError(
            code=code,
            category=category,
            retryable=retryable,
            user_message_key=message_key,
            stage="WORLD_COMMIT",
            message=message[:512],
            details=cast(FrozenJsonObject, details or {}),
        )
    )


def _same_actor(left: ActorRef, right: ActorRef) -> bool:
    return (
        left.tenant_id,
        left.actor_id,
        left.actor_type,
    ) == (
        right.tenant_id,
        right.actor_id,
        right.actor_type,
    )


def _same_authority(origin: RequestContext, context: OperationContext) -> bool:
    return _same_actor(origin.actor, context.actor) and origin.content_ref == context.content_ref


def _identifier(prefix: str, value: str, length: int = 24) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def world_commit_identifier(
    tenant_id: str,
    stream_id: str,
    run_id: str,
    previous_revision: int,
) -> str:
    """Return the stable identity shared by the World receipt and integration event."""

    return _identifier(
        "commit_world",
        f"{tenant_id}:{stream_id}:{run_id}:{previous_revision}",
    )


class _AbortWorldCommit(Exception):
    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.error.code)
        self.failure = failure


class PostgresWorldTransactionParticipant:
    """Internal same-connection participant; it never commits or closes the connection."""

    def __init__(self, world_engine: WateringWorldEngine) -> None:
        self._world_engine = world_engine

    async def commit_on(
        self,
        connection: _Connection,
        request: WorldAtomicCommit,
        context: OperationContext,
    ) -> Result[WorldAtomicCommitReceipt]:
        identity_failure = self._validate_operation_identity(request, context)
        if identity_failure is not None:
            return identity_failure

        # Keep the global mutation lock order aligned with Agent invocation:
        # Command authority first, then the World row, then its Event stream.
        # The public UoW can otherwise deadlock with an invocation that already
        # holds the Command while waiting for this World.
        command_failure = await self._validate_command(connection, context)
        if command_failure is not None:
            return command_failure

        world_cursor = await connection.execute(
            """
            SELECT actor_id,content_hash,stream_id,revision,last_event_sequence,
                   state_hash,world_rules_version,state_json,request_context_json,updated_at
            FROM yaya_worlds
            WHERE tenant_id=%s AND world_id=%s AND actor_id=%s AND content_hash=%s
            FOR UPDATE
            """,
            (
                context.actor.tenant_id,
                request.command.world_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
            ),
        )
        row = await world_cursor.fetchone()
        if row is None:
            return _failure("NOT_FOUND", "World was not found inside the requested authority")
        try:
            stored_context = decode_as(row["request_context_json"], RequestContext)
            state = cast(FrozenJsonObject, row["state_json"])
            state_hash = canonical_json_sha256(state)
        except (TypeError, ValueError) as error:
            return _failure(
                "INVARIANT_VIOLATION",
                "Persisted World state or origin context is invalid",
                {"exception_type": type(error).__name__},
            )
        if not _same_authority(stored_context, context):
            return _failure("AUTHORIZATION_DENIED", "World origin authority does not match")
        if row["stream_id"] != request.stream_id:
            return _failure(
                "INVARIANT_VIOLATION",
                "WorldAtomicCommit stream_id does not equal the durable World stream",
            )
        if row["world_rules_version"] != request.command.world_rules_version:
            return _failure(
                "CONTENT_VERSION_MISMATCH",
                "World rules version does not match the pinned World",
            )
        if row["state_hash"] != state_hash:
            return _failure(
                "INVARIANT_VIOLATION",
                "Persisted World state hash does not match its canonical state",
            )
        if row["revision"] != request.command.expected_world_revision:
            return _failure(
                "WORLD_REVISION_CONFLICT",
                "World revision changed before the atomic commit",
                {
                    "expected": request.command.expected_world_revision,
                    "actual": row["revision"],
                },
            )
        expected_sequence = request.expected_stream_sequence
        if expected_sequence == "NO_STREAM":
            return _failure(
                "EVENT_SEQUENCE_GAP",
                "An existing World stream cannot use NO_STREAM",
            )
        durable_sequence = cast(int, row["last_event_sequence"])
        if expected_sequence != durable_sequence:
            return _failure(
                "EVENT_SEQUENCE_GAP",
                "WorldAtomicCommit expected stream sequence is stale",
                {"expected": expected_sequence, "actual": durable_sequence},
            )
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"{context.actor.tenant_id}:{request.stream_id}",),
        )
        sequence_cursor = await connection.execute(
            """
            SELECT COALESCE(MAX(sequence),0) AS value FROM yaya_events
            WHERE tenant_id=%s AND stream_id=%s
            """,
            (context.actor.tenant_id, request.stream_id),
        )
        sequence_row = await sequence_cursor.fetchone()
        actual_sequence = 0 if sequence_row is None else cast(int, sequence_row["value"])
        if actual_sequence != durable_sequence:
            return _failure(
                "EVENT_SEQUENCE_GAP",
                "World row and durable Event stream disagree",
                {"world_sequence": durable_sequence, "event_sequence": actual_sequence},
            )

        try:
            snapshot = WorldSnapshot(
                request_context=stored_context,
                world_id=request.command.world_id,
                revision=cast(int, row["revision"]),
                last_event_sequence=durable_sequence,
                state_hash=cast(str, row["state_hash"]),
                generated_at=cast(datetime, row["updated_at"]),
                world_rules_version=cast(str, row["world_rules_version"]),
                state=state,
            )
            proposal = self._world_engine.stage(
                snapshot,
                request.command.skill_ref,
                request.command.intents,
            )
        except WorldRuleViolation as error:
            return _failure(
                error.code,
                str(error),
                {"reason": error.reason},
            )
        except (TypeError, ValueError) as error:
            return _failure(
                "INVARIANT_VIOLATION",
                "WorldEngine could not stage the typed World command",
                {"exception_type": type(error).__name__},
            )
        if not proposal.commit_eligible:
            return _failure(
                "WORLD_RULE_REJECTED",
                "World command did not satisfy the task commit rule",
                {"reason": proposal.failure_key or "TASK_INCOMPLETE"},
            )
        event_plan = self._build_world_committed_event(
            request,
            context,
            proposal.state_hash,
            proposal.revision_after,
            durable_sequence,
        )
        if isinstance(event_plan, Failure):
            return event_plan
        outbox_plan = await self._plan_outbox(connection, request, context)
        if isinstance(outbox_plan, Failure):
            return outbox_plan
        receipt_messages, messages_to_insert = outbox_plan.value

        committed_event = event_plan
        committed_at = committed_event.occurred_at
        committed_events = (committed_event,)
        next_sequence = durable_sequence + 1
        updated = await connection.execute(
            """
            UPDATE yaya_worlds
            SET revision=%s,last_event_sequence=%s,state_hash=%s,
                state_json=%s,updated_at=%s
            WHERE tenant_id=%s AND world_id=%s AND actor_id=%s
              AND content_hash=%s AND stream_id=%s AND revision=%s
              AND last_event_sequence=%s AND state_hash=%s
            """,
            (
                proposal.revision_after,
                next_sequence,
                proposal.state_hash,
                Jsonb(plain(proposal.staged_state)),
                committed_at,
                context.actor.tenant_id,
                request.command.world_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
                request.stream_id,
                snapshot.revision,
                durable_sequence,
                snapshot.state_hash,
            ),
        )
        if updated.rowcount != 1:
            return _failure("WORLD_REVISION_CONFLICT", "World CAS did not have one winner")

        for event in committed_events:
            event_wire = cast(dict[str, object], plain(event))
            await connection.execute(
                """
                INSERT INTO yaya_events(
                    tenant_id,event_id,stream_id,sequence,event_type,event_json,occurred_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    event.event_id,
                    event.stream_id,
                    event.sequence,
                    event.event_type,
                    Jsonb(event_wire),
                    event.occurred_at,
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_projection_outbox(
                    tenant_id,message_id,destination,idempotency_key,payload_sha256,
                    payload_json,status,attempt
                ) VALUES (%s,%s,'world_events',%s,%s,%s,'PENDING',0)
                """,
                (
                    context.actor.tenant_id,
                    _identifier("outbox_world", event.event_id),
                    event.event_id,
                    canonical_json_sha256(event_wire),
                    Jsonb(event_wire),
                ),
            )
        for message in messages_to_insert:
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

        event_receipt = EventAppendReceipt(
            stream_id=request.stream_id,
            previous_sequence=durable_sequence,
            next_sequence=next_sequence,
            events=committed_events,
        )
        world_receipt = WorldCommitReceipt(
            world_id=request.command.world_id,
            previous_revision=snapshot.revision,
            world_revision=proposal.revision_after,
            first_event_sequence=durable_sequence + 1,
            last_event_sequence=next_sequence,
            committed_at=committed_at,
            state_hash=proposal.state_hash,
        )
        return Success(
            WorldAtomicCommitReceipt(
                stream_id=request.stream_id,
                world=world_receipt,
                events=event_receipt,
                outbox_messages=receipt_messages,
            )
        )

    @staticmethod
    def _validate_operation_identity(
        request: WorldAtomicCommit,
        context: OperationContext,
    ) -> Failure | None:
        for event in request.events:
            if (
                event.command_id != context.command_id
                or event.trace_id != context.trace_id
                or event.correlation_id != context.correlation_id
                or event.content_ref != context.content_ref
            ):
                return _failure(
                    "AUTHORIZATION_DENIED",
                    "World event identity does not match OperationContext",
                )
        for message in request.outbox_messages:
            origin = message.operation_context
            if (
                not _same_actor(origin.actor, context.actor)
                or origin.content_ref != context.content_ref
                or origin.command_id != context.command_id
                or origin.trace_id != context.trace_id
                or origin.correlation_id != context.correlation_id
            ):
                return _failure(
                    "AUTHORIZATION_DENIED",
                    "World outbox identity does not match OperationContext",
                )
        return None

    @staticmethod
    async def _validate_command(
        connection: _Connection,
        context: OperationContext,
    ) -> Failure | None:
        cursor = await connection.execute(
            """
            SELECT revision,status,record_json FROM yaya_commands
            WHERE tenant_id=%s AND command_id=%s AND actor_id=%s AND content_hash=%s
            FOR KEY SHARE
            """,
            (
                context.actor.tenant_id,
                context.command_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            return _failure("AUTHORIZATION_DENIED", "World commit Command is not authorized")
        try:
            command = decode_as(row["record_json"], CommandRecord)
        except (TypeError, ValueError) as error:
            return _failure(
                "INVARIANT_VIOLATION",
                "Persisted Command is invalid",
                {"exception_type": type(error).__name__},
            )
        allowed = {
            CommandStatus.ACCEPTED,
            CommandStatus.VALIDATING,
            CommandStatus.RUNNING_SANDBOX,
            CommandStatus.APPLYING_WORLD,
        }
        if (
            command.command_id != context.command_id
            or command.revision != row["revision"]
            or command.status.value != row["status"]
            or command.status not in allowed
            or not _same_authority(command.request_context, context)
        ):
            return _failure(
                "AUTHORIZATION_DENIED",
                "World commit Command identity or lifecycle is not authorized",
            )
        return None

    @staticmethod
    def _build_world_committed_event(
        request: WorldAtomicCommit,
        context: OperationContext,
        state_hash: str,
        world_revision: int,
        durable_sequence: int,
    ) -> RuntimeEvent | Failure:
        if len(request.events) != 1:
            return _failure(
                "INVARIANT_VIOLATION",
                "A World CAS must publish exactly one world.committed event",
            )
        if not all(isinstance(intent, WaterIntent) for intent in request.command.intents):
            return _failure(
                "INVARIANT_VIOLATION",
                "Watering World commit contains a non-watering intent",
            )
        event = request.events[0]
        payload = dict(event.payload)
        expected_fields = {
            "commit_id": world_commit_identifier(
                context.actor.tenant_id,
                request.stream_id,
                request.command.run_id,
                request.command.expected_world_revision,
            ),
            "run_id": request.command.run_id,
            "world_id": request.command.world_id,
            "previous_world_revision": request.command.expected_world_revision,
            "world_revision": world_revision,
            "state_hash": state_hash,
            "applied_intent_ids": tuple(intent.intent_id for intent in request.command.intents),
        }
        if any(payload.get(key) != value for key, value in expected_fields.items()):
            return _failure(
                "INVARIANT_VIOLATION",
                "world.committed payload differs from the staged World mutation",
            )
        committed_text = payload.get("committed_at")
        if not isinstance(committed_text, str):
            return _failure(
                "INVARIANT_VIOLATION",
                "world.committed committed_at is not an RFC 3339 timestamp",
            )
        try:
            committed_at = datetime.fromisoformat(committed_text.replace("Z", "+00:00"))
            if committed_at.tzinfo is None:
                raise ValueError("timestamp must include an offset")
            committed_at = committed_at.astimezone(UTC)
            return RuntimeEvent(
                event_id=_identifier(
                    "evt_world",
                    (
                        f"{context.actor.tenant_id}:{request.stream_id}:"
                        f"{request.command.run_id}:{durable_sequence + 1}"
                    ),
                ),
                event_type=event.event_type,
                event_version=event.event_version,
                stream_id=request.stream_id,
                sequence=durable_sequence + 1,
                occurred_at=committed_at,
                producer=event.producer,
                trace_id=event.trace_id,
                command_id=event.command_id,
                correlation_id=event.correlation_id,
                causation_id=event.causation_id,
                content_ref=event.content_ref,
                payload=event.payload,
                schema_version=event.schema_version,
            )
        except (TypeError, ValueError) as error:
            return _failure(
                "INVARIANT_VIOLATION",
                "world.committed does not satisfy the frozen runtime event contract",
                {"exception_type": type(error).__name__},
            )

    @staticmethod
    async def _plan_outbox(
        connection: _Connection,
        request: WorldAtomicCommit,
        context: OperationContext,
    ) -> Result[tuple[tuple[OutboxMessage, ...], tuple[OutboxMessage, ...]]]:
        seen_scopes: set[tuple[str, str, str]] = set()
        seen_ids: set[str] = set()
        receipt: list[OutboxMessage] = []
        inserts: list[OutboxMessage] = []
        for message in request.outbox_messages:
            if message.idempotency_scope in seen_scopes or message.message_id in seen_ids:
                return _failure(
                    "INVARIANT_VIOLATION",
                    "WorldAtomicCommit contains duplicate outbox identity",
                )
            seen_scopes.add(message.idempotency_scope)
            seen_ids.add(message.message_id)
            cursor = await connection.execute(
                """
                SELECT message_id,payload_sha256,message_json FROM yaya_outbox
                WHERE tenant_id=%s AND destination=%s AND idempotency_key=%s
                FOR UPDATE
                """,
                message.idempotency_scope,
            )
            row = await cursor.fetchone()
            if row is None:
                receipt.append(message)
                inserts.append(message)
                continue
            if row["payload_sha256"] != message.payload_sha256:
                return _failure(
                    "IDEMPOTENCY_KEY_REUSED",
                    "World outbox key was used for a different payload",
                )
            try:
                existing = decode_as(row["message_json"], OutboxMessage)
            except (TypeError, ValueError) as error:
                return _failure(
                    "INVARIANT_VIOLATION",
                    "Persisted outbox message is invalid",
                    {"exception_type": type(error).__name__},
                )
            if (
                existing.message_id != row["message_id"]
                or existing.payload_sha256 != row["payload_sha256"]
                or existing.operation_context.actor.tenant_id != context.actor.tenant_id
            ):
                return _failure(
                    "INVARIANT_VIOLATION",
                    "Persisted outbox identity drifted",
                )
            receipt.append(existing)
        return Success((tuple(receipt), tuple(inserts)))


class PostgresWorldUnitOfWork:
    """Production WorldUnitOfWorkPort with an injectable transaction participant."""

    def __init__(
        self,
        database: PostgresDatabase,
        world_engine: WateringWorldEngine,
    ) -> None:
        self._database = database
        self._participant = PostgresWorldTransactionParticipant(world_engine)

    @property
    def participant(self) -> PostgresWorldTransactionParticipant:
        return self._participant

    async def commit(
        self,
        request: WorldAtomicCommit,
        context: OperationContext,
    ) -> Result[WorldAtomicCommitReceipt]:
        try:
            async with self._database.transaction_with_commit_boundary() as connection:
                result = await self._participant.commit_on(connection, request, context)
                if isinstance(result, Failure):
                    raise _AbortWorldCommit(result)
                return result
        except _AbortWorldCommit as error:
            return error.failure
        except PostgresCommitStateUnknown as error:
            return _failure(
                "UNKNOWN_COMMIT_STATE",
                "PostgreSQL did not confirm the World atomic COMMIT",
                {"exception_type": type(error.__cause__).__name__},
            )
        except psycopg.IntegrityError as error:
            return _failure(
                "INVARIANT_VIOLATION",
                "PostgreSQL rejected an invalid World atomic record",
                {"sqlstate": error.sqlstate or "UNKNOWN"},
            )
        except psycopg.Error as error:
            return _failure(
                "DEPENDENCY_UNAVAILABLE",
                "PostgreSQL rolled back the World atomic commit",
                {"sqlstate": error.sqlstate or "UNKNOWN"},
            )
        except (TypeError, ValueError) as error:
            return _failure(
                "INVARIANT_VIOLATION",
                "World atomic commit could not produce a trustworthy receipt",
                {"exception_type": type(error).__name__},
            )


__all__ = [
    "PostgresWorldTransactionParticipant",
    "PostgresWorldUnitOfWork",
    "world_commit_identifier",
]
