"""Real PostgreSQL tests for audit redaction and stable cursor pagination."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from types import MappingProxyType
from uuid import uuid4

import pytest
from sqlalchemy import select
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    AuditQuery,
    AuditRecord,
    ContentRef,
    OperationContext,
)
from yaya_agent_runtime import AgentTraceEvent

from walnut_backend.adapters.postgres.agent_runtime import (
    AgentRuntimeAuthorityError,
    PostgresAgentTrace,
)
from walnut_backend.adapters.postgres.audit import PostgresAudit
from walnut_backend.adapters.postgres.models import AuditRow
from walnut_backend.adapters.postgres.session import create_session_factory


def test_audit_redacts_mapping_proxy_secrets_and_paginates_without_overlap() -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError("set WALNUT_TEST_DATABASE_URL for required PostgreSQL audit coverage")
    asyncio.run(_exercise_audit(database_url))


def test_agent_trace_retry_is_single_row_and_payload_drift_fails_closed() -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError("set WALNUT_TEST_DATABASE_URL for required PostgreSQL audit coverage")
    asyncio.run(_exercise_agent_trace_retry(database_url))


async def _exercise_audit(database_url: str) -> None:
    session_factory = create_session_factory(database_url)
    audit = PostgresAudit(session_factory)
    run_id = uuid4().hex
    context = make_context(run_id)
    occurred_at = datetime.now(UTC)
    try:
        secret_record = record(
            run_id,
            context,
            0,
            occurred_at,
            MappingProxyType({"nested": MappingProxyType({"password": "never-store-this"})}),
        )
        saved = await audit.append(secret_record, context)
        assert saved.ok
        persisted = await audit.query(AuditQuery(limit=1), context)
        assert persisted.ok
        assert persisted.value.items[0].details["nested"]["password"] == "[REDACTED]"

        for ordinal in (1, 2, 3):
            appended = await audit.append(
                record(run_id, context, ordinal, occurred_at, {}), context
            )
            assert appended.ok

        first_page = await audit.query(AuditQuery(limit=2), context)
        assert first_page.ok
        assert len(first_page.value.items) == 2
        assert first_page.value.next_cursor is not None

        second_page = await audit.query(
            AuditQuery(limit=2, cursor=first_page.value.next_cursor), context
        )
        assert second_page.ok
        assert {item.audit_id for item in first_page.value.items}.isdisjoint(
            item.audit_id for item in second_page.value.items
        )
        assert len(second_page.value.items) == 2

        malformed = await audit.query(AuditQuery(limit=1, cursor="not-a-valid-cursor"), context)
        assert not malformed.ok
        assert malformed.error.code == "INVARIANT_VIOLATION"
    finally:
        await session_factory.kw["bind"].dispose()


async def _exercise_agent_trace_retry(database_url: str) -> None:
    session_factory = create_session_factory(database_url)
    trace = PostgresAgentTrace(session_factory)
    run_id = uuid4().hex
    context = make_context(run_id)
    event = AgentTraceEvent(
        "agent.model.requested",
        f"turn_{run_id}",
        "teaching_agent",
        {
            "request_number": 1,
            "message_count": 2,
            "tool_round_complete": False,
            "session_run_count": 0,
            "skill_history_versions": [],
        },
    )
    try:
        await trace.record(event, context)
        await trace.record(event, context)
        rows = await _agent_trace_rows(session_factory, context)
        assert len(rows) == 1

        drifted = AgentTraceEvent(
            event.name,
            event.turn_id,
            event.role,
            {**dict(event.fields), "message_count": 3},
        )
        with pytest.raises(AgentRuntimeAuthorityError, match="immutable audit bytes"):
            await trace.record(drifted, context)

        assert await _agent_trace_rows(session_factory, context) == rows
    finally:
        await session_factory.kw["bind"].dispose()


async def _agent_trace_rows(
    session_factory: object,
    context: OperationContext,
) -> list[tuple[str, str, datetime, str, str, str]]:
    async with session_factory() as session:  # type: ignore[operator]
        rows = list(
            await session.scalars(
                select(AuditRow)
                .where(
                    AuditRow.tenant_id == context.actor.tenant_id,
                    AuditRow.operation == "AGENT_RUNTIME_TRACE",
                    AuditRow.record_json["command_id"].astext == context.command_id,
                )
                .order_by(AuditRow.audit_id)
            )
        )
        return [
            (
                row.audit_id,
                row.tenant_id,
                row.occurred_at,
                row.operation,
                row.outcome,
                json.dumps(
                    row.record_json,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
            )
            for row in rows
        ]


def make_context(run_id: str) -> OperationContext:
    return OperationContext(
        request_id=f"req_{run_id}",
        correlation_id=f"corr_{run_id}",
        trace_id=f"trace_{run_id}",
        requested_at=datetime.now(UTC),
        actor=ActorRef(f"tenant_{run_id}", f"actor_{run_id}", ActorType.TEACHER),
        content_ref=ContentRef("UNIT_TEST", "1.0.0", "0" * 64),
        command_id=f"cmd_{run_id}",
        causation_id=None,
    )


def record(
    run_id: str,
    context: OperationContext,
    ordinal: int,
    occurred_at: datetime,
    details: object,
) -> AuditRecord:
    return AuditRecord(
        audit_id=f"audit_{run_id}_{ordinal}",
        occurred_at=occurred_at,
        operation="AUDIT_TEST",
        outcome="ALLOWED",
        actor=context.actor,
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        trace_id=context.trace_id,
        resource_type="COMMAND",
        resource_id=f"resource_{ordinal}",
        purpose=None,
        subject_hash=None,
        evidence_ids=(),
        error_code=None,
        details=details,
    )
