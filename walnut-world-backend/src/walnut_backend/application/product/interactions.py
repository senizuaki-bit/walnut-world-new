"""Product Agent interaction query use cases."""

from __future__ import annotations

from typing import Any

from yaya_agent_contracts import OperationContext, Result

from walnut_backend.adapters.postgres.product_interactions import (
    DecisionWrite,
    PostgresProductInteractionStore,
)


class ProductInteractions:
    def __init__(self, store: PostgresProductInteractionStore) -> None:
        self._store = store

    async def get(
        self, session_id: str, interaction_id: str, context: OperationContext
    ) -> Result[dict[str, Any]]:
        return await self._store.get(session_id, interaction_id, context)

    async def list(
        self, session_id: str, after_sequence: int, limit: int, context: OperationContext
    ) -> Result[dict[str, Any]]:
        return await self._store.list(session_id, after_sequence, limit, context)

    async def decide_patch(
        self,
        session_id: str,
        interaction_id: str,
        patch_id: str,
        request_body: dict[str, Any],
        raw_body: bytes,
        idempotency_key: str,
        context: OperationContext,
    ) -> Result[DecisionWrite]:
        return await self._store.decide_patch(
            session_id,
            interaction_id,
            patch_id,
            request_body,
            raw_body,
            idempotency_key,
            context,
        )
