"""Immutable Product ContentUnit storage and exact-version reads."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import Failure, OperationContext, Result, Success

from .models import ProductContentUnitRow


class PostgresProductContentStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def get(
        self, unit_id: str, version: str, content_hash: str, context: OperationContext
    ) -> Result[dict[str, Any]]:
        async with self._sessions() as session:
            row = await session.scalar(
                select(ProductContentUnitRow).where(
                    ProductContentUnitRow.tenant_id == context.actor.tenant_id,
                    ProductContentUnitRow.unit_id == unit_id,
                    ProductContentUnitRow.version == version,
                    ProductContentUnitRow.content_hash == content_hash,
                )
            )
        if row is None or not _audience_authorized(row.audiences, context):
            return Failure(_error("NOT_FOUND", "READ", "published content unit not found"))
        reference = row.content_json.get("content_ref")
        try:
            published_at = _time(row.content_json.get("published_at"))
        except (TypeError, ValueError):
            published_at = None
        if (
            not isinstance(reference, Mapping)
            or reference.get("unit_id") != row.unit_id
            or reference.get("version") != row.version
            or reference.get("content_hash") != row.content_hash
            or row.content_json.get("audiences") != row.audiences
            or published_at != row.published_at
        ):
            return Failure(
                _error(
                    "INVARIANT_VIOLATION",
                    "READ",
                    "published ContentUnit durable authority drifted",
                )
            )
        return Success(row.content_json)

    async def record_published(
        self, content: Mapping[str, Any], context: OperationContext
    ) -> Result[None]:
        """Internal Content Release projection writer; public routes cannot invoke it."""
        reference = content.get("content_ref")
        if not isinstance(reference, Mapping):
            return Failure(_error("INVARIANT_VIOLATION", "CONTENT_RELEASE", "content_ref is missing"))
        try:
            unit_id = _string(reference, "unit_id")
            version = _string(reference, "version")
            content_hash = _string(reference, "content_hash")
            audiences = content["audiences"]
            if not isinstance(audiences, list) or any(not isinstance(item, str) for item in audiences):
                raise TypeError("audiences is invalid")
            published_at = _time(content["published_at"])
        except (KeyError, TypeError, ValueError) as error:
            return Failure(_error("INVARIANT_VIOLATION", "CONTENT_RELEASE", str(error)))
        async with self._sessions() as session, session.begin():
            existing = await session.scalar(
                select(ProductContentUnitRow).where(
                    ProductContentUnitRow.tenant_id == context.actor.tenant_id,
                    ProductContentUnitRow.unit_id == unit_id,
                    ProductContentUnitRow.version == version,
                ).with_for_update()
            )
            if existing is not None:
                if existing.content_json == dict(content):
                    return Success(None)
                return Failure(_error("CONTENT_VERSION_MISMATCH", "CONTENT_RELEASE", "published content is immutable"))
            session.add(ProductContentUnitRow(tenant_id=context.actor.tenant_id, unit_id=unit_id, version=version, content_hash=content_hash, audiences=list(audiences), published_at=published_at, content_json=dict(content)))
        return Success(None)


def _audience_authorized(audiences: list[str], context: OperationContext) -> bool:
    if context.actor.actor_type.value == "student":
        return "LEARNER" in audiences
    return "TEACHER_PREVIEW" in audiences and bool(set(context.actor.roles) & {"teacher", "operator", "content:read"})


def _string(value: Mapping[str, Any], key: str) -> str:
    item = value[key]
    if not isinstance(item, str):
        raise TypeError(f"{key} must be a string")
    return item


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("published_at must be a timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _error(code: str, stage: str, message: str) -> Any:
    from yaya_agent_contracts import ContractError, ErrorCategory

    metadata = {"NOT_FOUND": (ErrorCategory.VALIDATION, "resource.not_found"), "CONTENT_VERSION_MISMATCH": (ErrorCategory.VALIDATION, "content.version_mismatch"), "INVARIANT_VIOLATION": (ErrorCategory.INVARIANT, "system.invariant_violation")}[code]
    return ContractError(code, metadata[0], False, metadata[1], stage, message)
