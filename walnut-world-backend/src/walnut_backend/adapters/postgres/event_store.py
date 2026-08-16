"""PostgreSQL append-only event store with stream-sequence CAS."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    CursorPage,
    DomainEvent,
    EventAppendReceipt,
    Failure,
    OperationContext,
    Result,
    Success,
    UncommittedEvent,
)

from .models import EventRow, WorldStreamRow, domain_event_data, domain_event_from_data


class PostgresEventStore:
    """Durable stream access; world snapshots are deliberately absent from this port."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def append(
        self,
        stream_id: str,
        expected_sequence: int | str,
        events: tuple[UncommittedEvent, ...],
        context: OperationContext,
    ) -> Result[EventAppendReceipt]:
        if not events:
            return Failure(_invariant("APPEND", "at least one event is required"))
        try:
            async with self._sessions() as session, session.begin():
                return Success(
                    await append_events_in_session(
                        session, stream_id, expected_sequence, events, context, world_id=None
                    )
                )
        except _StreamConflict:
            return Failure(_conflict("APPEND", "stale stream sequence"))
        except (TypeError, ValueError, KeyError) as error:
            return Failure(_invariant("APPEND", str(error)))

    async def read_stream(
        self, stream_id: str, after_sequence: int, limit: int, context: OperationContext
    ) -> Result[CursorPage[DomainEvent[Any]]]:
        if after_sequence < 0 or limit < 1:
            return Failure(_invariant("READ", "after_sequence must be non-negative and limit positive"))
        async with self._sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(EventRow)
                        .where(
                            EventRow.stream_id == stream_id,
                            EventRow.tenant_id == context.actor.tenant_id,
                            EventRow.sequence > after_sequence,
                        )
                        .order_by(EventRow.sequence)
                        .limit(limit + 1)
                    )
                ).all()
            )
        has_more = len(rows) > limit
        parsed = tuple(_event_from_row(row) for row in rows[:limit])
        if any(event is None for event in parsed):
            return Failure(_invariant("READ", "event durable identity drifted"))
        items = tuple(event for event in parsed if event is not None)
        return Success(CursorPage(items=items, next_cursor=str(items[-1].sequence) if has_more else None))

    async def get_by_id(
        self, event_id: str, context: OperationContext
    ) -> Result[DomainEvent[Any]]:
        async with self._sessions() as session:
            row = await session.scalar(
                select(EventRow).where(
                    EventRow.event_id == event_id,
                    EventRow.tenant_id == context.actor.tenant_id,
                )
            )
        if row is None:
            return Failure(_not_found())
        event = _event_from_row(row)
        if event is None:
            return Failure(_invariant("READ", "event durable identity drifted"))
        return Success(event)


def _event_from_row(row: EventRow) -> DomainEvent[Any] | None:
    try:
        event = domain_event_from_data(row.event_json)
    except (KeyError, TypeError, ValueError):
        return None
    if (
        event.event_id != row.event_id
        or event.stream_id != row.stream_id
        or event.sequence != row.sequence
        or event.occurred_at != row.occurred_at
    ):
        return None
    return event


class _StreamConflict(Exception):
    pass


async def append_events_in_session(
    session: AsyncSession,
    stream_id: str,
    expected_sequence: int | str,
    events: tuple[UncommittedEvent, ...],
    context: OperationContext,
    *,
    world_id: str | None,
    event_model: type[DomainEvent[Any]] = DomainEvent,
    occurred_at: datetime | None = None,
) -> EventAppendReceipt:
    """Claim a stream head and append a contiguous range in the caller transaction."""
    if expected_sequence == "NO_STREAM":
        created = await session.scalar(
            insert(WorldStreamRow)
            .values(
                stream_id=stream_id,
                tenant_id=context.actor.tenant_id,
                world_id=world_id,
                last_sequence=len(events),
            )
            .on_conflict_do_nothing(
                index_elements=[WorldStreamRow.tenant_id, WorldStreamRow.stream_id]
            )
            .returning(WorldStreamRow.stream_id)
        )
        if created is None:
            raise _StreamConflict
        previous_sequence = 0
    else:
        if not isinstance(expected_sequence, int) or expected_sequence < 0:
            raise ValueError("expected stream sequence is invalid")
        updated = await session.execute(
            update(WorldStreamRow)
            .where(
                WorldStreamRow.tenant_id == context.actor.tenant_id,
                WorldStreamRow.stream_id == stream_id,
                WorldStreamRow.last_sequence == expected_sequence,
            )
            .values(last_sequence=expected_sequence + len(events))
        )
        if getattr(updated, "rowcount", 0) != 1:
            raise _StreamConflict
        previous_sequence = expected_sequence

    event_occurred_at = occurred_at or datetime.now(UTC)
    committed: list[DomainEvent[Any]] = []
    for offset, event in enumerate(events, start=1):
        record = event_model(
            event_id=f"evt_{uuid4().hex}",
            event_type=event.event_type,
            event_version=event.event_version,
            stream_id=stream_id,
            sequence=previous_sequence + offset,
            occurred_at=event_occurred_at,
            producer=event.producer,
            trace_id=event.trace_id,
            command_id=event.command_id,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            content_ref=event.content_ref,
            payload=event.payload,
            schema_version=event.schema_version,
        )
        session.add(
            EventRow(
                event_id=record.event_id,
                tenant_id=context.actor.tenant_id,
                stream_id=record.stream_id,
                sequence=record.sequence,
                occurred_at=record.occurred_at,
                event_json=domain_event_data(record),
            )
        )
        committed.append(record)
    return EventAppendReceipt(
        stream_id=stream_id,
        previous_sequence=previous_sequence,
        next_sequence=previous_sequence + len(committed),
        events=tuple(committed),
    )


def _error(code: str, stage: str, message: str, retryable: bool) -> Any:
    from yaya_agent_contracts import ContractError, ErrorCategory

    category = ErrorCategory.CONCURRENCY if code == "WORLD_REVISION_CONFLICT" else ErrorCategory.INVARIANT
    key = "world.changed_retry" if code == "WORLD_REVISION_CONFLICT" else "system.invariant_violation"
    return ContractError(code=code, category=category, retryable=retryable, user_message_key=key, stage=stage, message=message)


def _conflict(stage: str, message: str) -> Any:
    return _error("WORLD_REVISION_CONFLICT", stage, message, True)


def _invariant(stage: str, message: str) -> Any:
    return _error("INVARIANT_VIOLATION", stage, message, False)


def _not_found() -> Any:
    from yaya_agent_contracts import ContractError, ErrorCategory

    return ContractError(code="NOT_FOUND", category=ErrorCategory.VALIDATION, retryable=False, user_message_key="resource.not_found", stage="READ", message="event not found")
