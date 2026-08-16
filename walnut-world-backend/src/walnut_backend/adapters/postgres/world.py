"""The sole PostgreSQL write path for World snapshots, events, and outbox."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    Failure,
    OperationContext,
    Result,
    RuntimeEvent,
    Success,
    WorldAtomicCommit,
    WorldAtomicCommitReceipt,
    WorldCommitReceipt,
    WorldSnapshot,
    canonical_json_sha256,
)

from walnut_backend.domain.world.engine import WorldEngine, WorldTransition
from walnut_backend.domain.world.rules import WorldRules
from walnut_backend.domain.world.state import WorldRuleViolation

from .event_store import _conflict, _invariant, _StreamConflict, append_events_in_session
from .models import (
    OutboxRow,
    WorldSnapshotRow,
    outbox_message_data,
    world_snapshot_data,
    world_snapshot_from_data,
)
from .world_presentation import stage_world_presentation


class PostgresWorld:
    """Read-only WorldPort implementation."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def get_snapshot(self, world_id: str, context: OperationContext) -> Result[WorldSnapshot]:
        async with self._sessions() as session:
            row = await session.scalar(
                select(WorldSnapshotRow).where(
                    WorldSnapshotRow.world_id == world_id,
                    WorldSnapshotRow.tenant_id == context.actor.tenant_id,
                    WorldSnapshotRow.actor_id == context.actor.actor_id,
                    WorldSnapshotRow.content_hash == context.content_ref.content_hash,
                )
            )
        if row is None:
            return Failure(_not_found())
        snapshot = _read_snapshot(row)
        if snapshot is None:
            return Failure(_invariant("READ", "World snapshot durable authority drifted"))
        return Success(snapshot)

    async def get_actor_snapshot(
        self, world_id: str, context: OperationContext
    ) -> Result[WorldSnapshot]:
        """Resolve public read authority from the opaque World ID and actor."""

        async with self._sessions() as session:
            row = await session.scalar(
                select(WorldSnapshotRow).where(
                    WorldSnapshotRow.world_id == world_id,
                    WorldSnapshotRow.tenant_id == context.actor.tenant_id,
                    WorldSnapshotRow.actor_id == context.actor.actor_id,
                )
            )
        if row is None:
            return Failure(_not_found())
        snapshot = _read_snapshot(row)
        if snapshot is None:
            return Failure(_invariant("READ", "World snapshot durable authority drifted"))
        return Success(snapshot)

    async def get_latest_snapshot(self, context: OperationContext) -> Result[WorldSnapshot]:
        """Return the caller's most recently generated authoritative World."""
        async with self._sessions() as session:
            row = await session.scalar(
                select(WorldSnapshotRow)
                .where(
                    WorldSnapshotRow.tenant_id == context.actor.tenant_id,
                    WorldSnapshotRow.actor_id == context.actor.actor_id,
                )
                .order_by(WorldSnapshotRow.generated_at.desc())
                .limit(1)
            )
        if row is None:
            return Failure(_not_found())
        snapshot = _read_snapshot(row)
        if snapshot is None:
            return Failure(_invariant("READ", "World snapshot durable authority drifted"))
        return Success(snapshot)


def _read_snapshot(row: WorldSnapshotRow) -> WorldSnapshot | None:
    try:
        snapshot = world_snapshot_from_data(row.snapshot_json)
    except (KeyError, TypeError, ValueError):
        return None
    origin = snapshot.request_context
    if (
        origin.actor.tenant_id != row.tenant_id
        or origin.actor.actor_id != row.actor_id
        or origin.content_ref.content_hash != row.content_hash
        or snapshot.world_id != row.world_id
        or snapshot.revision != row.revision
        or snapshot.last_event_sequence != row.last_event_sequence
        or snapshot.state_hash != row.state_hash
        or canonical_json_sha256(snapshot.state) != snapshot.state_hash
        or snapshot.generated_at != row.generated_at
    ):
        return None
    return snapshot


