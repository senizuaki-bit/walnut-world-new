"""Product ContentUnit query use case."""

from __future__ import annotations

from typing import Any

from yaya_agent_contracts import OperationContext, Result

from walnut_backend.adapters.postgres.product_content import PostgresProductContentStore


class ProductContent:
    def __init__(self, store: PostgresProductContentStore) -> None:
        self._store = store

    async def get(
        self, unit_id: str, version: str, content_hash: str, context: OperationContext
    ) -> Result[dict[str, Any]]:
        return await self._store.get(unit_id, version, content_hash, context)
