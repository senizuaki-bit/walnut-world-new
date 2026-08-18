"""The public student bootstrap reads one explicit PostgreSQL authority without defaults."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.test_terminal_read_closure import (
    _activate_and_read_skill,
    _execute_build,
    _portal_call,
    _TerminalBuild,
)
from walnut_backend.adapters.postgres.models import (
    AgentProfileRow,
    AgentSessionRow,
    BuildPolicyRow,
    CommandRow,
    CurrentSessionBindingRow,
    LaunchAuthorityRow,
    LearnerProfileRow,
    ProductContentUnitRow,
    RegistryEntryRow,
    RegistryHeadRow,
    SkillActivationRow,
    SkillArtifactRow,
    SkillBuildRow,
    SkillCertificationRow,
    WorldSnapshotRow,
)
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.adapters.postgres.session_binding_authority import (
    current_session_binding_id,
)
from walnut_backend.api.app import create_app
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings

BACKEND_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = DEFAULT_CONTRACT_PATH


@dataclass(frozen=True, slots=True)
class SeededAuthority:
    authority_id: str
    actor_id: str
    content_unit_id: str
    content_hash: str
    world_id: str
    build_policy_id: str
    learner_id: str
    agent_profile_id: str


def test_student_bootstrap_returns_persisted_zero_registry_head() -> None:
    database_url = _database_url()
    seeded = asyncio.run(_seed_authority(database_url, include_registry_head=True))
    with TestClient(create_app(_settings(database_url))) as client:
        bootstrap_response = client.get(
            "/v1/student-bootstrap", headers=_headers(seeded.actor_id, "resolved")
        )
        capability_response = client.get(
            "/product-experience/v1/capabilities",
            headers=_headers(seeded.actor_id, "capabilities"),
        )
    assert bootstrap_response.status_code == 200, bootstrap_response.text
    assert capability_response.status_code == 200, capability_response.text
    body = bootstrap_response.json()
    capability = capability_response.json()
    assert body["api_version"] == "1.1.0"
    assert body["contract_version"] == "0.4.0"
    assert body["content"] == {
        "unit_id": seeded.content_unit_id,
        "version": "1.0.0",
        "content_hash": seeded.content_hash,
    }
    assert body["request_context"]["content_ref"] == body["content"]
    assert body["session"]["current_session_id"] is None
    assert body["session"]["teaching_spec_version"] == "agent-teaching-v1"
    assert body["session"]["create_request"] == {
        "world_id": seeded.world_id,
        "learner_id": seeded.learner_id,
        "agent_profile_id": seeded.agent_profile_id,
        "channel": "GAME",
        "locale": "zh-CN",
        "content": body["content"],
        "expected_world_revision": 3,
    }
    assert body["build"]["build_policy_id"] == seeded.build_policy_id
    assert body["activation"] == {
        "scope": {
            "world_id": seeded.world_id,
            "agent_profile_id": seeded.agent_profile_id,
        },
        "registry_revision": 0,
        "active": None,
    }
    assert body["world"]["snapshot_url"] == f"/v1/worlds/{seeded.world_id}/snapshot"
    assert body["world"]["events_url"] == f"/v1/worlds/{seeded.world_id}/events"
    assert capability["request_context"]["actor"] == body["actor"]
    assert capability["request_context"]["content_ref"] == body["content"]
    assert (
        capability["request_context"]["request_id"] == capability_response.headers["X-Request-Id"]
    )
    assert capability["request_context"]["trace_id"] == capability_response.headers["X-Trace-Id"]
    assert (
        capability["request_context"]["correlation_id"]
        == capability_response.headers["X-Correlation-Id"]
    )
    assert capability["request_context"]["request_id"] != body["request_context"]["request_id"]


def test_student_bootstrap_fails_closed_when_registry_head_is_missing() -> None:
    database_url = _database_url()
    seeded = asyncio.run(_seed_authority(database_url, include_registry_head=False))
    with TestClient(create_app(_settings(database_url))) as client:
        bootstrap_response = client.get(
            "/v1/student-bootstrap", headers=_headers(seeded.actor_id, "missing_head")
        )
        capability_response = client.get(
            "/product-experience/v1/capabilities",
            headers=_headers(seeded.actor_id, "capabilities_missing_head"),
        )
    assert bootstrap_response.status_code == 500, bootstrap_response.text
    assert bootstrap_response.json()["error"]["code"] == "INVARIANT_VIOLATION"
    assert capability_response.status_code == 500, capability_response.text
    assert capability_response.json()["error"]["code"] == "INVARIANT_VIOLATION"


def test_student_bootstrap_fails_closed_when_learner_locale_is_missing() -> None:
    database_url = _database_url()
    seeded = asyncio.run(
        _seed_authority(
            database_url,
            include_registry_head=True,
            learner_locale=None,
        )
    )
    with TestClient(create_app(_settings(database_url))) as client:
        response = client.get(
            "/v1/student-bootstrap", headers=_headers(seeded.actor_id, "missing_locale")
        )
    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "INVARIANT_VIOLATION"


def test_student_bootstrap_closes_current_session_and_active_skill_tuple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = _database_url()
    with TestClient(create_app(_settings(database_url))) as client:
        terminal = _execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        _activate_and_read_skill(client, terminal)
        active = _portal_call(client, _formal_active_tuple, terminal)
        response = client.get(
            "/v1/student-bootstrap",
            headers=_headers(terminal.actor_id, "active_tuple", tenant_id=terminal.tenant_id),
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session"]["current_session_id"] == active["session_id"]
    assert body["activation"]["registry_revision"] == 1
    assert body["activation"]["active"] == active["active"]


async def _formal_active_tuple(terminal: _TerminalBuild) -> dict[str, object]:
    async with terminal.sessions() as session:
        binding = await session.scalar(
            select(CurrentSessionBindingRow).where(
                CurrentSessionBindingRow.tenant_id == terminal.tenant_id,
                CurrentSessionBindingRow.actor_id == terminal.actor_id,
            )
        )
        activation = await session.scalar(
            select(SkillActivationRow).where(
                SkillActivationRow.tenant_id == terminal.tenant_id,
                SkillActivationRow.actor_id == terminal.actor_id,
                SkillActivationRow.skill_version_id == terminal.skill_version_id,
            )
        )
    assert binding is not None
    assert activation is not None
    wire = activation.activation_json
    return {
        "session_id": binding.session_id,
        "active": {
            "activation_id": wire["activation_id"],
            "skill_id": wire["skill_id"],
            "skill_version_id": wire["skill_version_id"],
            "artifact_sha256": wire["artifact_sha256"],
            "certification_id": wire["certification_id"],
            "registry_revision": wire["registry_revision"],
            "activated_at": wire["activated_at"],
        },
    }


def _database_url() -> str:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError(
            "set WALNUT_TEST_DATABASE_URL for required PostgreSQL student bootstrap coverage"
        )
    return database_url


def _settings(database_url: str) -> Settings:
    return replace(Settings.for_test(contract_path=AGENT_ROOT), database_url=database_url)


def _headers(actor_id: str, suffix: str, *, tenant_id: str = "tenant_yaya") -> dict[str, str]:
    token = uuid4().hex
    return {
        "Authorization": f"Bearer {tenant_id}:{actor_id}",
        "X-Request-Id": f"req_student_{suffix}_{token}",
        "X-Trace-Id": f"trace_student_{suffix}_{token}",
        "X-Correlation-Id": f"corr_student_{suffix}_{token}",
        "X-Schema-Version": "1.0.0",
    }


async def _seed_authority(
    database_url: str,
    *,
    include_registry_head: bool,
    learner_locale: str | None = "zh-CN",
) -> SeededAuthority:
    suffix = uuid4().hex[:16]
    seeded = SeededAuthority(
        authority_id=f"authority_{suffix}",
        actor_id=f"student_{suffix}",
        content_unit_id=f"UNIT_{suffix.upper()}",
        content_hash=(suffix * 4)[:64],
        world_id=f"world_{suffix}",
        build_policy_id=f"policy-{suffix}",
        learner_id=f"student_{suffix}",
        agent_profile_id=f"profile_{suffix}",
    )
    now = datetime.now(UTC)
    sessions = create_session_factory(database_url)
    try:
        await _insert_authority_rows(
            sessions,
            seeded,
            authority_id=seeded.authority_id,
            now=now,
            include_registry_head=include_registry_head,
            learner_locale=learner_locale,
        )
    finally:
        await sessions.kw["bind"].dispose()
    return seeded


async def _insert_authority_rows(
    sessions: async_sessionmaker[AsyncSession],
    seeded: SeededAuthority,
    *,
    authority_id: str,
    now: datetime,
    include_registry_head: bool,
    learner_locale: str | None,
) -> None:
    async with sessions() as session, session.begin():
        session.add_all(
            [
                ProductContentUnitRow(
                    tenant_id="tenant_yaya",
                    unit_id=seeded.content_unit_id,
                    version="1.0.0",
                    content_hash=seeded.content_hash,
                    audiences=["LEARNER"],
                    published_at=now,
                    content_json={"content_ref": {"unit_id": seeded.content_unit_id}},
                ),
                WorldSnapshotRow(
                    tenant_id="tenant_yaya",
                    world_id=seeded.world_id,
                    actor_id=seeded.actor_id,
                    content_hash=seeded.content_hash,
                    revision=3,
                    last_event_sequence=8,
                    state_hash="d" * 64,
                    generated_at=now,
                    snapshot_json={"world_id": seeded.world_id},
                ),
                LearnerProfileRow(
                    tenant_id="tenant_yaya",
                    learner_id=seeded.learner_id,
                    actor_id=seeded.actor_id,
                    content_hash=seeded.content_hash,
                    profile_sha256="e" * 64,
                    profile_json={
                        "learner_id": seeded.learner_id,
                        **({"locale": learner_locale} if learner_locale is not None else {}),
                    },
                    created_at=now,
                    updated_at=now,
                ),
                AgentProfileRow(
                    tenant_id="tenant_yaya",
                    agent_profile_id=seeded.agent_profile_id,
                    actor_id=seeded.actor_id,
                    content_hash=seeded.content_hash,
                    profile_sha256="f" * 64,
                    profile_json={"agent_profile_id": seeded.agent_profile_id},
                    created_at=now,
                ),
                BuildPolicyRow(
                    tenant_id="tenant_yaya",
                    build_policy_id=seeded.build_policy_id,
                    actor_id=seeded.actor_id,
                    content_hash=seeded.content_hash,
                    compiler_profile="cpp20-restricted-v1",
                    compiler_version="gcc-14.2.0",
                    sandbox_image_digest="sha256:" + "b" * 64,
                    test_suite_version="student-skill-v1",
                    allowed_capabilities=["WORLD_READ", "MOVE", "WATER"],
                    max_source_files=32,
                    max_source_bytes=1_048_576,
                    policy_json={
                        "image_ref": "ghcr.io/yaya/student-cpp@sha256:" + "b" * 64,
                        "compile_flags": ["-std=c++20"],
                        "public_tests": [{"test_id": "public_0001"}],
                        "hidden_tests": [{"test_id": "hidden_0001"}],
                        "limits": {"max_source_bytes": 1_048_576},
                        "runtime_abi_version": "student-skill-abi-v1",
                    },
                    policy_sha256="a" * 64,
                    active=True,
                    created_at=now,
                ),
            ]
        )
        await session.flush()
        session.add(
            LaunchAuthorityRow(
                tenant_id="tenant_yaya",
                authority_id=authority_id,
                actor_id=seeded.actor_id,
                content_unit_id=seeded.content_unit_id,
                content_version="1.0.0",
                content_hash=seeded.content_hash,
                world_id=seeded.world_id,
                learner_id=seeded.learner_id,
                agent_profile_id=seeded.agent_profile_id,
                build_policy_id=seeded.build_policy_id,
                channel="GAME",
                teaching_spec_version="agent-teaching-v1",
                authority_sha256="c" * 64,
                active=True,
                created_at=now,
            )
        )
        await session.flush()
        if include_registry_head:
            session.add(
                RegistryHeadRow(
                    tenant_id="tenant_yaya",
                    authority_id=authority_id,
                    actor_id=seeded.actor_id,
                    content_hash=seeded.content_hash,
                    world_id=seeded.world_id,
                    agent_profile_id=seeded.agent_profile_id,
                    revision=0,
                    updated_at=now,
                )
            )


async def _seed_active_tuple(database_url: str, seeded: SeededAuthority) -> dict[str, object]:
    suffix = uuid4().hex[:16]
    now = datetime.now(UTC)
    command_id = f"cmd_active_{suffix}"
    session_id = f"session_{suffix}"
    build_id = f"build_{suffix}"
    skill_id = f"skill_{suffix}"
    skill_version_id = f"skillver_{suffix}"
    certification_id = f"cert_{suffix}"
    activation_id = f"activation_{suffix}"
    artifact_sha256 = suffix * 4
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session, session.begin():
            session.add_all(
                [
                    CommandRow(
                        command_id=command_id,
                        tenant_id="tenant_yaya",
                        actor_id=seeded.actor_id,
                        command_type="CREATE_AGENT_SESSION",
                        status="SUCCEEDED",
                        revision=2,
                        terminal=True,
                        accepted_at=now,
                        updated_at=now,
                        record_json={"command_id": command_id},
                    ),
                    AgentSessionRow(
                        session_id=session_id,
                        tenant_id="tenant_yaya",
                        actor_id=seeded.actor_id,
                        command_id=command_id,
                        world_id=seeded.world_id,
                        status="ACTIVE",
                        created_at=now,
                        updated_at=now,
                        session_json={
                            "learner_id": seeded.learner_id,
                            "agent_profile_id": seeded.agent_profile_id,
                            "channel": "GAME",
                            "content": {
                                "unit_id": seeded.content_unit_id,
                                "version": "1.0.0",
                                "content_hash": seeded.content_hash,
                            },
                        },
                    ),
                    SkillBuildRow(
                        build_id=build_id,
                        tenant_id="tenant_yaya",
                        actor_id=seeded.actor_id,
                        command_id=f"cmd_build_{suffix}",
                        skill_id=skill_id,
                        status="SUCCEEDED",
                        terminal=True,
                        created_at=now,
                        updated_at=now,
                        build_json={"build_id": build_id},
                        request_json={"source_bundle": {"files": []}},
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    CurrentSessionBindingRow(
                        binding_id=current_session_binding_id(
                            "tenant_yaya",
                            seeded.authority_id,
                            session_id,
                        ),
                        tenant_id="tenant_yaya",
                        authority_id=seeded.authority_id,
                        session_id=session_id,
                        actor_id=seeded.actor_id,
                        content_hash=seeded.content_hash,
                        world_id=seeded.world_id,
                        learner_id=seeded.learner_id,
                        agent_profile_id=seeded.agent_profile_id,
                        bound_at=now,
                    ),
                    SkillArtifactRow(
                        tenant_id="tenant_yaya",
                        artifact_sha256=artifact_sha256,
                        build_id=build_id,
                        actor_id=seeded.actor_id,
                        content_hash=seeded.content_hash,
                        skill_id=skill_id,
                        source_sha256="2" * 64,
                        artifact_uri=f"artifact://sha256/{artifact_sha256}",
                        metadata_json={"build_id": build_id},
                        created_at=now,
                    ),
                ]
            )
            await session.flush()
            session.add(
                SkillCertificationRow(
                    certification_id=certification_id,
                    tenant_id="tenant_yaya",
                    build_id=build_id,
                    skill_id=skill_id,
                    skill_version_id=skill_version_id,
                    artifact_sha256=artifact_sha256,
                    actor_id=seeded.actor_id,
                    content_hash=seeded.content_hash,
                    certification_sha256="3" * 64,
                    certification_json={"certification_id": certification_id},
                    certified_at=now,
                )
            )
            head = await session.scalar(
                select(RegistryHeadRow)
                .where(
                    RegistryHeadRow.tenant_id == "tenant_yaya",
                    RegistryHeadRow.authority_id == seeded.authority_id,
                )
                .with_for_update()
            )
            assert head is not None
            head.revision = 1
            head.updated_at = now
            await session.flush()
            session.add(
                RegistryEntryRow(
                    tenant_id="tenant_yaya",
                    actor_id=seeded.actor_id,
                    content_hash=seeded.content_hash,
                    world_id=seeded.world_id,
                    agent_profile_id=seeded.agent_profile_id,
                    revision=1,
                    skill_id=skill_id,
                    skill_version_id=skill_version_id,
                    certification_id=certification_id,
                    artifact_sha256=artifact_sha256,
                    previous_revision=0,
                    entry_sha256="4" * 64,
                    entry_json={"registry_revision": 1},
                    activated_at=now,
                )
            )
            await session.flush()
            session.add(
                SkillActivationRow(
                    activation_id=activation_id,
                    tenant_id="tenant_yaya",
                    actor_id=seeded.actor_id,
                    content_hash=seeded.content_hash,
                    world_id=seeded.world_id,
                    agent_profile_id=seeded.agent_profile_id,
                    skill_id=skill_id,
                    skill_version_id=skill_version_id,
                    certification_id=certification_id,
                    artifact_sha256=artifact_sha256,
                    previous_registry_revision=0,
                    registry_revision=1,
                    activation_sha256="5" * 64,
                    activation_json={"activation_id": activation_id},
                    activated_at=now,
                )
            )
    finally:
        await sessions.kw["bind"].dispose()
    return {
        "session_id": session_id,
        "active": {
            "activation_id": activation_id,
            "skill_id": skill_id,
            "skill_version_id": skill_version_id,
            "artifact_sha256": artifact_sha256,
            "certification_id": certification_id,
            "registry_revision": 1,
            "activated_at": now.isoformat().replace("+00:00", "Z"),
        },
    }