class PostgresWorldUnitOfWork:
    """Recompute a typed command from a durable snapshot under CAS protection."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        rules_by_version: Mapping[str, WorldRules],
        *,
        world_engine: WorldEngine | None = None,
    ) -> None:
        self._sessions = session_factory
        self._rules_by_version = dict(rules_by_version)
        self._engine = world_engine or WorldEngine()

    async def commit(
        self, request: WorldAtomicCommit, context: OperationContext
    ) -> Result[WorldAtomicCommitReceipt]:
        async with self._sessions() as session, session.begin():
            return await self.commit_in_session(session, request, context)

    async def commit_in_session(
        self,
        session: AsyncSession,
        request: WorldAtomicCommit,
        context: OperationContext,
    ) -> Result[WorldAtomicCommitReceipt]:
        """Participate in the caller's transaction without creating a second commit."""

        if request.stream_id != f"world:{request.command.world_id}":
            return Failure(_invariant("WORLD_COMMIT", "stream_id must match command.world_id"))
        rules = self._rules_by_version.get(request.command.world_rules_version)
        if rules is None:
            return Failure(_content_mismatch("world rules version is not activated"))
        try:
            # A caller-owned transaction must not retain a partially staged
            # event/snapshot when a later outbox or serialization invariant
            # fails.  The savepoint is released only for a complete receipt;
            # every mapped failure rolls it back before returning to the
            # workflow transaction.
            async with session.begin_nested():
                return await self._commit_staged(
                    session,
                    request,
                    context,
                    rules,
                )
        except _StreamConflict:
            return Failure(_conflict("WORLD_COMMIT", "world revision or stream sequence is stale"))
        except WorldRuleViolation as error:
            return Failure(_world_rule(error.code, str(error)))
        except (KeyError, TypeError, ValueError) as error:
            return Failure(_invariant("WORLD_COMMIT", str(error)))

    async def _commit_staged(
        self,
        session: AsyncSession,
        request: WorldAtomicCommit,
        context: OperationContext,
        rules: WorldRules,
    ) -> Result[WorldAtomicCommitReceipt]:
        _validate_operation_identity(request, context)
        existing = await session.scalar(
            select(WorldSnapshotRow)
            .where(
                WorldSnapshotRow.world_id == request.command.world_id,
                WorldSnapshotRow.tenant_id == context.actor.tenant_id,
                WorldSnapshotRow.actor_id == context.actor.actor_id,
                WorldSnapshotRow.content_hash == context.content_ref.content_hash,
            )
            .with_for_update()
        )
        if existing is None:
            return Failure(_not_found())
        snapshot = world_snapshot_from_data(existing.snapshot_json)
        _validate_snapshot(request, context, snapshot, existing)
        transition = self._engine.apply(snapshot.state, request.command.intents, rules)
        committed_at = _validated_world_event(
            request, context, transition, snapshot.revision, snapshot.last_event_sequence
        )
        events = await append_events_in_session(
            session,
            request.stream_id,
            request.expected_stream_sequence,
            request.events,
            context,
            world_id=request.command.world_id,
            event_model=RuntimeEvent,
            occurred_at=committed_at,
        )
        if events.next_sequence != snapshot.last_event_sequence + 1:
            raise _StreamConflict
        next_snapshot = _next_snapshot(snapshot, transition, events.next_sequence, committed_at)
        await stage_world_presentation(
            session,
            tenant_id=context.actor.tenant_id,
            actor_id=context.actor.actor_id,
            content_hash=context.content_ref.content_hash,
            command_id=context.command_id,
            run_id=request.command.run_id,
            world_id=request.command.world_id,
            commit_id=world_commit_identifier(
                context.actor.tenant_id,
                request.stream_id,
                request.command.run_id,
                snapshot.revision,
            ),
            previous_world_revision=snapshot.revision,
            previous_world_event_sequence=snapshot.last_event_sequence,
            previous_snapshot_state_hash=snapshot.state_hash,
            world_revision=next_snapshot.revision,
            world_event_sequence=next_snapshot.last_event_sequence,
            final_snapshot_state_hash=next_snapshot.state_hash,
            occurred_at=committed_at,
            transition=transition,
        )
        existing.revision = next_snapshot.revision
        existing.last_event_sequence = next_snapshot.last_event_sequence
        existing.state_hash = next_snapshot.state_hash
        existing.generated_at = next_snapshot.generated_at
        existing.snapshot_json = world_snapshot_data(next_snapshot)
        outbox_messages = await _insert_outbox(session, request, context)
        receipt = WorldCommitReceipt(
            world_id=next_snapshot.world_id,
            previous_revision=snapshot.revision,
            world_revision=next_snapshot.revision,
            first_event_sequence=events.previous_sequence + 1,
            last_event_sequence=events.next_sequence,
            committed_at=committed_at,
            state_hash=next_snapshot.state_hash,
        )
        return Success(
            WorldAtomicCommitReceipt(
                stream_id=request.stream_id,
                world=receipt,
                events=events,
                outbox_messages=outbox_messages,
            )
        )


def world_commit_identifier(
    tenant_id: str, stream_id: str, run_id: str, previous_revision: int
) -> str:
    value = f"{tenant_id}:{stream_id}:{run_id}:{previous_revision}"
    return f"commit_world_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


def _validate_operation_identity(request: WorldAtomicCommit, context: OperationContext) -> None:
    if len(request.events) != 1:
        raise ValueError("a world commit must publish exactly one world.committed event")
    event = request.events[0]
    if (
        event.command_id != context.command_id
        or event.trace_id != context.trace_id
        or event.correlation_id != context.correlation_id
        or event.content_ref != context.content_ref
    ):
        raise ValueError("world event identity must match OperationContext")
    for message in request.outbox_messages:
        if message.operation_context != context:
            raise ValueError("world outbox identity must match OperationContext")


