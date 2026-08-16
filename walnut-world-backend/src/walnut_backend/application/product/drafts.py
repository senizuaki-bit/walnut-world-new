"""Product Draft use cases keep HTTP path semantics out of persistence."""

from __future__ import annotations

from typing import Any

from yaya_agent_contracts import OperationContext, Result

from walnut_backend.adapters.postgres.product_drafts import DraftWrite, PostgresProductDraftStore
from walnut_backend.application.game.skill_builds import parse_strict_object


class ProductDrafts:
    def __init__(self, store: PostgresProductDraftStore) -> None:
        self._store = store

    async def get(
        self, session_id: str, draft_id: str, context: OperationContext
    ) -> Result[dict[str, Any]]:
        return await self._store.get(session_id, draft_id, context)

    async def upsert(
        self,
        session_id: str,
        draft_id: str,
        raw_body: bytes,
        idempotency_key: str,
        context: OperationContext,
    ) -> Result[DraftWrite]:
        return await self._store.upsert(
            session_id,
            draft_id,
            parse_strict_object(raw_body),
            raw_body,
            idempotency_key,
            context,
        )
