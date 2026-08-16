"""PostgreSQL coverage for backend-owned Product session workspaces."""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import ActorRef, ActorType, ContentRef, OperationContext

from tests.integration._session_authority_support import seed_session_launch_authority
from walnut_backend.adapters.postgres.models import WorldSnapshotRow
from walnut_backend.api.app import create_app
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings

AGENT_ROOT = DEFAULT_CONTRACT_PATH
HEADERS = {
    "Authorization": "Bearer tenant_yaya:student_workspace",
    "X-Request-Id": "req_product_workspace_0001",
    "X-Trace-Id": "trace_product_workspace_0001",
    "X-Correlation-Id": "corr_product_workspace_0001",
    "X-Schema-Version": "1.0.0",
}


def test_product_workspace_is_actor_scoped_and_follows_durable_facts() -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError("set WALNUT_TEST_DATABASE_URL for Product Workspace PostgreSQL coverage")
    settings = replace(Settings.for_test(contract_path=AGENT_ROOT), database_url=database_url)
    with TestClient(create_app(settings)) as client:
        session = _create_session(client, database_url)
        context = _context(session)
        workspace = _workspace(session)
        assert client.portal is not None
        app = cast(FastAPI, client.app)
        client.portal.call(
            _seed_checkpoint,
            app.state.product_workspaces._store._sessions,
            workspace,
            context,
        )
        outcome = client.portal.call(app.state.product_workspaces._store.record, workspace, context)
        assert outcome.__class__.__name__ == "Success", outcome

        session_id = str(session["session_id"])
        response = client.get(
            f"/product-experience/v1/sessions/{session_id}/workspace", headers=HEADERS
        )
        assert response.status_code == 200, response.text
        assert response.json() == workspace
        assert response.headers["etag"].startswith('"workspace:1:')

        denied = client.get(
            f"/product-experience/v1/sessions/{session_id}/workspace",
            headers={**HEADERS, "Authorization": "Bearer tenant_yaya:student_other"},
        )
        assert denied.status_code == 404

        checkpoint = workspace["world_checkpoint"]
        assert isinstance(checkpoint, dict)
        stale = {
            **workspace,
            "workspace_revision": 2,
            "world_checkpoint": {**checkpoint, "state_hash": "0" * 64},
        }
        rejected = client.portal.call(app.state.product_workspaces._store.record, stale, context)
        assert rejected.__class__.__name__ == "Failure"
        assert rejected.error.code == "INVARIANT_VIOLATION"


def _create_session(client: TestClient, database_url: str) -> dict[str, object]:
    request = {
        "world_id": "world_workspace_001",
        "learner_id": "learner_workspace_001",
        "agent_profile_id": "agent_workspace_001",
        "channel": "GAME",
        "locale": "zh-CN",
        "content": {
            "unit_id": "YAYA_FARM_001",
            "version": "1.4.0",
            "content_hash": "a" * 64,
        },
    }
    asyncio.run(
        seed_session_launch_authority(
            database_url,
            tenant_id="tenant_yaya",
            actor_id="student_workspace",
            request=request,
        )
    )
    accepted = client.post(
        "/v1/agent-sessions",
        headers={**HEADERS, "Idempotency-Key": f"idem_workspace_{uuid4().hex}"},
        json=request,
    )
    assert accepted.status_code == 202, accepted.text
    session_id = (
        f"session_{hashlib.sha256(accepted.json()['command_id'].encode('utf-8')).hexdigest()[:24]}"
    )
    response = client.get(f"/v1/agent-sessions/{session_id}", headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def _context(session: dict[str, object]) -> OperationContext:
    request = session["request_context"]
    assert isinstance(request, dict)
    content = session["content"]
    assert isinstance(content, dict)
    requested_at = datetime.fromisoformat(str(request["requested_at"]).replace("Z", "+00:00"))
    return OperationContext(
        request_id=str(request["request_id"]),
        correlation_id=str(request["correlation_id"]),
        trace_id=str(request["trace_id"]),
        requested_at=requested_at,
        actor=ActorRef("tenant_yaya", "student_workspace", ActorType.STUDENT, ("game:player",)),
        content_ref=ContentRef(
            str(content["unit_id"]),
            str(content["version"]),
            str(content["content_hash"]),
        ),
        schema_version="1.0.0",
        command_id=f"cmd_workspace_seed_{uuid4().hex}",
        causation_id=None,
    )


def _workspace(session: dict[str, object]) -> dict[str, object]:
    session_id = str(session["session_id"])
    world_id = str(session["world_id"])
    content = session["content"]
    assert isinstance(content, dict)
    request_context = session["request_context"]
    assert isinstance(request_context, dict)
    request_time = datetime.fromisoformat(
        str(request_context["requested_at"]).replace("Z", "+00:00")
    )
    session_time = datetime.fromisoformat(str(session["created_at"]).replace("Z", "+00:00"))
    workspace_time = max(request_time, session_time).isoformat().replace("+00:00", "Z")
    return {
        "request_context": session["request_context"],
        "workspace_id": _workspace_id(session_id),
        "workspace_revision": 1,
        "session": session,
        "content_ref": content,
        "current_task": {
            "task_id": "task_workspace_001",
            "status": "IN_PROGRESS",
            "started_at": workspace_time,
            "completed_at": None,
        },
        "world_checkpoint": {
            "world_id": world_id,
            "world_revision": 0,
            "last_event_sequence": 0,
            "state_hash": "f" * 64,
        },
        "skill_draft_refs": [],
        "last_interaction_sequence": 0,
        "created_at": workspace_time,
        "updated_at": workspace_time,
        "links": {
            "self": f"/product-experience/v1/sessions/{session_id}/workspace",
            "content_unit": f"/product-experience/v1/content-units/{content['unit_id']}/versions/{content['version']}?content_hash={content['content_hash']}",
            "agent_interactions": f"/product-experience/v1/sessions/{session_id}/agent-interactions?after_sequence=0",
            "world_snapshot": f"/v1/worlds/{world_id}/snapshot",
        },
    }


def _workspace_id(session_id: str) -> str:
    framed = "\x00".join(("workspace", "tenant_yaya", session_id)).encode()
    return f"workspace_{hashlib.sha256(framed).hexdigest()[:24]}"


async def _seed_checkpoint(
    sessions: async_sessionmaker[AsyncSession],
    workspace: dict[str, object],
    context: OperationContext,
) -> None:
    checkpoint = workspace["world_checkpoint"]
    assert isinstance(checkpoint, dict)
    async with sessions() as session, session.begin():
        row = await session.scalar(
            select(WorldSnapshotRow).where(
                WorldSnapshotRow.world_id == str(checkpoint["world_id"]),
                WorldSnapshotRow.tenant_id == context.actor.tenant_id,
                WorldSnapshotRow.actor_id == context.actor.actor_id,
                WorldSnapshotRow.content_hash == context.content_ref.content_hash,
            )
        )
        assert row is not None
        row.revision = int(checkpoint["world_revision"])
        row.last_event_sequence = int(checkpoint["last_event_sequence"])
        row.state_hash = str(checkpoint["state_hash"])
        row.generated_at = datetime.now(UTC)
        snapshot = dict(row.snapshot_json)
        snapshot.update(
            {
                "revision": row.revision,
                "last_event_sequence": row.last_event_sequence,
                "state_hash": row.state_hash,
            }
        )
        row.snapshot_json = snapshot
