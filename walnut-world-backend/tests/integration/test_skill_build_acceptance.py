"""Skill Build acceptance uses command idempotency and a canonical Build resource."""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from yaya_agent_build import CPP20_SAFE_V1_FLAGS
from yaya_agent_contracts import canonical_json_sha256

from tests.integration.test_product_workspace_lifecycle import (
    _accept_session,
    _dispose,
)
from tests.integration.test_terminal_read_closure import _enable_worker_policy
from walnut_backend.adapters.postgres.models import (
    AgentProfileRow,
    BuildPolicyRow,
    LaunchAuthorityRow,
    LearnerProfileRow,
    ProductContentUnitRow,
    WorldSnapshotRow,
)
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.api.app import create_app
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings

BACKEND_ROOT = Path(__file__).resolve().parents[2]
HEADERS = {
    "Authorization": "Bearer tenant_yaya:student_builder",
    "X-Request-Id": "req_skill_build_0001",
    "X-Trace-Id": "trace_skill_build_0001",
    "X-Correlation-Id": "corr_skill_build_0001",
    "X-Schema-Version": "1.0.0",
    "Idempotency-Key": "idem_skill_build_0001",
}


def test_skill_build_acceptance_is_atomic_idempotent_and_contract_valid() -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError(
            "set WALNUT_TEST_DATABASE_URL for required PostgreSQL Skill Build coverage"
        )
    fixture = asyncio.run(_seed_build_authority(database_url))
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=BACKEND_ROOT / "contract-release.json",
        ),
        database_url=database_url,
    )
    with TestClient(create_app(settings)) as client:
        idempotency_key = f"idem_skill_build_{uuid4().hex}"
        headers = {
            **HEADERS,
            "Authorization": (
                f"Bearer {fixture['tenant_id']}:{fixture['actor_id']}"
            ),
            "Idempotency-Key": idempotency_key,
        }
        payload = valid_payload(fixture["starter"])
        accepted = client.post("/v1/skill-builds", headers=headers, json=payload)
        assert accepted.status_code == 202
        first = accepted.json()
        assert first["status"] == "ACCEPTED"
        assert accepted.headers["idempotency-replayed"] == "false"
        assert accepted.headers["location"] == f"/v1/commands/{first['command_id']}"

        replay_headers = {
            **headers,
            "X-Request-Id": "req_skill_build_0002",
            "X-Trace-Id": "trace_skill_build_0002",
            "X-Correlation-Id": "corr_skill_build_0002",
        }
        replay = client.post("/v1/skill-builds", headers=replay_headers, json=payload)
        assert replay.status_code == 202
        assert replay.headers["idempotency-replayed"] == "true"
        assert replay.json()["command_id"] == first["command_id"]
        assert replay.json()["trace_id"] == first["trace_id"]
        assert replay.headers["x-trace-id"] == replay_headers["X-Trace-Id"]

        build_id = f"build_{hashlib.sha256(first['command_id'].encode('utf-8')).hexdigest()[:24]}"
        build = client.get(f"/v1/skill-builds/{build_id}", headers=replay_headers)
        assert build.status_code == 200, build.text
        assert build.json()["build_id"] == build_id
        assert build.json()["status"] == "ACCEPTED"
        assert "source_bundle" not in build.json()

        malformed = valid_payload(fixture["starter"])
        malformed["source_bundle"]["files"][0]["content_sha256"] = "0" * 64
        rejected = client.post(
            "/v1/skill-builds",
            headers={**headers, "Idempotency-Key": f"idem_skill_build_bad_{uuid4().hex}"},
            json=malformed,
        )
        assert rejected.status_code == 400
        assert rejected.json()["error"]["code"] == "INVALID_REQUEST"

        missing_key = client.post(
            "/v1/skill-builds",
            headers={key: value for key, value in headers.items() if key != "Idempotency-Key"},
            json=valid_payload(fixture["starter"]),
        )
        assert missing_key.status_code == 400
        assert missing_key.json()["error"]["code"] == "INVALID_REQUEST"


def valid_payload(starter: dict[str, Any]) -> dict[str, Any]:
    return {
        "skill_id": starter["skill_id"],
        "display_name": "Water Skill",
        "client_draft_revision": 1,
        "source_bundle": starter["source_bundle"],
        "compiler_profile": starter["compiler_profile"],
        "test_suite_version": starter["test_suite_version"],
        "requested_capabilities": ["WORLD_READ"],
    }


