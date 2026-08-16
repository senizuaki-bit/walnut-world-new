"""Product session workspace projection query use case."""

from __future__ import annotations

from typing import Any

from yaya_agent_contracts import OperationContext, Result

from walnut_backend.adapters.postgres.product_workspaces import PostgresProductWorkspaceStore


class ProductWorkspaces:
    def __init__(self, store: PostgresProductWorkspaceStore) -> None:
        self._store = store

    async def get(self, session_id: str, context: OperationContext) -> Result[dict[str, Any]]:
        return await self._store.get(session_id, context)
