"""PostgreSQL contract tests for leased, replayable Outbox delivery."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import text
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    ContentRef,
    ContractError,
    DeliveryPayload,
    ErrorCategory,
    FeishuReportDraftBody,
    OperationContext,
    OutboxMessage,
)

from walnut_backend.adapters.postgres.outbox import PostgresOutbox
from walnut_backend.adapters.postgres.session import create_session_factory


def test_expired_outbox_lease_is_replayable_by_a_new_worker() -> None:
    """A crash after claim cannot strand a message or let a stale lease complete it."""
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError(
            "PostgreSQL integration prerequisite missing: set WALNUT_TEST_DATABASE_URL; "
            "tests must not silently skip durable adapter coverage."
        )
    asyncio.run(_exercise_outbox(database_url))


async def _exercise_outbox(database_url: str) -> None:
    session_factory = create_session_factory(database_url)
    outbox = PostgresOutbox(session_factory)
    run_id = uuid4().hex
    context = make_context(run_id)
    message_id = f"outbox_{run_id}"
    idempotency_key = f"idempotency-outbox-{run_id}"
    message = OutboxMessage(
        message_id=message_id,
        destination="FEISHU_REPORT_DRAFT",
        idempotency_key=idempotency_key,
        payload=DeliveryPayload(
            delivery_id=message_id,
            operation="FEISHU_REPORT_DRAFT",
            deduplication_key=idempotency_key,
            attempt=1,
            body=FeishuReportDraftBody(report_id=f"report_{run_id}"),
        ),
        created_at=datetime.now(UTC),
        operation_context=context,
    )
    try:
        enqueued = await outbox.enqueue(message, context)
        assert enqueued.ok
        claimed_once = await outbox.claim_ready("worker-first", 1, 30, context)
        assert claimed_once.ok
        first = claimed_once.value[0]
        assert first.lease_id is not None
        assert first.attempt == 1

        async with session_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE outbox_messages SET lease_expires_at = CURRENT_TIMESTAMP - "
                    "INTERVAL '1 second' WHERE message_id = :message_id"
                ),
                {"message_id": message.message_id},
            )

        reclaimed = await outbox.claim_ready("worker-after-crash", 1, 30, context)
        assert reclaimed.ok
        second = reclaimed.value[0]
        assert second.attempt == 2
        assert second.lease_id != first.lease_id

        stale_completion = await outbox.mark_dead_letter(
            first.message_id,
            first.lease_id,
            retryable_error(),
            datetime.now(UTC),
            context,
        )
        assert not stale_completion.ok
        assert stale_completion.error.code == "WORLD_REVISION_CONFLICT"

        dead_lettered = await outbox.mark_dead_letter(
            second.message_id,
            second.lease_id,
            retryable_error(),
            datetime.now(UTC),
            context,
        )
        assert dead_lettered.ok
        assert dead_lettered.value.status.value == "DEAD_LETTER"
    finally:
        await session_factory.kw["bind"].dispose()


def make_context(run_id: str) -> OperationContext:
    return OperationContext(
        request_id=f"req_{run_id}",
        correlation_id=f"corr_{run_id}",
        trace_id=f"trace_{run_id}",
        requested_at=datetime.now(UTC),
        actor=ActorRef(
            tenant_id=f"tenant_{run_id}", actor_id=f"actor_{run_id}", actor_type=ActorType.TEACHER
        ),
        content_ref=ContentRef(unit_id="UNIT_TEST", version="1.0.0", content_hash="0" * 64),
        command_id=f"cmd_{run_id}",
        causation_id=None,
    )


def retryable_error() -> ContractError:
    return ContractError(
        code="WORLD_REVISION_CONFLICT",
        category=ErrorCategory.CONCURRENCY,
        retryable=True,
        user_message_key="world.changed_retry",
        stage="OUTBOX",
    )
