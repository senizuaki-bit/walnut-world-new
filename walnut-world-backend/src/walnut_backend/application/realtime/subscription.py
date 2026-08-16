"""World-event replay for the public realtime channel."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from yaya_agent_contracts import Failure, OperationContext, Result, Success
from yaya_agent_contracts.ports import EventStorePort

from walnut_backend.adapters.postgres.models import domain_event_data
from walnut_backend.adapters.postgres.world import PostgresWorld


class RealtimeSubscriptions:
    def __init__(self, world: PostgresWorld, event_store: EventStorePort) -> None:
        self._world = world
        self._event_store = event_store

    async def replay(
        self, stream_id: str, after_sequence: int, context: OperationContext
    ) -> Result[tuple[int, tuple[Mapping[str, Any], ...]]]:
        if not stream_id.startswith("world:"):
            return Failure(_invalid("only committed World streams are public"))
        world_id = stream_id.removeprefix("world:")
        snapshot = await self._world.get_snapshot(world_id, context)
        if isinstance(snapshot, Failure):
            return snapshot
        high_watermark = snapshot.value.last_event_sequence
        if after_sequence > high_watermark:
            return Failure(_gap("resume sequence is above the committed stream head"))
        events: list[Mapping[str, Any]] = []
        cursor = after_sequence
        while cursor < high_watermark:
            page = await self._event_store.read_stream(stream_id, cursor, 500, context)
            if isinstance(page, Failure):
                return page
            if not page.value.items:
                return Failure(_gap("durable event stream has a sequence gap"))
            for event in page.value.items:
                if event.sequence != cursor + 1:
                    return Failure(_gap("durable event stream is not contiguous"))
                events.append(domain_event_data(event))
                cursor = event.sequence
            if page.value.next_cursor is None:
                break
        if cursor != high_watermark:
            return Failure(_gap("durable event replay did not reach the stream head"))
        return Success((high_watermark, tuple(events)))


def _invalid(message: str) -> Any:
    return _error("INVALID_REQUEST", "REALTIME", message, retryable=False)


def _gap(message: str) -> Any:
    return _error("EVENT_SEQUENCE_GAP", "REALTIME", message, retryable=True)


def _error(code: str, stage: str, message: str, *, retryable: bool) -> Any:
    from yaya_agent_contracts import ContractError, ErrorCategory

    metadata = {
        "INVALID_REQUEST": (ErrorCategory.VALIDATION, "request.invalid"),
        "EVENT_SEQUENCE_GAP": (ErrorCategory.CONCURRENCY, "event.resync_required"),
    }[code]
    return ContractError(
        code=code,
        category=metadata[0],
        retryable=retryable,
        user_message_key=metadata[1],
        stage=stage,
        message=message,
    )
