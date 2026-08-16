"""Agent Session resources are atomically accepted and actor-scoped."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.integration._session_authority_support import seed_session_launch_authority
from walnut_backend.adapters.postgres.models import WorkflowJobRow
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.api.app import create_app
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings

BACKEND_ROOT = Path(__file__).resolve().parents[2]
HEADERS = {
    "Authorization": "Bearer tenant_yaya:student_session",
    "X-Request-Id": "req_agent_session_0001",
    "X-Trace-Id": "trace_agent_session_0001",
    "X-Correlation-Id": "corr_agent_session_0001",
    "X-Schema-Version": "1.0.0",
}


def test_agent_session_acceptance_is_idempotent_and_actor_scoped() -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError(
            "set WALNUT_TEST_DATABASE_URL for required PostgreSQL Agent Session coverage"
        )
    expected_versions = asyncio.run(
        seed_session_launch_authority(
            database_url,
            tenant_id="tenant_yaya",
            actor_id="student_session",
            request=payload(),
        )
    )
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=BACKEND_ROOT / "contract-release.json",
        ),
        database_url=database_url,
    )
    with TestClient(create_app(settings)) as client:
        headers = {**HEADERS, "Idempotency-Key": f"idem_agent_session_{uuid4().hex}"}
        accepted = client.post("/v1/agent-sessions", headers=headers, json=payload())
        assert accepted.status_code == 202, accepted.text
        first = accepted.json()
        assert accepted.headers["idempotency-replayed"] == "false"
        assert accepted.headers["location"] == f"/v1/commands/{first['command_id']}"

        replay_headers = {
            **headers,
            "X-Request-Id": "req_agent_session_0002",
            "X-Trace-Id": "trace_agent_session_0002",
            "X-Correlation-Id": "corr_agent_session_0002",
        }
        replay = client.post("/v1/agent-sessions", headers=replay_headers, json=payload())
        assert replay.status_code == 202, replay.text
        assert replay.headers["idempotency-replayed"] == "true"
        assert replay.json()["command_id"] == first["command_id"]
        assert replay.json()["trace_id"] == first["trace_id"]

        session_id = (
            f"session_{hashlib.sha256(first['command_id'].encode('utf-8')).hexdigest()[:24]}"
        )
        session = client.get(f"/v1/agent-sessions/{session_id}", headers=replay_headers)
        assert session.status_code == 200, session.text
        assert session.json()["session_id"] == session_id
        assert session.json()["status"] == "ACTIVE"
        assert session.json()["last_turn_sequence"] == 0
        assert session.json()["versions"] == expected_versions

        denied = client.get(
            f"/v1/agent-sessions/{session_id}",
            headers={**replay_headers, "Authorization": "Bearer tenant_yaya:student_other"},
        )
        assert denied.status_code == 404

        missing_key = client.post("/v1/agent-sessions", headers=HEADERS, json=payload())
        assert missing_key.status_code == 400
        assert missing_key.json()["error"]["code"] == "INVALID_REQUEST"

        asyncio.run(_tamper_session_job_learner(database_url, first["command_id"]))
        drifted = client.get(f"/v1/agent-sessions/{session_id}", headers=replay_headers)
        assert drifted.status_code == 500
        assert drifted.json()["error"]["code"] == "INVARIANT_VIOLATION"


def payload() -> dict[str, Any]:
    return {
        "world_id": "world_session_0001",
        "learner_id": "learner_session_0001",
        "agent_profile_id": "agent_session_0001",
        "channel": "GAME",
        "locale": "zh-CN",
        "content": {
            "unit_id": "UNIT_SESSION_001",
            "version": "1.0.0",
            "content_hash": "1" * 64,
        },
        "expected_world_revision": 0,
    }


async def _tamper_session_job_learner(database_url: str, command_id: str) -> None:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session, session.begin():
            row = await session.scalar(
                select(WorkflowJobRow).where(WorkflowJobRow.command_id == command_id)
            )
            assert row is not None
            job = copy.deepcopy(row.job_json)
            request = job["request"]
            assert isinstance(request, dict)
            request["learner_id"] = "learner_session_tampered"
            row.job_json = job
    finally:
        await sessions.kw["bind"].dispose()
