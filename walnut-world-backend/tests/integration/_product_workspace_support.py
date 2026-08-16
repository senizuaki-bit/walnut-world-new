"""Test-only complete Product workspace seed for focused adapter tests."""

from __future__ import annotations

import hashlib

from sqlalchemy import select

from walnut_backend.adapters.postgres.models import (
    AgentSessionRow,
    ProductDraftRow,
    ProductWorkspaceRow,
    WorldSnapshotRow,
)
from walnut_backend.adapters.postgres.product_drafts import draft_resource
from walnut_backend.adapters.postgres.product_workspaces import initial_workspace_resource
from walnut_backend.adapters.postgres.session import create_session_factory


async def seed_complete_product_workspace(
    database_url: str,
    *,
    tenant_id: str,
    actor_id: str,
    session_id: str,
) -> None:
    """Seed every durable fact required by workspace-refreshing focused tests."""

    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session, session.begin():
            owner = await session.scalar(
                select(AgentSessionRow)
                .where(
                    AgentSessionRow.tenant_id == tenant_id,
                    AgentSessionRow.actor_id == actor_id,
                    AgentSessionRow.session_id == session_id,
                )
                .with_for_update()
            )
            assert owner is not None
            existing_workspace = await session.scalar(
                select(ProductWorkspaceRow).where(
                    ProductWorkspaceRow.tenant_id == tenant_id,
                    ProductWorkspaceRow.session_id == session_id,
                )
            )
            assert existing_workspace is None

            snapshot = await session.scalar(
                select(WorldSnapshotRow).where(
                    WorldSnapshotRow.tenant_id == tenant_id,
                    WorldSnapshotRow.actor_id == actor_id,
                    WorldSnapshotRow.world_id == owner.world_id,
                    WorldSnapshotRow.content_hash == owner.session_json["content"]["content_hash"],
                )
            )
            if snapshot is None:
                state_hash = hashlib.sha256(f"fixture-world:{session_id}".encode()).hexdigest()
                snapshot = WorldSnapshotRow(
                    world_id=owner.world_id,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    content_hash=owner.session_json["content"]["content_hash"],
                    revision=0,
                    last_event_sequence=0,
                    state_hash=state_hash,
                    generated_at=owner.updated_at,
                    snapshot_json={"world_id": owner.world_id},
                )
                session.add(snapshot)

            suffix = hashlib.sha256(f"{tenant_id}\0{session_id}".encode()).hexdigest()[:24]
            draft_id = f"draft_fixture_{suffix}"
            skill_id = f"skill_fixture_{suffix}"
            source = "int main() { return 0; }\n"
            draft = draft_resource(
                {
                    "session_id": session_id,
                    "draft_id": draft_id,
                    "skill_id": skill_id,
                    "content_ref": owner.session_json["content"],
                    "display_name": "fixture starter skill",
                    "source_bundle": {
                        "language": "CPP20",
                        "entrypoint": "main.cpp",
                        "files": [
                            {
                                "path": "main.cpp",
                                "content": source,
                                "content_sha256": hashlib.sha256(source.encode()).hexdigest(),
                            }
                        ],
                    },
                },
                owner.session_json["request_context"],
                1,
                owner.updated_at,
                owner.updated_at,
                None,
            )
            session.add(
                ProductDraftRow(
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    session_id=session_id,
                    draft_id=draft_id,
                    skill_id=skill_id,
                    revision=1,
                    draft_sha256=draft["draft_sha256"],
                    created_at=owner.updated_at,
                    updated_at=owner.updated_at,
                    draft_json=draft,
                )
            )
            workspace = initial_workspace_resource(
                tenant_id=tenant_id,
                session_resource=owner.session_json,
                world_revision=snapshot.revision,
                last_event_sequence=snapshot.last_event_sequence,
                state_hash=snapshot.state_hash,
                draft_resource=draft,
                task_id=f"task_fixture_{suffix}",
                created_at=owner.updated_at,
            )
            session.add(
                ProductWorkspaceRow(
                    workspace_id=workspace["workspace_id"],
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    session_id=session_id,
                    workspace_revision=1,
                    updated_at=owner.updated_at,
                    workspace_json=workspace,
                )
            )
    finally:
        await sessions.kw["bind"].dispose()


__all__ = ["seed_complete_product_workspace"]
