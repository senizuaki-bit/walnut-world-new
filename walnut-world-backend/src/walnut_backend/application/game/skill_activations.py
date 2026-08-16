"""Certified Skill activation commands isolated from HTTP."""

from __future__ import annotations

import hashlib
from typing import Any

from yaya_agent_contracts import (
    CommandCreateReceipt,
    NewCommand,
    OperationContext,
    Result,
    VersionSet,
)

from walnut_backend.adapters.postgres.skill_activations import (
    PostgresSkillActivationStore,
)
from walnut_backend.application.game.skill_builds import parse_strict_object


class SkillActivations:
    def __init__(self, store: PostgresSkillActivationStore) -> None:
        self._store = store

    async def accept(
        self,
        skill_version_id: str,
        raw_body: bytes,
        idempotency_key: str,
        context: OperationContext,
    ) -> Result[tuple[str, CommandCreateReceipt]]:
        body = parse_strict_object(raw_body)
        command = NewCommand(
            command_type="ACTIVATE_SKILL_VERSION",
            idempotency_key=idempotency_key,
            request_sha256=hashlib.sha256(raw_body).hexdigest(),
            versions=VersionSet(
                api_version="1.0.0",
                event_version="1",
                policy_version="policy-1",
                world_rules_version="rules-1",
                teaching_spec_version="teaching-1",
                skill_version=skill_version_id,
            ),
        )
        return await self._store.accept(command, skill_version_id, body, context)

    async def get(
        self, activation_id: str, context: OperationContext
    ) -> Result[dict[str, Any]]:
        return await self._store.get(activation_id, context)


__all__ = ["SkillActivations"]
