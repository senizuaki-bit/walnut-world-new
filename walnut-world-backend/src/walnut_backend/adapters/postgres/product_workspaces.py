"""Actor-scoped recoverable Product workspace projections."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import Failure, OperationContext, Result, Success

from .models import (
    AgentSessionRow,
    ProductDraftRow,
    ProductInteractionRow,
    ProductWorkspaceRow,
    WorldSnapshotRow,
)


class PostgresProductWorkspaceStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def get(self, session_id: str, context: OperationContext) -> Result[dict[str, Any]]:
        async with self._sessions() as session:
            row = await session.scalar(select(ProductWorkspaceRow).where(ProductWorkspaceRow.tenant_id == context.actor.tenant_id, ProductWorkspaceRow.actor_id == context.actor.actor_id, ProductWorkspaceRow.session_id == session_id))
            owner = (
                await session.scalar(
                    select(AgentSessionRow).where(
                        AgentSessionRow.tenant_id == row.tenant_id,
                        AgentSessionRow.actor_id == row.actor_id,
                        AgentSessionRow.session_id == row.session_id,
                    )
                )
                if row is not None
                else None
            )
            owner_content = owner.session_json.get("content") if owner is not None else None
            owner_content_hash = (
                owner_content.get("content_hash")
                if isinstance(owner_content, Mapping)
                else None
            )
            snapshot = (
                await session.scalar(
                    select(WorldSnapshotRow).where(
                        WorldSnapshotRow.tenant_id == owner.tenant_id,
                        WorldSnapshotRow.actor_id == owner.actor_id,
                        WorldSnapshotRow.world_id == owner.world_id,
                        WorldSnapshotRow.content_hash == owner_content_hash,
                    )
                )
                if owner is not None and isinstance(owner_content_hash, str)
                else None
            )
            drafts = (
                list(
                    await session.scalars(
                        select(ProductDraftRow)
                        .where(
                            ProductDraftRow.tenant_id == owner.tenant_id,
                            ProductDraftRow.actor_id == owner.actor_id,
                            ProductDraftRow.session_id == owner.session_id,
                        )
                        .order_by(ProductDraftRow.skill_id, ProductDraftRow.draft_id)
                    )
                )
                if owner is not None
                else []
            )
            interaction_high_watermark = (
                await session.scalar(
                    select(func.coalesce(func.max(ProductInteractionRow.sequence), 0)).where(
                        ProductInteractionRow.tenant_id == owner.tenant_id,
                        ProductInteractionRow.actor_id == owner.actor_id,
                        ProductInteractionRow.session_id == owner.session_id,
                    )
                )
                if owner is not None
                else None
            )
        if row is None:
            return Failure(_error("NOT_FOUND", "READ", "session workspace not found"))
        if (
            owner is None
            or snapshot is None
            or not workspace_authority_matches(
                row,
                owner,
                snapshot,
                drafts,
                int(interaction_high_watermark or 0),
            )
        ):
            return Failure(
                _error(
                    "INVARIANT_VIOLATION",
                    "READ",
                    "Product workspace durable authority drifted",
                )
            )
        return Success(row.workspace_json)

    async def record(self, workspace: Mapping[str, Any], context: OperationContext) -> Result[None]:
        """Internal projector writer; all referenced facts must already be durable."""
        try:
            session_id = _string(_mapping(workspace, "session"), "session_id")
            workspace_id = _string(workspace, "workspace_id")
            revision = workspace["workspace_revision"]
            updated_at = _time(workspace["updated_at"])
            origin = _mapping(workspace, "request_context")
            actor = _mapping(origin, "actor")
            content = _mapping(workspace, "content_ref")
            checkpoint = _mapping(workspace, "world_checkpoint")
            created_at = _time(workspace["created_at"])
            if actor.get("tenant_id") != context.actor.tenant_id or actor.get("actor_id") != context.actor.actor_id:
                raise ValueError("workspace actor differs from context")
            if dict(content) != _content_ref(context):
                raise ValueError("workspace content differs from context")
            if created_at < context.requested_at or updated_at < created_at:
                raise ValueError("workspace timestamps are inconsistent")
            if _string(checkpoint, "world_id") != _string(_mapping(workspace, "session"), "world_id"):
                raise ValueError("workspace checkpoint differs from session world")
        except (KeyError, TypeError, ValueError) as error:
            return Failure(_error("INVARIANT_VIOLATION", "PROJECT", str(error)))
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            return Failure(_error("INVARIANT_VIOLATION", "PROJECT", "workspace revision is invalid"))
        async with self._sessions() as session, session.begin():
            owner = await session.scalar(select(AgentSessionRow).where(AgentSessionRow.tenant_id == context.actor.tenant_id, AgentSessionRow.actor_id == context.actor.actor_id, AgentSessionRow.session_id == session_id).with_for_update())
            if owner is None:
                return Failure(_error("NOT_FOUND", "PROJECT", "agent session not found"))
            if dict(_mapping(workspace, "session")) != owner.session_json:
                return Failure(_error("INVARIANT_VIOLATION", "PROJECT", "workspace session is not durable"))
            if _string(_mapping(workspace, "session"), "world_id") != owner.world_id:
                return Failure(_error("INVARIANT_VIOLATION", "PROJECT", "workspace world differs from durable session"))
            if _mapping(owner.session_json, "content") != content:
                return Failure(_error("CONTENT_VERSION_MISMATCH", "PROJECT", "workspace content differs from durable session"))
            snapshot = await session.scalar(
                select(WorldSnapshotRow).where(
                    WorldSnapshotRow.world_id == owner.world_id,
                    WorldSnapshotRow.tenant_id == context.actor.tenant_id,
                    WorldSnapshotRow.actor_id == context.actor.actor_id,
                    WorldSnapshotRow.content_hash == context.content_ref.content_hash,
                )
            )
            if snapshot is None:
                return Failure(_error("NOT_FOUND", "PROJECT", "world checkpoint not found"))
            if any(
                checkpoint.get(field) != getattr(snapshot, attribute)
                for field, attribute in (
                    ("world_revision", "revision"),
                    ("last_event_sequence", "last_event_sequence"),
                    ("state_hash", "state_hash"),
                )
            ):
                return Failure(_error("INVARIANT_VIOLATION", "PROJECT", "workspace checkpoint is stale"))
            drafts = _mapping_list(workspace, "skill_draft_refs")
            for reference in drafts:
                draft = await session.scalar(
                    select(ProductDraftRow).where(
                        ProductDraftRow.tenant_id == context.actor.tenant_id,
                        ProductDraftRow.actor_id == context.actor.actor_id,
                        ProductDraftRow.session_id == session_id,
                        ProductDraftRow.draft_id == _string(reference, "draft_id"),
                    )
                )
                if draft is None or any(
                    reference.get(field) != getattr(draft, attribute)
                    for field, attribute in (("skill_id", "skill_id"), ("revision", "revision"), ("draft_sha256", "draft_sha256"))
                ):
                    return Failure(_error("INVARIANT_VIOLATION", "PROJECT", "workspace draft reference is not durable"))
            interaction_high_watermark = await session.scalar(
                select(func.coalesce(func.max(ProductInteractionRow.sequence), 0)).where(
                    ProductInteractionRow.tenant_id == context.actor.tenant_id,
                    ProductInteractionRow.actor_id == context.actor.actor_id,
                    ProductInteractionRow.session_id == session_id,
                )
            )
            if workspace.get("last_interaction_sequence") != interaction_high_watermark:
                return Failure(_error("INVARIANT_VIOLATION", "PROJECT", "workspace interaction high-watermark is stale"))
            existing = await session.scalar(select(ProductWorkspaceRow).where(ProductWorkspaceRow.tenant_id == context.actor.tenant_id, ProductWorkspaceRow.session_id == session_id).with_for_update())
            if existing is not None:
                if existing.workspace_json == dict(workspace):
                    return Success(None)
                if existing.workspace_id != workspace_id:
                    return Failure(_error("INVARIANT_VIOLATION", "PROJECT", "workspace identity cannot change"))
                if revision != existing.workspace_revision + 1:
                    return Failure(_error("CONTENT_VERSION_MISMATCH", "PROJECT", "workspace revision is not next"))
                existing.workspace_revision, existing.updated_at, existing.workspace_json = revision, updated_at, dict(workspace)
            else:
                session.add(ProductWorkspaceRow(workspace_id=workspace_id, tenant_id=context.actor.tenant_id, actor_id=context.actor.actor_id, session_id=session_id, workspace_revision=revision, updated_at=updated_at, workspace_json=dict(workspace)))
        return Success(None)


def initial_workspace_resource(
    *,
    tenant_id: str,
    session_resource: Mapping[str, Any],
    world_revision: int,
    last_event_sequence: int,
    state_hash: str,
    draft_resource: Mapping[str, Any],
    task_id: str,
    created_at: datetime,
) -> dict[str, Any]:
    """Build the first recoverable workspace from already-durable authorities."""

    session_id = _string(session_resource, "session_id")
    world_id = _string(session_resource, "world_id")
    content = dict(_mapping(session_resource, "content"))
    draft_id = _string(draft_resource, "draft_id")
    timestamp = _timestamp(created_at)
    return {
        "request_context": dict(_mapping(session_resource, "request_context")),
        "workspace_id": _identifier("workspace", tenant_id, session_id),
        "workspace_revision": 1,
        "session": dict(session_resource),
        "content_ref": content,
        "current_task": {
            "task_id": task_id,
            "status": "IN_PROGRESS",
            "started_at": timestamp,
            "completed_at": None,
        },
        "world_checkpoint": {
            "world_id": world_id,
            "world_revision": world_revision,
            "last_event_sequence": last_event_sequence,
            "state_hash": state_hash,
        },
        "skill_draft_refs": [
            {
                "draft_id": draft_id,
                "skill_id": _string(draft_resource, "skill_id"),
                "revision": draft_resource["revision"],
                "draft_sha256": _string(draft_resource, "draft_sha256"),
                "url": (
                    f"/product-experience/v1/sessions/{session_id}/"
                    f"skill-drafts/{draft_id}"
                ),
            }
        ],
        "last_interaction_sequence": 0,
        "created_at": timestamp,
        "updated_at": timestamp,
        "links": {
            "self": f"/product-experience/v1/sessions/{session_id}/workspace",
            "content_unit": (
                f"/product-experience/v1/content-units/{content['unit_id']}/"
                f"versions/{content['version']}?content_hash={content['content_hash']}"
            ),
            "agent_interactions": (
                f"/product-experience/v1/sessions/{session_id}/"
                "agent-interactions?after_sequence=0"
            ),
            "world_snapshot": f"/v1/worlds/{world_id}/snapshot",
        },
    }


async def refresh_workspace_in_session(
    session: AsyncSession,
    *,
    tenant_id: str,
    actor_id: str,
    session_id: str,
    updated_at: datetime,
) -> ProductWorkspaceRow:
    """Advance one workspace after a durable Draft, World, or Interaction change."""

    workspace = await session.scalar(
        select(ProductWorkspaceRow)
        .where(
            ProductWorkspaceRow.tenant_id == tenant_id,
            ProductWorkspaceRow.actor_id == actor_id,
            ProductWorkspaceRow.session_id == session_id,
        )
        .with_for_update()
    )
    owner = await session.scalar(
        select(AgentSessionRow).where(
            AgentSessionRow.tenant_id == tenant_id,
            AgentSessionRow.actor_id == actor_id,
            AgentSessionRow.session_id == session_id,
        )
    )
    if workspace is None or owner is None:
        raise RuntimeError("durable Session has no recoverable Product workspace")
    if updated_at.tzinfo is None or updated_at < workspace.updated_at:
        raise RuntimeError("Product workspace update timestamp regressed")
    snapshot = await session.scalar(
        select(WorldSnapshotRow).where(
            WorldSnapshotRow.tenant_id == tenant_id,
            WorldSnapshotRow.actor_id == actor_id,
            WorldSnapshotRow.world_id == owner.world_id,
            WorldSnapshotRow.content_hash == owner.session_json["content"]["content_hash"],
        )
    )
    if snapshot is None:
        raise RuntimeError("Product workspace has no authoritative World snapshot")
    drafts = list(
        await session.scalars(
            select(ProductDraftRow)
            .where(
                ProductDraftRow.tenant_id == tenant_id,
                ProductDraftRow.actor_id == actor_id,
                ProductDraftRow.session_id == session_id,
            )
            .order_by(ProductDraftRow.skill_id, ProductDraftRow.draft_id)
        )
    )
    if not drafts:
        raise RuntimeError("Product workspace has no durable Skill Draft")
    interaction_high_watermark = await session.scalar(
        select(func.coalesce(func.max(ProductInteractionRow.sequence), 0)).where(
            ProductInteractionRow.tenant_id == tenant_id,
            ProductInteractionRow.actor_id == actor_id,
            ProductInteractionRow.session_id == session_id,
        )
    )
    value = deepcopy(workspace.workspace_json)
    value["session"] = deepcopy(owner.session_json)
    value["content_ref"] = deepcopy(owner.session_json["content"])
    value["world_checkpoint"] = {
        "world_id": snapshot.world_id,
        "world_revision": snapshot.revision,
        "last_event_sequence": snapshot.last_event_sequence,
        "state_hash": snapshot.state_hash,
    }
    value["skill_draft_refs"] = [
        {
            "draft_id": draft.draft_id,
            "skill_id": draft.skill_id,
            "revision": draft.revision,
            "draft_sha256": draft.draft_sha256,
            "url": (
                f"/product-experience/v1/sessions/{session_id}/"
                f"skill-drafts/{draft.draft_id}"
            ),
        }
        for draft in drafts
    ]
    value["last_interaction_sequence"] = int(interaction_high_watermark or 0)
    value["workspace_revision"] = workspace.workspace_revision + 1
    value["updated_at"] = _timestamp(updated_at)
    workspace.workspace_revision += 1
    workspace.updated_at = updated_at
    workspace.workspace_json = value
    return workspace


def workspace_authority_matches(
    row: ProductWorkspaceRow,
    owner: AgentSessionRow,
    snapshot: WorldSnapshotRow,
    drafts: list[ProductDraftRow],
    interaction_high_watermark: int,
) -> bool:
    value = row.workspace_json
    origin = value.get("request_context")
    actor = origin.get("actor") if isinstance(origin, Mapping) else None
    origin_content = origin.get("content_ref") if isinstance(origin, Mapping) else None
    checkpoint = value.get("world_checkpoint")
    references = value.get("skill_draft_refs")
    try:
        created_at = _time(value.get("created_at"))
        updated_at = _time(value.get("updated_at"))
    except (TypeError, ValueError):
        return False
    expected_references = [
        {
            "draft_id": draft.draft_id,
            "skill_id": draft.skill_id,
            "revision": draft.revision,
            "draft_sha256": draft.draft_sha256,
            "url": (
                f"/product-experience/v1/sessions/{row.session_id}/"
                f"skill-drafts/{draft.draft_id}"
            ),
        }
        for draft in drafts
    ]
    return (
        isinstance(actor, Mapping)
        and actor.get("tenant_id") == row.tenant_id
        and actor.get("actor_id") == row.actor_id
        and isinstance(origin_content, Mapping)
        and origin == owner.session_json.get("request_context")
        and value.get("content_ref") == origin_content == owner.session_json.get("content")
        and value.get("workspace_id")
        == row.workspace_id
        == _identifier("workspace", row.tenant_id, row.session_id)
        and value.get("workspace_revision") == row.workspace_revision
        and value.get("session") == owner.session_json
        and owner.session_id == row.session_id
        and isinstance(checkpoint, Mapping)
        and checkpoint.get("world_id") == snapshot.world_id == owner.world_id
        and checkpoint.get("world_revision") == snapshot.revision
        and checkpoint.get("last_event_sequence") == snapshot.last_event_sequence
        and checkpoint.get("state_hash") == snapshot.state_hash
        and references == expected_references
        and value.get("last_interaction_sequence") == interaction_high_watermark
        and created_at <= updated_at == row.updated_at
    )


def _mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    item = value[key]
    if not isinstance(item, Mapping):
        raise TypeError(f"{key} must be an object")
    return item


def _string(value: Mapping[str, Any], key: str) -> str:
    item = value[key]
    if not isinstance(item, str):
        raise TypeError(f"{key} must be a string")
    return item


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("updated_at must be a timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _mapping_list(value: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    items = value[key]
    if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
        raise TypeError(f"{key} must be an array of objects")
    return items


def _content_ref(context: OperationContext) -> dict[str, str]:
    return {
        "unit_id": context.content_ref.unit_id,
        "version": context.content_ref.version,
        "content_hash": context.content_ref.content_hash,
    }


def _identifier(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x00".join((prefix, *parts)).encode()).hexdigest()
    return f"{prefix}_{digest[:24]}"


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("workspace timestamp must be offset-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _error(code: str, stage: str, message: str) -> Any:
    from yaya_agent_contracts import ContractError, ErrorCategory
    category, key = {"NOT_FOUND": (ErrorCategory.VALIDATION, "resource.not_found"), "CONTENT_VERSION_MISMATCH": (ErrorCategory.VALIDATION, "content.version_mismatch"), "INVARIANT_VIOLATION": (ErrorCategory.INVARIANT, "system.invariant_violation")}[code]
    return ContractError(code, category, False, key, stage, message)


__all__ = [
    "PostgresProductWorkspaceStore",
    "initial_workspace_resource",
    "refresh_workspace_in_session",
    "workspace_authority_matches",
]
