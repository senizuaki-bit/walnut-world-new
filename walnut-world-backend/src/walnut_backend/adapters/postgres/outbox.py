"""Leased PostgreSQL OutboxPort with replay and dead-letter state transitions."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    ContractError,
    DeliveryReceipt,
    Failure,
    OperationContext,
    OutboxMessage,
    OutboxStatus,
    Result,
    Success,
)

from .audit import append_in_session, system_audit_record
from .models import OutboxRow, outbox_message_data, outbox_message_from_data


class PostgresOutbox:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def enqueue(
        self, message: OutboxMessage, context: OperationContext
    ) -> Result[OutboxMessage]:
        if message.operation_context.actor.tenant_id != context.actor.tenant_id:
            return Failure(_invariant("OUTBOX", "message tenant differs from OperationContext"))
        tenant_id, destination, idempotency_key = message.idempotency_scope
        async with self._sessions() as session, session.begin():
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
            if inserted is not None:
                await append_in_session(
                    session,
                    system_audit_record(context, "OUTBOX_ENQUEUED", message.message_id, {"destination": destination}),
                    context,
                )
                return Success(message)
            existing = await session.scalar(
                select(OutboxRow).where(
                    OutboxRow.tenant_id == tenant_id,
                    OutboxRow.destination == destination,
                    OutboxRow.idempotency_key == idempotency_key,
                )
            )
            if existing is None:
                return Failure(_invariant("OUTBOX", "outbox receipt disappeared"))
            if existing.payload_sha256 != message.payload_sha256:
                return Failure(_idempotency_reused())
            return Success(cast(OutboxMessage, outbox_message_from_data(existing.message_json)))

    async def claim_ready(
        self, worker_id: str, limit: int, lease_seconds: int, context: OperationContext
    ) -> Result[tuple[OutboxMessage, ...]]:
        if limit < 1 or lease_seconds < 1:
            return Failure(_invariant("OUTBOX", "limit and lease_seconds must be positive"))
        async with self._sessions() as session, session.begin():
            database_now = await session.scalar(select(func.current_timestamp()))
            if not isinstance(database_now, datetime):
                return Failure(_invariant("OUTBOX", "database clock is unavailable"))
            now = database_now
            rows = list(
                (
                    await session.scalars(
                        select(OutboxRow)
                        .where(
                            OutboxRow.tenant_id == context.actor.tenant_id,
                            or_(
                                OutboxRow.status == OutboxStatus.PENDING.value,
                                and_retry_due(now),
                                and_lease_expired(now),
                            ),
                        )
                        .order_by(OutboxRow.created_at, OutboxRow.message_id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            claimed = []
            for row in rows:
                current = cast(OutboxMessage, outbox_message_from_data(row.message_json))
                attempt = current.attempt + 1
                payload = replace(current.payload, attempt=attempt)
                next_message = replace(
                    current,
                    status=OutboxStatus.SENDING,
                    attempt=attempt,
                    payload=payload,
                    next_attempt_at=None,
                    lease_id=f"lease_{worker_id}_{uuid4().hex}",
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    last_error=None,
                    delivery_receipt=None,
                    dead_lettered_at=None,
                )
                row.status = next_message.status.value
                row.attempt = next_message.attempt
                row.next_attempt_at = None
                row.lease_id = next_message.lease_id
                row.lease_expires_at = next_message.lease_expires_at
                row.message_json = outbox_message_data(next_message)
                await append_in_session(
                    session,
                    system_audit_record(context, "OUTBOX_CLAIMED", next_message.message_id, {"worker_id": worker_id}),
                    context,
                )
                claimed.append(next_message)
        return Success(tuple(claimed))

    async def mark_sent(
        self,
        message_id: str,
        lease_id: str,
        receipt: DeliveryReceipt,
        context: OperationContext,
    ) -> Result[OutboxMessage]:
        return await self._complete(message_id, lease_id, context, "sent", receipt=receipt)

    async def mark_retry(
        self,
        message_id: str,
        lease_id: str,
        error: ContractError,
        next_attempt_at: datetime,
        context: OperationContext,
    ) -> Result[OutboxMessage]:
        return await self._complete(
            message_id, lease_id, context, "retry", error=error, timestamp=next_attempt_at
        )

    async def mark_dead_letter(
        self,
        message_id: str,
        lease_id: str,
        error: ContractError,
        dead_lettered_at: datetime,
        context: OperationContext,
    ) -> Result[OutboxMessage]:
        return await self._complete(
            message_id, lease_id, context, "dead_letter", error=error, timestamp=dead_lettered_at
        )

    async def _complete(
        self,
        message_id: str,
        lease_id: str,
        context: OperationContext,
        action: str,
        *,
        receipt: DeliveryReceipt | None = None,
        error: ContractError | None = None,
        timestamp: datetime | None = None,
    ) -> Result[OutboxMessage]:
        async with self._sessions() as session, session.begin():
            row = await session.scalar(
                select(OutboxRow)
                .where(
                    OutboxRow.message_id == message_id,
                    OutboxRow.tenant_id == context.actor.tenant_id,
                    OutboxRow.status == OutboxStatus.SENDING.value,
                    OutboxRow.lease_id == lease_id,
                    OutboxRow.lease_expires_at > func.current_timestamp(),
                )
                .with_for_update()
            )
            if row is None:
                return Failure(_lease_conflict())
            current = cast(OutboxMessage, outbox_message_from_data(row.message_json))
            if action == "sent":
                next_message = replace(
                    current,
                    status=OutboxStatus.SENT,
                    lease_id=None,
                    lease_expires_at=None,
                    delivery_receipt=receipt,
                )
            elif action == "retry":
                next_message = replace(
                    current,
                    status=OutboxStatus.RETRYING,
                    lease_id=None,
                    lease_expires_at=None,
                    last_error=error,
                    next_attempt_at=timestamp,
                )
            else:
                next_message = replace(
                    current,
                    status=OutboxStatus.DEAD_LETTER,
                    lease_id=None,
                    lease_expires_at=None,
                    last_error=error,
                    dead_lettered_at=timestamp,
                )
            row.status = next_message.status.value
            row.next_attempt_at = next_message.next_attempt_at
            row.lease_id = None
            row.lease_expires_at = None
            row.message_json = outbox_message_data(next_message)
            await append_in_session(
                session,
                system_audit_record(context, f"OUTBOX_{action.upper()}", message_id, {}),
                context,
            )
        return Success(next_message)


def and_retry_due(now: datetime) -> Any:
    from sqlalchemy import and_

    return and_(OutboxRow.status == OutboxStatus.RETRYING.value, OutboxRow.next_attempt_at <= now)


def and_lease_expired(now: datetime) -> Any:
    from sqlalchemy import and_

    return and_(OutboxRow.status == OutboxStatus.SENDING.value, OutboxRow.lease_expires_at <= now)


def _error(code: str, stage: str, message: str, retryable: bool) -> Any:
    from yaya_agent_contracts import ContractError, ErrorCategory

    metadata = {
        "IDEMPOTENCY_KEY_REUSED": (ErrorCategory.CONCURRENCY, False, "request.idempotency_conflict"),
        "WORLD_REVISION_CONFLICT": (ErrorCategory.CONCURRENCY, True, "world.changed_retry"),
        "INVARIANT_VIOLATION": (ErrorCategory.INVARIANT, False, "system.invariant_violation"),
    }[code]
    return ContractError(
        code=code,
        category=metadata[0],
        retryable=retryable,
        user_message_key=metadata[2],
        stage=stage,
        message=message,
    )


def _idempotency_reused() -> Any:
    return _error("IDEMPOTENCY_KEY_REUSED", "OUTBOX", "outbox key has a different payload", False)


def _lease_conflict() -> Any:
    return _error("WORLD_REVISION_CONFLICT", "OUTBOX", "outbox lease is stale or unknown", True)


def _invariant(stage: str, message: str) -> Any:
    return _error("INVARIANT_VIOLATION", stage, message, False)
