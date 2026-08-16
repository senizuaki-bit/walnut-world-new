"""Atomic ordered ClientEvent batch ingestion."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    CommandCreateReceipt,
    CommandStatus,
    CommandTransition,
    ContentRef,
    Failure,
    NewCommand,
    OperationContext,
    Result,
    Success,
)

from .command_store import PostgresCommandStore
from .models import AgentSessionRow, ClientEventRow


class PostgresClientEventStore:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], command_store: PostgresCommandStore
    ) -> None:
        self._sessions = session_factory
        self._command_store = command_store

    async def accept(
        self, command: NewCommand, body: Mapping[str, Any], context: OperationContext
    ) -> Result[CommandCreateReceipt]:
        validation_error = _validate_ordered_batch(body)
        if validation_error:
            return Failure(
                _error("EVENT_SEQUENCE_GAP", "VALIDATE", validation_error, retryable=True)
            )
        session_id = _string(body, "session_id")
        try:
            async with self._sessions() as session, session.begin():
                owner = await session.scalar(
                    select(AgentSessionRow)
                    .where(
                        AgentSessionRow.tenant_id == context.actor.tenant_id,
                        AgentSessionRow.actor_id == context.actor.actor_id,
                        AgentSessionRow.session_id == session_id,
                    )
                    .with_for_update()
                )
                if owner is None:
                    return Failure(
                        _error("NOT_FOUND", "READ", "agent session not found", retryable=False)
                    )
                if owner.world_id != body.get("world_id"):
                    return Failure(
                        _error(
                            "INVALID_REQUEST",
                            "VALIDATE",
                            "batch world differs from session",
                            retryable=False,
                        )
                    )
                command_context = replace(
                    context, content_ref=ContentRef(**owner.session_json["content"])
                )
                command_result = await self._command_store.accept_once_in_session(
                    session, command, command_context
                )
                if isinstance(command_result, Failure):
                    return command_result
                receipt = command_result.value
                if not receipt.created:
                    return Success(receipt)
                accepted_count = 0
                duplicate_count = 0
                for event in body["events"]:
                    existing = await session.scalar(
                        select(ClientEventRow)
                        .where(
                            ClientEventRow.event_id == event["event_id"],
                            ClientEventRow.tenant_id == context.actor.tenant_id,
                        )
                        .with_for_update()
                    )
                    if existing is not None:
                        if existing.event_json != event:
                            raise _BatchRejected(
                                _error(
                                    "INVARIANT_VIOLATION",
                                    "INGEST",
                                    "client event identity changed",
                                    retryable=False,
                                )
                            )
                        duplicate_count += 1
                        continue
                    same_sequence = await session.scalar(
                        select(ClientEventRow)
                        .where(
                            ClientEventRow.tenant_id == context.actor.tenant_id,
                            ClientEventRow.session_id == session_id,
                            ClientEventRow.sequence == event["sequence"],
                        )
                        .with_for_update()
                    )
                    if same_sequence is not None:
                        raise _BatchRejected(
                            _error(
                                "EVENT_SEQUENCE_GAP",
                                "INGEST",
                                "client sequence already belongs to another event",
                                retryable=True,
                            )
                        )
                    session.add(
                        ClientEventRow(
                            event_id=event["event_id"],
                            tenant_id=context.actor.tenant_id,
                            actor_id=context.actor.actor_id,
                            session_id=session_id,
                            world_id=body["world_id"],
                            sequence=event["sequence"],
                            occurred_at=_time(event["occurred_at"]),
                            event_json=dict(event),
                        )
                    )
                    accepted_count += 1
                validating_at = max(
                    await _database_now(session),
                    receipt.command.updated_at,
                )
                validating = replace(
                    receipt.command,
                    status=CommandStatus.VALIDATING,
                    stage="VALIDATE",
                    terminal=False,
                    updated_at=validating_at,
                    revision=receipt.command.revision + 1,
                )
                validation_transition = await self._command_store.transition_in_session(
                    session, CommandTransition(receipt.command, validating), command_context
                )
                if isinstance(validation_transition, Failure):
                    raise _BatchRejected(validation_transition.error)
                completed_at = max(
                    await _database_now(session),
                    validating.updated_at,
                )
                completed = replace(
                    validating,
                    status=CommandStatus.APPLIED,
                    stage="COMPLETE",
                    terminal=True,
                    updated_at=completed_at,
                    result={
                        "result_type": "CLIENT_EVENTS_ACCEPTED",
                        "batch_id": body["batch_id"],
                        "accepted_count": accepted_count,
                        "duplicate_count": duplicate_count,
                        "rejected_count": 0,
                    },
                    revision=validating.revision + 1,
                )
                transition = await self._command_store.transition_in_session(
                    session, CommandTransition(validating, completed), command_context
                )
                if isinstance(transition, Failure):
                    raise _BatchRejected(transition.error)
                return Success(receipt)
        except _BatchRejected as rejected:
            return Failure(rejected.error)


async def _database_now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RuntimeError("PostgreSQL returned an invalid ClientEvent timestamp")
    return value.astimezone(UTC)


def _validate_ordered_batch(body: Mapping[str, Any]) -> str | None:
    events = body.get("events")
    if not isinstance(events, list) or not events:
        return "client event batch is empty"
    if body.get("first_sequence") != events[0].get("sequence") or body.get(
        "last_sequence"
    ) != events[-1].get("sequence"):
        return "client event batch boundaries differ"
    ids: set[str] = set()
    for index, event in enumerate(events):
        if not isinstance(event, Mapping) or not isinstance(event.get("event_id"), str):
            return "client event is malformed"
        if event["event_id"] in ids:
            return "client event batch has duplicate event_id"
        ids.add(event["event_id"])
        if event.get("sequence") != body["first_sequence"] + index:
            return "client event batch is not gap-free"
    return None


def _string(value: Mapping[str, Any], key: str) -> str:
    item = value[key]
    if not isinstance(item, str):
        raise ValueError(f"{key} must be a string")
    return item


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("occurred_at must be a datetime")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _error(code: str, stage: str, message: str, *, retryable: bool) -> Any:
    from yaya_agent_contracts import ContractError, ErrorCategory

    metadata = {
        "NOT_FOUND": (ErrorCategory.VALIDATION, "resource.not_found"),
        "INVALID_REQUEST": (ErrorCategory.VALIDATION, "request.invalid"),
        "EVENT_SEQUENCE_GAP": (ErrorCategory.CONCURRENCY, "event.resync_required"),
        "INVARIANT_VIOLATION": (ErrorCategory.INVARIANT, "system.invariant_violation"),
    }[code]
    return ContractError(
        code=code,
        category=metadata[0],
        retryable=retryable,
        user_message_key=metadata[1],
        stage=stage,
        message=message,
    )


class _BatchRejected(Exception):
    def __init__(self, error: Any) -> None:
        self.error = error
