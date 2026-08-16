"""Outbox delivery loop with lease fencing, retry and dead-letter handling."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from yaya_agent_contracts import DeliveryPort, Failure, OperationContext, OutboxMessage, OutboxPort


class OutboxWorker:
    def __init__(self, outbox: OutboxPort, delivery: DeliveryPort, *, max_attempts: int = 5) -> None:
        self._outbox = outbox
        self._delivery = delivery
        self._max_attempts = max_attempts

    async def run_once(
        self, worker_id: str, context: OperationContext, *, limit: int = 20
    ) -> tuple[OutboxMessage, ...]:
        claimed = await self._outbox.claim_ready(worker_id, limit, 60, context)
        if isinstance(claimed, Failure):
            raise RuntimeError(f"cannot claim outbox: {claimed.error.code}")
        completed: list[OutboxMessage] = []
        for message in claimed.value:
            if message.destination != "FEISHU_REPORT_DRAFT":
                raise RuntimeError(f"unknown outbox event type: {message.destination}")
            if message.lease_id is None:
                raise RuntimeError("claimed outbox message has no lease")
            delivery = await self._delivery.deliver(message.payload, message.operation_context)
            if not isinstance(delivery, Failure):
                outcome = await self._outbox.mark_sent(
                    message.message_id, message.lease_id, delivery.value, context
                )
            else:
                if message.attempt >= self._max_attempts:
                    outcome = await self._outbox.mark_dead_letter(
                        message.message_id, message.lease_id, delivery.error, datetime.now(UTC), context
                    )
                else:
                    outcome = await self._outbox.mark_retry(
                        message.message_id,
                        message.lease_id,
                        delivery.error,
                        datetime.now(UTC) + timedelta(seconds=2**message.attempt),
                        context,
                    )
            if isinstance(outcome, Failure):
                raise RuntimeError(f"outbox lease completion failed: {outcome.error.code}")
            completed.append(outcome.value)
        return tuple(completed)
