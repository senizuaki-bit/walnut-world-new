"""Feature-gated, GET-only HTTP surface for authoritative presentation pages."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient
from yaya_agent_contracts import ContentRef, Success

from tests.integration.test_world_presentation_commit import _context, _request, _rules, _seed
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.adapters.postgres.world import PostgresWorldUnitOfWork
from walnut_backend.api.app import create_app
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def test_world_presentation_route_is_default_closed_and_strict_when_enabled() -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError("set WALNUT_TEST_DATABASE_URL for required PostgreSQL coverage")
    asyncio.run(_exercise_route(database_url))


async def _exercise_route(database_url: str) -> None:
    sessions = create_session_factory(database_url)
    # Public GET middleware deliberately carries a transport-only zero hash.  The
    # presentation read must authorize by the authenticated actor, then retain
    # this non-placeholder PostgreSQL/Snapshot content authority.
    context = replace(
        _context("route"),
        content_ref=ContentRef("UNIT_INT2", "1.0.0", "a" * 64),
    )
    request = _request(context, mixed=False)
    headers = {
        "Authorization": f"Bearer {context.actor.tenant_id}:{context.actor.actor_id}",
        "X-Request-Id": "req_presentation_route_0001",
        "X-Trace-Id": "trace_presentation_route_0001",
        "X-Correlation-Id": "corr_presentation_route_0001",
        "X-Schema-Version": "1.0.0",
    }
    path = f"/v1/worlds/{request.command.world_id}/presentation-events"
    try:
        await _seed(sessions, request, context)
        committed = await PostgresWorldUnitOfWork(
            sessions, {"rules-1": _rules(success_score=2)}
        ).commit(request, context)
        assert isinstance(committed, Success)

        base = replace(
            Settings.for_test(
                contract_path=DEFAULT_CONTRACT_PATH,
                contract_release_path=BACKEND_ROOT / "contract-release.json",
            ),
            database_url=database_url,
        )
        assert base.world_presentation_enabled is False
        with TestClient(create_app(base)) as client:
            closed = client.get(f"{path}?after_sequence=0&limit=1", headers=headers)
            assert closed.status_code == 404

        enabled = replace(base, world_presentation_enabled=True)
        app = create_app(enabled)
        with TestClient(app) as client:
            first = client.get(f"{path}?after_sequence=0&limit=1", headers=headers)
            assert first.status_code == 200, first.text
            page = first.json()
            assert page["request_context"]["content_ref"]["content_hash"] == "a" * 64
            assert page["presentation_high_watermark"] == 2
            assert page["from_sequence"] == page["to_sequence"] == 1
            assert page["next_after_sequence"] == 1
            assert page["has_more"] is True
            assert len(page["events"]) == 1
            assert page["events"][0]["event_type"] == "world.action.harvested"

            second = client.get(f"{path}?after_sequence=1&limit=1", headers=headers)
            assert second.status_code == 200, second.text
            assert second.json()["from_sequence"] == second.json()["to_sequence"] == 2
            assert second.json()["has_more"] is False

            empty = client.get(f"{path}?after_sequence=2&limit=100", headers=headers)
            assert empty.status_code == 200, empty.text
            assert empty.json()["events"] == []
            assert empty.json()["from_sequence"] == 2
            assert empty.json()["to_sequence"] == 2
            assert empty.json()["next_after_sequence"] == 2

            invalid_cursor = client.get(
                f"{path}?after_sequence=-1&limit=1", headers=headers
            )
            assert invalid_cursor.status_code == 400
            assert invalid_cursor.json()["error"]["code"] == "INVALID_REQUEST"
            invalid_limit = client.get(
                f"{path}?after_sequence=0&limit=0", headers=headers
            )
            assert invalid_limit.status_code == 400
            assert invalid_limit.json()["error"]["code"] == "INVALID_REQUEST"
            wrong_method = client.post(
                f"{path}?after_sequence=0&limit=1", headers=headers, json={}
            )
            assert wrong_method.status_code == 405
    finally:
        await sessions.kw["bind"].dispose()