def _validate_snapshot(
    request: WorldAtomicCommit,
    context: OperationContext,
    snapshot: WorldSnapshot,
    row: WorldSnapshotRow,
) -> None:
    if snapshot.world_id != request.command.world_id:
        raise ValueError("durable snapshot identity is inconsistent")
    origin = snapshot.request_context
    if origin.actor != context.actor or origin.content_ref != context.content_ref:
        raise ValueError("durable snapshot authority is inconsistent")
    if snapshot.world_rules_version != request.command.world_rules_version:
        raise ValueError("world rules version does not match the durable snapshot")
    if snapshot.revision != request.command.expected_world_revision or row.revision != snapshot.revision:
        raise _StreamConflict
    if snapshot.last_event_sequence != row.last_event_sequence:
        raise ValueError("snapshot event sequence is inconsistent")
    if canonical_json_sha256(snapshot.state) != snapshot.state_hash or row.state_hash != snapshot.state_hash:
        raise ValueError("durable snapshot state hash is inconsistent")
    expected_sequence = 0 if request.expected_stream_sequence == "NO_STREAM" else request.expected_stream_sequence
    if expected_sequence != snapshot.last_event_sequence:
        raise _StreamConflict


def _validated_world_event(
    request: WorldAtomicCommit,
    context: OperationContext,
    transition: WorldTransition,
    previous_revision: int,
    previous_sequence: int,
) -> datetime:
    event = request.events[0]
    payload = dict(event.payload)
    expected = {
        "commit_id": world_commit_identifier(
            context.actor.tenant_id,
            request.stream_id,
            request.command.run_id,
            previous_revision,
        ),
        "run_id": request.command.run_id,
        "world_id": request.command.world_id,
        "previous_world_revision": previous_revision,
        "world_revision": previous_revision + 1,
        "state_hash": transition.state_hash,
        "applied_intent_ids": transition.applied_intent_ids,
    }
    if event.event_type != "world.committed" or event.event_version != 1:
        raise ValueError("world mutation must publish world.committed version 1")
    if any(payload.get(field) != value for field, value in expected.items()):
        raise ValueError("world.committed payload differs from the staged world transition")
    committed_at = payload.get("committed_at")
    if not isinstance(committed_at, str):
        raise ValueError("world.committed committed_at must be an RFC 3339 string")
    timestamp = datetime.fromisoformat(committed_at.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("world.committed committed_at must carry an offset")
    timestamp = timestamp.astimezone(UTC)
    RuntimeEvent(
        event_id="evt_world_contract_validation",
        event_type=event.event_type,
        event_version=event.event_version,
        stream_id=request.stream_id,
        sequence=previous_sequence + 1,
        occurred_at=timestamp,
        producer=event.producer,
        trace_id=event.trace_id,
        command_id=event.command_id,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        content_ref=event.content_ref,
        payload=event.payload,
        schema_version=event.schema_version,
    )
    return timestamp


def _next_snapshot(
    snapshot: WorldSnapshot, transition: WorldTransition, sequence: int, committed_at: datetime
) -> WorldSnapshot:
    return WorldSnapshot(
        request_context=snapshot.request_context,
        world_id=snapshot.world_id,
        revision=snapshot.revision + 1,
        last_event_sequence=sequence,
        state_hash=transition.state_hash,
        generated_at=committed_at,
        world_rules_version=snapshot.world_rules_version,
        state=transition.state,
        state_schema_version=snapshot.state_schema_version,
    )


async def _insert_outbox(
    session: AsyncSession, request: WorldAtomicCommit, context: OperationContext
) -> tuple[Any, ...]:
    delivered: list[Any] = []
    for message in request.outbox_messages:
        tenant_id, destination, idempotency_key = message.idempotency_scope
        if tenant_id != context.actor.tenant_id:
            raise ValueError("outbox message tenant differs from OperationContext")
        inserted = await session.scalar(
            insert(OutboxRow)
            .values(
                message_id=message.message_id,
                tenant_id=tenant_id,
                destination=destination,
                idempotency_key=idempotency_key,
                payload_sha256=message.payload_sha256,
                status=message.status.value,
                attempt=message.attempt,
                next_attempt_at=message.next_attempt_at,
                lease_id=message.lease_id,
                lease_expires_at=message.lease_expires_at,
                created_at=message.created_at,
                message_json=outbox_message_data(message),
            )
            .on_conflict_do_nothing(constraint="uq_outbox_delivery_scope")
            .returning(OutboxRow.message_id)
        )
        if inserted is None:
            raise ValueError("outbox message conflicts with an existing delivery")
        delivered.append(message)
    return tuple(delivered)


def _not_found() -> Any:
    from yaya_agent_contracts import ContractError, ErrorCategory

    return ContractError(
        "NOT_FOUND", ErrorCategory.VALIDATION, False, "resource.not_found", "READ", "world snapshot not found"
    )


def _content_mismatch(message: str) -> Any:
    from yaya_agent_contracts import ContractError, ErrorCategory

    return ContractError(
        "CONTENT_VERSION_MISMATCH", ErrorCategory.VALIDATION, False,
        "content.version_mismatch", "WORLD_COMMIT", message,
    )


def _world_rule(code: str, message: str) -> Any:
    from yaya_agent_contracts import ContractError, ErrorCategory

    return ContractError(code, ErrorCategory.WORLD_RULE, False, "world.rule_rejected", "WORLD_COMMIT", message)
