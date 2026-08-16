"""Client event batch command acceptance."""

from __future__ import annotations

import hashlib

from yaya_agent_contracts import (
    CommandCreateReceipt,
    NewCommand,
    OperationContext,
    Result,
    VersionSet,
)

from walnut_backend.adapters.postgres.client_events import PostgresClientEventStore
from walnut_backend.application.game.skill_builds import parse_strict_object


class ClientEvents:
    def __init__(self, store: PostgresClientEventStore) -> None:
        self._store = store

    async def accept(
        self, raw_body: bytes, idempotency_key: str, context: OperationContext
    ) -> Result[CommandCreateReceipt]:
        command = NewCommand(
            command_type="INGEST_CLIENT_EVENTS",
            idempotency_key=idempotency_key,
            request_sha256=hashlib.sha256(raw_body).hexdigest(),
            versions=VersionSet(api_version="1.0.0", event_version="1", policy_version="policy-1", world_rules_version="rules-1", teaching_spec_version="teaching-1"),
        )
        return await self._store.accept(command, parse_strict_object(raw_body), context)
