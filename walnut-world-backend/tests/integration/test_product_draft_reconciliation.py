"""Product Draft writes atomically combine hash CAS and scoped idempotency."""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.integration._product_workspace_support import seed_complete_product_workspace
from tests.integration._session_authority_support import seed_session_launch_authority
from walnut_backend.adapters.postgres.models import (
    ProductDraftRevisionRow,
    ProductDraftRow,
    ProductIdempotencyReceiptRow,
    ProductWorkspaceRow,
)
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.api import middleware as transport_middleware
from walnut_backend.api.app import create_app
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings

BACKEND_ROOT = Path(__file__).resolve().parents[2]
HEADERS = {
    "Authorization": "Bearer tenant_yaya:student_draft",
    "X-Request-Id": "req_product_draft_0001",
    "X-Trace-Id": "trace_product_draft_0001",
    "X-Correlation-Id": "corr_product_draft_0001",
    "X-Schema-Version": "1.0.0",
}


def test_product_draft_create_replay_update_and_stale_cas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError("set WALNUT_TEST_DATABASE_URL for Product Draft PostgreSQL coverage")
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=BACKEND_ROOT / "contract-release.json",
        ),
        database_url=database_url,
    )
    with TestClient(create_app(settings)) as client:
        logical_clock = {"now": datetime.now(UTC) + timedelta(seconds=30)}

        class _AheadMiddlewareDateTime(datetime):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> datetime:
                assert tz is UTC
                return logical_clock["now"]

        monkeypatch.setattr(transport_middleware, "datetime", _AheadMiddlewareDateTime)
        session_id = create_session(client, database_url)
        path = f"/product-experience/v1/sessions/{session_id}/skill-drafts/draft_product_0001"
        create_headers = {**HEADERS, "Idempotency-Key": f"idem_product_draft_{uuid4().hex}"}
        body = draft_payload(session_id)
        created = client.put(path, headers=create_headers, json=body)
        assert created.status_code == 201, created.text
        initial = created.json()
        assert created.headers["idempotency-replayed"] == "false"
        assert created.headers["location"] == path
        assert created.headers["x-draft-revision"] == "1"
        assert initial["request_context"]["content_ref"] == initial["content_ref"]
        initial_requested_at = _timestamp(initial["request_context"]["requested_at"])
        initial_created_at = _timestamp(initial["created_at"])
        initial_updated_at = _timestamp(initial["updated_at"])
        assert logical_clock["now"] == initial_requested_at
        assert initial_requested_at <= initial_created_at <= initial_updated_at
        workspace = client.get(
            f"/product-experience/v1/sessions/{session_id}/workspace",
            headers=HEADERS,
        )
        assert workspace.status_code == 200, workspace.text
        assert _timestamp(workspace.json()["updated_at"]) == initial_updated_at
        initial_authority = asyncio.run(
            _draft_authority_snapshot(database_url, session_id, body["draft_id"], path)
        )
        assert initial_authority["draft"] == (
            1,
            initial_created_at,
            initial_updated_at,
            initial,
        )
        assert initial_authority["revisions"] == [(1, initial_updated_at, initial)]
        assert initial_authority["receipt_times"] == [initial_updated_at]
        assert initial_authority["workspace"] == (2, initial_updated_at, workspace.json())

        logical_clock["now"] += timedelta(seconds=1)
        replay = client.put(
            path,
            headers={
                **create_headers,
                "X-Request-Id": "req_product_draft_0002",
                "X-Trace-Id": "trace_product_draft_0002",
                "X-Correlation-Id": "corr_product_draft_0002",
            },
            json=body,
        )
        assert replay.status_code == 201, replay.text
        assert replay.headers["idempotency-replayed"] == "true"
        assert replay.json() == initial
        assert (
            asyncio.run(_draft_authority_snapshot(database_url, session_id, body["draft_id"], path))
            == initial_authority
        )

        logical_clock["now"] += timedelta(microseconds=1)
        update = draft_payload(session_id)
        update["base_revision"] = initial["revision"]
        update["base_draft_sha256"] = initial["draft_sha256"]
        update["display_name"] = "更新后的浇水技能"
        updated = client.put(
            path,
            headers={**HEADERS, "Idempotency-Key": f"idem_product_draft_{uuid4().hex}"},
            json=update,
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["revision"] == 2
        assert updated.json()["draft_sha256"] != initial["draft_sha256"]
        assert _timestamp(updated.json()["created_at"]) == initial_created_at
        assert _timestamp(updated.json()["updated_at"]) == logical_clock["now"]
        updated_workspace = client.get(
            f"/product-experience/v1/sessions/{session_id}/workspace",
            headers=HEADERS,
        )
        assert updated_workspace.status_code == 200, updated_workspace.text
        assert _timestamp(updated_workspace.json()["updated_at"]) == logical_clock["now"]
        updated_authority = asyncio.run(
            _draft_authority_snapshot(database_url, session_id, body["draft_id"], path)
        )
        assert updated_authority["draft"] == (
            2,
            initial_created_at,
            logical_clock["now"],
            updated.json(),
        )
        assert updated_authority["revisions"] == [
            (1, initial_updated_at, initial),
            (2, logical_clock["now"], updated.json()),
        ]
        assert updated_authority["receipt_times"] == [
            initial_updated_at,
            logical_clock["now"],
        ]
        assert updated_authority["workspace"] == (
            3,
            logical_clock["now"],
            updated_workspace.json(),
        )

        logical_clock["now"] += timedelta(seconds=1)
        stale = client.put(
            path,
            headers={**HEADERS, "Idempotency-Key": f"idem_product_draft_{uuid4().hex}"},
            json=update,
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "CONTENT_VERSION_MISMATCH"
        assert (
            asyncio.run(_draft_authority_snapshot(database_url, session_id, body["draft_id"], path))
            == updated_authority
        )

        invalid_source = draft_payload(session_id)
        invalid_source["source_bundle"]["files"][0]["content_sha256"] = "0" * 64
        invalid = client.put(
            path,
            headers={**HEADERS, "Idempotency-Key": f"idem_product_draft_{uuid4().hex}"},
            json=invalid_source,
        )
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == "INVALID_REQUEST"

        cross_actor = client.get(
            path,
            headers={**HEADERS, "Authorization": "Bearer tenant_yaya:student_other"},
        )
        assert cross_actor.status_code == 404


def create_session(client: TestClient, database_url: str) -> str:
    request = {
        "world_id": "world_product_0001",
        "learner_id": "learner_draft_0001",
        "agent_profile_id": "agent_draft_0001",
        "channel": "GAME",
        "locale": "zh-CN",
        "content": {
            "unit_id": "UNIT_PRODUCT_001",
            "version": "1.0.0",
            "content_hash": "3" * 64,
        },
    }
    asyncio.run(
        seed_session_launch_authority(
            database_url,
            tenant_id="tenant_yaya",
            actor_id="student_draft",
            request=request,
        )
    )
    response = client.post(
        "/v1/agent-sessions",
        headers={**HEADERS, "Idempotency-Key": f"idem_product_session_{uuid4().hex}"},
        json=request,
    )
    assert response.status_code == 202, response.text
    session_id = (
        f"session_{hashlib.sha256(response.json()['command_id'].encode('utf-8')).hexdigest()[:24]}"
    )
    asyncio.run(
        seed_complete_product_workspace(
            database_url,
            tenant_id="tenant_yaya",
            actor_id="student_draft",
            session_id=session_id,
        )
    )
    return session_id


def draft_payload(session_id: str) -> dict[str, Any]:
    source = "int main() { return 0; }\n"
    return {
        "session_id": session_id,
        "draft_id": "draft_product_0001",
        "skill_id": "skill_product_0001",
        "content_ref": {
            "unit_id": "UNIT_PRODUCT_001",
            "version": "1.0.0",
            "content_hash": "3" * 64,
        },
        "base_revision": 0,
        "base_draft_sha256": None,
        "display_name": "浇水技能",
        "source_bundle": {
            "language": "CPP20",
            "entrypoint": "main.cpp",
            "files": [
                {
                    "path": "main.cpp",
                    "content": source,
                    "content_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                }
            ],
        },
        "client_saved_at": "2026-08-09T12:00:00Z",
    }


async def _draft_authority_snapshot(
    database_url: str,
    session_id: str,
    draft_id: str,
    canonical_path: str,
) -> dict[str, Any]:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session:
            draft = await session.scalar(
                select(ProductDraftRow).where(
                    ProductDraftRow.tenant_id == "tenant_yaya",
                    ProductDraftRow.actor_id == "student_draft",
                    ProductDraftRow.session_id == session_id,
                    ProductDraftRow.draft_id == draft_id,
                )
            )
            revisions = list(
                await session.scalars(
                    select(ProductDraftRevisionRow)
                    .where(
                        ProductDraftRevisionRow.tenant_id == "tenant_yaya",
                        ProductDraftRevisionRow.actor_id == "student_draft",
                        ProductDraftRevisionRow.session_id == session_id,
                        ProductDraftRevisionRow.draft_id == draft_id,
                    )
                    .order_by(ProductDraftRevisionRow.revision)
                )
            )
            receipts = list(
                await session.scalars(
                    select(ProductIdempotencyReceiptRow)
                    .where(
                        ProductIdempotencyReceiptRow.tenant_id == "tenant_yaya",
                        ProductIdempotencyReceiptRow.actor_id == "student_draft",
                        ProductIdempotencyReceiptRow.operation == "upsertProductSkillDraft",
                        ProductIdempotencyReceiptRow.canonical_path == canonical_path,
                    )
                    .order_by(ProductIdempotencyReceiptRow.receipt_id)
                )
            )
            workspace = await session.scalar(
                select(ProductWorkspaceRow).where(
                    ProductWorkspaceRow.tenant_id == "tenant_yaya",
                    ProductWorkspaceRow.actor_id == "student_draft",
                    ProductWorkspaceRow.session_id == session_id,
                )
            )
            assert draft is not None
            assert workspace is not None
            return {
                "draft": (
                    draft.revision,
                    draft.created_at.astimezone(UTC),
                    draft.updated_at.astimezone(UTC),
                    draft.draft_json,
                ),
                "revisions": [
                    (
                        revision.revision,
                        revision.created_at.astimezone(UTC),
                        revision.draft_json,
                    )
                    for revision in revisions
                ],
                "receipt_times": [receipt.created_at.astimezone(UTC) for receipt in receipts],
                "workspace": (
                    workspace.workspace_revision,
                    workspace.updated_at.astimezone(UTC),
                    workspace.workspace_json,
                ),
            }
    finally:
        await sessions.kw["bind"].dispose()


def _timestamp(value: object) -> datetime:
    assert isinstance(value, str)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    return parsed.astimezone(UTC)
