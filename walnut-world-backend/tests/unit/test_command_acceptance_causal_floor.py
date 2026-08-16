"""Command acceptance cannot precede its server-owned request context."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    ContentRef,
    NewCommand,
    OperationContext,
    VersionSet,
)

from walnut_backend.adapters.postgres.command_store import PostgresCommandStore
from walnut_backend.adapters.postgres.models import CommandRow


class _AcceptanceSession:
    """Small adapter seam for the two scalar writes in a new acceptance."""

    def __init__(self, database_now: datetime) -> None:
        self.database_now = database_now
        self.scalar_count = 0
        self.added: list[object] = []

    async def scalar(self, _statement: object) -> object:
        self.scalar_count += 1
        if self.scalar_count == 1:
            return self.database_now
        if self.scalar_count == 2:
            return 1  # INSERT .. RETURNING receipt_id
        raise AssertionError("new Command acceptance issued an unexpected scalar query")

    def add(self, value: object) -> None:
        self.added.append(value)


def test_command_acceptance_uses_request_time_when_database_clock_lags() -> None:
    asyncio.run(_exercise_database_clock_lag())


async def _exercise_database_clock_lag() -> None:
    database_now = datetime(2026, 8, 14, 23, 2, 35, 376651, tzinfo=UTC)
    requested_at = database_now + timedelta(microseconds=339_007)
    context = OperationContext(
        request_id="req_clock_lag_0001",
        correlation_id="corr_clock_lag_0001",
        trace_id="trace_clock_lag_0001",
        requested_at=requested_at,
        actor=ActorRef(
            tenant_id="tenant_clock_lag",
            actor_id="student_clock_lag",
            actor_type=ActorType.STUDENT,
            roles=("game:player",),
        ),
        content_ref=ContentRef(
            unit_id="CLOCK_LAG_UNIT",
            version="1.0.0",
            content_hash="a" * 64,
        ),
        command_id="cmd_clock_lag_0001",
        causation_id=None,
    )
    command = NewCommand(
        command_type="CREATE_AGENT_SESSION",
        idempotency_key="idem_clock_lag_0001",
        request_sha256="b" * 64,
        versions=VersionSet(
            api_version="1.0.0",
            event_version="1",
            policy_version="policy-clock-lag",
            world_rules_version="rules-clock-lag",
            teaching_spec_version="teaching-clock-lag",
        ),
    )
    session = _AcceptanceSession(database_now)
    store = PostgresCommandStore(cast(Any, object()))

    result = await store.accept_once_in_session(cast(Any, session), command, context)

    assert result.ok
    assert result.value.created is True
    assert result.value.command.accepted_at == requested_at
    assert result.value.command.updated_at == requested_at
    assert result.value.command.request_context.requested_at == requested_at
    assert result.value.command.request_context.actor == context.actor
    assert result.value.command.request_context.content_ref == context.content_ref
    rows = [value for value in session.added if isinstance(value, CommandRow)]
    assert len(rows) == 1
    assert rows[0].accepted_at == requested_at
    assert rows[0].updated_at == requested_at
