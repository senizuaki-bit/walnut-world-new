"""Agent Session commands isolated from HTTP and persistence adapters."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any, cast

from yaya_agent_contracts import (
    CommandCreateReceipt,
    ContentRef,
    NewCommand,
    OperationContext,
    Result,
    VersionSet,
)

from walnut_backend.adapters.postgres.agent_sessions import PostgresAgentSessionStore
from walnut_backend.application.game.skill_builds import parse_strict_object


class AgentSessions:
    def __init__(self, store: PostgresAgentSessionStore) -> None:
        self._store = store

    async def accept(
        self, raw_body: bytes, idempotency_key: str, context: OperationContext
    ) -> Result[tuple[dict[str, Any], CommandCreateReceipt]]:
        body = parse_strict_object(raw_body)
        content = cast(dict[str, Any], body["content"])
        content_context = replace(context, content_ref=ContentRef(**content))
        command = NewCommand(
            command_type="CREATE_AGENT_SESSION",
            idempotency_key=idempotency_key,
            request_sha256=hashlib.sha256(raw_body).hexdigest(),
            versions=VersionSet(
                api_version="1.0.0",
                event_version="1",
                policy_version="policy-1",
                world_rules_version="rules-1",
                teaching_spec_version="teaching-1",
                test_suite_version=content["version"],
            ),
        )
        return await self._store.accept(command, body, content_context)

    async def get(self, session_id: str, context: OperationContext) -> Result[dict[str, Any]]:
        return await self._store.get(session_id, context)