async def _seed_build_authority(database_url: str) -> dict[str, Any]:
    fixture = await _accept_session(database_url, student_is_learner=True)
    try:
        await fixture.handler.execute(fixture.claim)
        await _enable_worker_policy(
            database_url,
            fixture.tenant_id,
            fixture.actor_id,
        )
        return {
            "tenant_id": fixture.tenant_id,
            "actor_id": fixture.actor_id,
            "starter": fixture.starter,
        }
    finally:
        await _dispose(fixture.sessions)


async def _seed_legacy_build_authority(database_url: str) -> None:
    now = datetime.now(UTC)
    content_hash = "1" * 64
    image_digest = "sha256:" + "b" * 64
    policy_json = {
        "schema_version": "1.0.0",
        "compiler_image": f"ghcr.io/yaya/student-cpp@{image_digest}",
        "compiler_profile": "YAYA_CPP20_SAFE_V1",
        "compiler_version": "gcc-14.2.0",
        "test_suite_version": "test-suite-1",
        "compile_flags": list(CPP20_SAFE_V1_FLAGS),
        "public_tests": [],
        "hidden_tests": [],
        "parameter_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["length"],
            "properties": {"length": {"type": "integer", "const": 8}},
        },
        "limits": {
            "compile_wall_ms": 30_000,
            "test_wall_ms": 30_000,
            "memory_bytes": 268_435_456,
            "max_processes": 32,
            "cpu_millis": 1_000,
            "tmpfs_bytes": 67_108_864,
            "max_output_bytes": 1_048_576,
            "max_artifact_bytes": 16_777_216,
        },
    }
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session, session.begin():
            session.add_all(
                [
                    ProductContentUnitRow(
                        tenant_id="tenant_yaya",
                        unit_id="UNIT_BUILD_001",
                        version="1.0.0",
                        content_hash=content_hash,
                        audiences=["LEARNER"],
                        published_at=now,
                        content_json={"content_ref": {"unit_id": "UNIT_BUILD_001"}},
                    ),
                    WorldSnapshotRow(
                        tenant_id="tenant_yaya",
                        world_id="world_build_0001",
                        actor_id="student_builder",
                        content_hash=content_hash,
                        revision=0,
                        last_event_sequence=0,
                        state_hash="2" * 64,
                        generated_at=now,
                        snapshot_json={"world_id": "world_build_0001"},
                    ),
                    LearnerProfileRow(
                        tenant_id="tenant_yaya",
                        learner_id="student_builder",
                        actor_id="student_builder",
                        content_hash=content_hash,
                        profile_sha256="3" * 64,
                        profile_json={"learner_id": "student_builder", "locale": "zh-CN"},
                        created_at=now,
                        updated_at=now,
                    ),
                    AgentProfileRow(
                        tenant_id="tenant_yaya",
                        agent_profile_id="profile_build_0001",
                        actor_id="student_builder",
                        content_hash=content_hash,
                        profile_sha256="4" * 64,
                        profile_json={"agent_profile_id": "profile_build_0001"},
                        created_at=now,
                    ),
                    BuildPolicyRow(
                        tenant_id="tenant_yaya",
                        build_policy_id="policy-build-test-1",
                        actor_id="student_builder",
                        content_hash=content_hash,
                        compiler_profile="YAYA_CPP20_SAFE_V1",
                        compiler_version="gcc-14.2.0",
                        sandbox_image_digest=image_digest,
                        test_suite_version="test-suite-1",
                        allowed_capabilities=["WORLD_READ"],
                        max_source_files=32,
                        max_source_bytes=1_048_576,
                        policy_json=policy_json,
                        policy_sha256=canonical_json_sha256(policy_json),
                        active=True,
                        created_at=now,
                    ),
                ]
            )
            await session.flush()
            session.add(
                LaunchAuthorityRow(
                    tenant_id="tenant_yaya",
                    authority_id="authority_build_0001",
                    actor_id="student_builder",
                    content_unit_id="UNIT_BUILD_001",
                    content_version="1.0.0",
                    content_hash=content_hash,
                    world_id="world_build_0001",
                    learner_id="student_builder",
                    agent_profile_id="profile_build_0001",
                    build_policy_id="policy-build-test-1",
                    channel="GAME",
                    teaching_spec_version="agent-teaching-v1",
                    authority_sha256="6" * 64,
                    active=True,
                    created_at=now,
                )
            )
    finally:
        await sessions.kw["bind"].dispose()
