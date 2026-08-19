"""Append-only, secret-redacting PostgreSQL audit implementation."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    ActorRef,
    AuditQuery,
    AuditRecord,
    CursorPage,
    Failure,
    OperationContext,
    Result,
    Success,
)

from .models import AuditRow, json_value

_SECRET_MARKERS = ("secret", "token", "password", "authorization", "credential", "evidence")


def redact(value: Any, key: str = "") -> Any:
    """Never persist a secret-like payload/evidence field in audit JSON."""
    if any(marker in key.lower() for marker in _SECRET_MARKERS):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {item_key: redact(item_value, item_key) for item_key, item_value in value.items()}
    if isinstance(value, tuple | list):
        return [redact(item, key) for item in value]
    return value


def _audit_payload(record: AuditRecord) -> dict[str, Any]:
    return json_value(
        # Evidence references are opaque IDs, not payloads; retain them for traceability while
        # redacting any secret-like data included in ``details``.
        replace(record, details=redact(dict(record.details)))
    )


def _record_from_payload(data: dict[str, Any]) -> AuditRecord:
    return AuditRecord(
        audit_id=data["audit_id"],
        occurred_at=datetime.fromisoformat(data["occurred_at"].replace("Z", "+00:00")),
        operation=data["operation"],
        outcome=data["outcome"],
        actor=ActorRef(**data["actor"]),
        request_id=data["request_id"],
        correlation_id=data["correlation_id"],
        trace_id=data["trace_id"],
        resource_type=data["resource_type"],
        resource_id=data["resource_id"],
        purpose=data["purpose"],
        subject_hash=data["subject_hash"],
        evidence_ids=tuple(data["evidence_ids"]),
        error_code=data["error_code"],
        details=data["details"],
        schema_version=data["schema_version"],
        redacted=data["redacted"],
    )


async def append_in_session(
    session: AsyncSession, record: AuditRecord, context: OperationContext
) -> AuditRecord:
    if record.actor.tenant_id != context.actor.tenant_id:
        raise ValueError("audit tenant must match complete OperationContext")
    payload = _audit_payload(record)
    session.add(
        AuditRow(
            audit_id=record.audit_id,
            tenant_id=context.actor.tenant_id,
            occurred_at=record.occurred_at,
            operation=record.operation,
            outcome=record.outcome,
            record_json=payload,
        )
    )
    return _record_from_payload(payload)


class PostgresAudit:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def append(self, record: AuditRecord, context: OperationContext) -> Result[AuditRecord]:
        try:
            async with self._sessions() as session, session.begin():
                saved = await append_in_session(session, record, context)
            return Success(saved)
        except (TypeError, ValueError) as error:
            return Failure(_invariant_error("AUDIT", str(error)))

    async def query(
        self, query: AuditQuery, context: OperationContext
    ) -> Result[CursorPage[AuditRecord]]:
        statement: Select[tuple[AuditRow]] = select(AuditRow).where(
            AuditRow.tenant_id == context.actor.tenant_id
        )
        if query.operations:
            statement = statement.where(AuditRow.operation.in_(query.operations))
        if query.outcomes:
            statement = statement.where(AuditRow.outcome.in_(query.outcomes))
        if query.occurred_after:
            statement = statement.where(AuditRow.occurred_at > query.occurred_after)
        if query.occurred_before:
            statement = statement.where(AuditRow.occurred_at < query.occurred_before)
        if query.cursor:
            try:
                cursor_at, cursor_id = _decode_cursor(query.cursor)
            except ValueError:
                return Failure(_invariant_error("AUDIT", "invalid audit cursor"))
            statement = statement.where(
                or_(
                    AuditRow.occurred_at > cursor_at,
                    and_(AuditRow.occurred_at == cursor_at, AuditRow.audit_id > cursor_id),
                )
            )
        statement = statement.order_by(AuditRow.occurred_at, AuditRow.audit_id).limit(query.limit + 1)
        async with self._sessions() as session:
            rows = list((await session.scalars(statement)).all())
        has_more = len(rows) > query.limit
        records = tuple(_record_from_payload(row.record_json) for row in rows[: query.limit])
        next_cursor = _encode_cursor(rows[query.limit - 1]) if has_more and records else None
        return Success(CursorPage(items=records, next_cursor=next_cursor))


def system_audit_record(
    context: OperationContext, operation: str, resource_id: str, details: Mapping[str, Any]
) -> AuditRecord:
    return AuditRecord(
        # Not derived from the clock. This id was a microsecond timestamp, which
        # is only unique if the clock advances between two records -- and on
        # Windows the system clock moves in ~15.6ms steps, so two audit rows
        # written in the same transaction collide on the primary key and the
        # whole operation dies with an IntegrityError. That is what stopped
        # Builds: their accept phase writes several audit records in one
        # transaction, retried five times, and dead-lettered every time, while
        # single-record operations like a hint went on working. The same table is
        # already written elsewhere with a uuid; ordering comes from occurred_at.
        audit_id=f"audit_{uuid4().hex}",
        occurred_at=datetime.now(UTC),
        operation=operation,
        outcome="ALLOWED",
        actor=context.actor,
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        trace_id=context.trace_id,
        resource_type="COMMAND",
        resource_id=resource_id,
        purpose=None,
        subject_hash=None,
        evidence_ids=(),
        error_code=None,
        details=details,
    )


def _invariant_error(stage: str, message: str) -> Any:
    from yaya_agent_contracts import ContractError, ErrorCategory

    return ContractError(
        code="INVARIANT_VIOLATION",
        category=ErrorCategory.INVARIANT,
        retryable=False,
        user_message_key="system.invariant_violation",
        stage=stage,
        message=message[:512] or "audit invariant failed",
    )


def _encode_cursor(row: AuditRow) -> str:
    raw = json.dumps(
        {"occurred_at": row.occurred_at.isoformat(), "audit_id": row.audit_id},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(cursor + padding))
        occurred_at = datetime.fromisoformat(data["occurred_at"].replace("Z", "+00:00"))
        audit_id = data["audit_id"]
        if not isinstance(audit_id, str) or occurred_at.tzinfo is None:
            raise ValueError("invalid audit cursor")
        return occurred_at, audit_id
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
    ) as error:
        raise ValueError("invalid audit cursor") from error
