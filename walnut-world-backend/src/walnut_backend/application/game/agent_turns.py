"""Agent Turn acceptance; actual LLM/Sandbox execution is worker-owned."""

from __future__ import annotations

import hashlib

from yaya_agent_contracts import (
    ActorType,
    CommandCreateReceipt,
    NewCommand,
    OperationContext,
    Result,
    VersionSet,
)

from walnut_backend.adapters.postgres.agent_turns import PostgresAgentTurnStore
from walnut_backend.application.game.skill_builds import parse_strict_object


class AgentTurns:
    def __init__(
        self,
        store: PostgresAgentTurnStore,
        *,
        skill_patch_enabled: bool = False,
    ) -> None:
        if not isinstance(skill_patch_enabled, bool):
            raise TypeError("skill_patch_enabled must be a boolean")
        self._store = store
        self._skill_patch_enabled = skill_patch_enabled

    async def accept(
        self,
        session_id: str,
        raw_body: bytes,
        idempotency_key: str,
        context: OperationContext,
    ) -> Result[CommandCreateReceipt]:
        body = parse_strict_object(raw_body)
        turn_input = body.get("input")
        if (
            isinstance(turn_input, dict)
            and turn_input.get("type") == "UI_ACTION"
            and turn_input.get("action_id") == "request_ai_patch"
            and not self._skill_patch_enabled
        ):
            raise ValueError("Skill Patch capability is disabled")
        if (
            context.actor.actor_type is not ActorType.STUDENT
            or "game:player" not in context.actor.roles
        ):
            raise ValueError("Agent Turn requires the student game:player authority")
        command = NewCommand(
            command_type="EXECUTE_AGENT_TURN",
            idempotency_key=idempotency_key,
            request_sha256=hashlib.sha256(raw_body).hexdigest(),
            versions=VersionSet(
                api_version="1.0.0",
                event_version="1",
                policy_version="policy-1",
                world_rules_version="rules-1",
                teaching_spec_version="teaching-1",
            ),
        )
        return await self._store.accept(session_id, command, body, context)
