"""Agent Turns advance an owning session only once through idempotent acceptance."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from yaya_agent_build import CPP20_SAFE_V1_FLAGS
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    ContentRef,
    FrozenJsonObject,
    RequestContext,
    WorldSnapshot,
    canonical_json_sha256,
)

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
    CurrentSessionBindingRow,
    LaunchAuthorityRow,
    LearnerProfileRow,
    ProductContentUnitRow,
    RegistryEntryRow,
    RegistryHeadRow,
    SkillActivationRow,
    SkillArtifactRow,
    SkillBuildRow,
    SkillCertificationRevocationRow,
    SkillCertificationRow,
    WorldSnapshotRow,
    request_context_data,
    world_snapshot_data,
)
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.adapters.postgres.session_binding_authority import (
    current_session_binding_id,
)
from walnut_backend.api.app import create_app
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings

BACKEND_ROOT = Path(__file__).resolve().parents[2]
HEADERS = {
    "Authorization": "Bearer tenant_yaya:student_turn",
    "X-Request-Id": "req_agent_turn_0001",
    "X-Trace-Id": "trace_agent_turn_0001",
    "X-Correlation-Id": "corr_agent_turn_0001",
    "X-Schema-Version": "1.0.0",
}
CONTENT_HASH = "2" * 64
ARTIFACT_SHA256 = "9" * 64
SKILL_BINDING = {
    "skill_id": "skill_turn_0001",
    "skill_version_id": "skillver_turn_0001",
    "artifact_sha256": ARTIFACT_SHA256,
    "certification_id": "cert_turn_0001",
}


def test_agent_turn_acceptance_is_idempotent_and_advances_session_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError(
            "set WALNUT_TEST_DATABASE_URL for required PostgreSQL Agent Turn coverage"
        )
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=BACKEND_ROOT / "contract-release.json",
        ),
        database_url=database_url,
    )
    with TestClient(create_app(settings)) as client:
        terminal = _execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        _activate_and_read_skill(client, terminal)
        session_id, world_revision, last_event_sequence = _portal_call(
            client, _formal_turn_state, terminal
        )
        suffix = uuid4().hex[:16]

        turn_headers = _formal_headers(terminal, suffix, "turn")
        first_payload = _formal_turn_payload(
            terminal,
            suffix,
            world_revision=world_revision,
            last_event_sequence=last_event_sequence,
            client_turn_sequence=1,
        )
        accepted = client.post(
            f"/v1/agent-sessions/{session_id}/turns",
            headers=turn_headers,
            json=first_payload,
        )
        assert accepted.status_code == 202, accepted.text
        first = accepted.json()
        assert accepted.headers["idempotency-replayed"] == "false"

        replay = client.post(
            f"/v1/agent-sessions/{session_id}/turns",
            headers={
                **turn_headers,
                "X-Request-Id": f"req_agent_turn_replay_{suffix}",
                "X-Trace-Id": f"trace_agent_turn_replay_{suffix}",
                "X-Correlation-Id": f"corr_agent_turn_replay_{suffix}",
            },
            json=first_payload,
        )
        assert replay.status_code == 202, replay.text
        assert replay.headers["idempotency-replayed"] == "true"
        assert replay.json()["command_id"] == first["command_id"]

        read_headers = _formal_headers(terminal, suffix, "read")
        session = client.get(f"/v1/agent-sessions/{session_id}", headers=read_headers)
        assert session.status_code == 200, session.text
        assert session.json()["last_turn_sequence"] == 1

        out_of_order = _formal_turn_payload(
            terminal,
            f"{suffix}02",
            world_revision=world_revision,
            last_event_sequence=last_event_sequence,
            client_turn_sequence=3,
        )
        rejected = client.post(
            f"/v1/agent-sessions/{session_id}/turns",
            headers=_formal_headers(terminal, suffix, "out_of_order"),
            json=out_of_order,
        )
        assert rejected.status_code == 400
        assert rejected.json()["error"]["code"] == "INVALID_REQUEST"

        _portal_call(client, _formal_revoke_certification, terminal, suffix)
        revoked_turn = _formal_turn_payload(
            terminal,
            f"{suffix}03",
            world_revision=world_revision,
            last_event_sequence=last_event_sequence,
            client_turn_sequence=2,
        )
        revoked = client.post(
            f"/v1/agent-sessions/{session_id}/turns",
            headers=_formal_headers(terminal, suffix, "revoked"),
            json=revoked_turn,
        )
        assert revoked.status_code == 422, revoked.text
        assert revoked.json()["error"]["code"] == "SKILL_NOT_CERTIFIED"
        session_after_revocation = client.get(
            f"/v1/agent-sessions/{session_id}", headers=read_headers
        )
        assert session_after_revocation.status_code == 200, session_after_revocation.text
        assert session_after_revocation.json()["last_turn_sequence"] == 1
        _portal_call(client, _formal_remove_certification_revocation, terminal, suffix)

        _portal_call(client, _formal_tamper_activation, terminal)
        corrupt_authority = _formal_turn_payload(
            terminal,
            f"{suffix}04",
            world_revision=world_revision,
            last_event_sequence=last_event_sequence,
            client_turn_sequence=2,
        )
        corrupted_turn = client.post(
            f"/v1/agent-sessions/{session_id}/turns",
            headers=_formal_headers(terminal, suffix, "corrupt_activation"),
            json=corrupt_authority,
        )
        assert corrupted_turn.status_code == 500, corrupted_turn.text
        assert corrupted_turn.json()["error"]["code"] == "INVARIANT_VIOLATION"

        _portal_call(client, _formal_tamper_session_last_turn_sequence, terminal, session_id)
        corrupted = client.get(f"/v1/agent-sessions/{session_id}", headers=read_headers)
        assert corrupted.status_code == 500, corrupted.text
        assert corrupted.json()["error"]["code"] == "INVARIANT_VIOLATION"


def _formal_headers(
    terminal: _TerminalBuild, suffix: str, operation: str
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {terminal.tenant_id}:{terminal.actor_id}",
        "X-Request-Id": f"req_agent_{operation}_{suffix}",
        "X-Trace-Id": f"trace_agent_{operation}_{suffix}",
        "X-Correlation-Id": f"corr_agent_{operation}_{suffix}",
        "X-Schema-Version": "1.0.0",
        "Idempotency-Key": f"idem_agent_{operation}_{suffix}",
    }


def _formal_turn_payload(
    terminal: _TerminalBuild,
    suffix: str,
    *,
    world_revision: int,
    last_event_sequence: int,
    client_turn_sequence: int,
) -> dict[str, Any]:
    assert terminal.skill_version_id is not None
    assert terminal.artifact_sha256 is not None
    assert terminal.certification_id is not None
    return {
        "turn_id": f"turn_agent_{suffix}",
        "expected_world_revision": world_revision,
        "input": {"type": "MESSAGE", "text": "move", "locale": "zh-CN"},
        "skill_bindings": [
            {
                "skill_id": terminal.skill_id,
                "skill_version_id": terminal.skill_version_id,
                "artifact_sha256": terminal.artifact_sha256,
                "certification_id": terminal.certification_id,
            }
        ],
        "client_state": {
            "last_event_sequence": last_event_sequence,
            "client_turn_sequence": client_turn_sequence,
        },
    }


async def _formal_turn_state(terminal: _TerminalBuild) -> tuple[str, int, int]:
    async with terminal.sessions() as session:
        binding = await session.scalar(
            select(CurrentSessionBindingRow).where(
                CurrentSessionBindingRow.tenant_id == terminal.tenant_id,
                CurrentSessionBindingRow.actor_id == terminal.actor_id,
            )
        )
        world = await session.scalar(
            select(WorldSnapshotRow).where(
                WorldSnapshotRow.tenant_id == terminal.tenant_id,
                WorldSnapshotRow.actor_id == terminal.actor_id,
            )
        )
    assert binding is not None
    assert world is not None
    return binding.session_id, world.revision, world.last_event_sequence


async def _formal_revoke_certification(
    terminal: _TerminalBuild, suffix: str
) -> None:
    assert terminal.certification_id is not None
    now = datetime.now(UTC)
    wire = {
        "schema_version": "1.0.0",
        "revocation_id": f"revocation_turn_{suffix}",
        "certification_id": terminal.certification_id,
        "reason_code": "SECURITY_POLICY_CHANGED",
        "revoked_at": now.isoformat(),
    }
    async with terminal.sessions() as session, session.begin():
        session.add(
            SkillCertificationRevocationRow(
                revocation_id=wire["revocation_id"],
                tenant_id=terminal.tenant_id,
                certification_id=terminal.certification_id,
                revocation_sha256=canonical_json_sha256(wire),
                reason_code=wire["reason_code"],
                revocation_json=wire,
                revoked_at=now,
            )
        )


async def _formal_remove_certification_revocation(
    terminal: _TerminalBuild, suffix: str
) -> None:
    async with terminal.sessions() as session, session.begin():
        row = await session.scalar(
            select(SkillCertificationRevocationRow).where(
                SkillCertificationRevocationRow.tenant_id == terminal.tenant_id,
                SkillCertificationRevocationRow.revocation_id
                == f"revocation_turn_{suffix}",
            )
        )
        assert row is not None
        await session.delete(row)


async def _formal_tamper_activation(terminal: _TerminalBuild) -> None:
    async with terminal.sessions() as session, session.begin():
        row = await session.scalar(
            select(SkillActivationRow)
            .where(
                SkillActivationRow.tenant_id == terminal.tenant_id,
                SkillActivationRow.actor_id == terminal.actor_id,
                SkillActivationRow.skill_version_id == terminal.skill_version_id,
            )
            .with_for_update()
        )
        assert row is not None
        value = dict(row.activation_json)
        value["skill_version_id"] = "skillver_coordinated_tamper"
        row.activation_json = value
        row.activation_sha256 = canonical_json_sha256(value)


async def _formal_tamper_session_last_turn_sequence(
    terminal: _TerminalBuild, session_id: str
) -> None:
    async with terminal.sessions() as session, session.begin():
        row = await session.scalar(
            select(AgentSessionRow)
            .where(
                AgentSessionRow.tenant_id == terminal.tenant_id,
                AgentSessionRow.session_id == session_id,
            )
            .with_for_update()
        )
        assert row is not None
        value = dict(row.session_json)
        value["last_turn_sequence"] = 2
        row.session_json = value


def session_payload() -> dict[str, Any]:
    return {
        "world_id": "world_turn_0001",
        "learner_id": "learner_turn_0001",
        "agent_profile_id": "agent_turn_0001",
        "channel": "GAME",
        "locale": "zh-CN",
        "content": {
            "unit_id": "UNIT_TURN_001",
            "version": "1.0.0",
            "content_hash": CONTENT_HASH,
        },
    }


def turn_payload() -> dict[str, Any]:
    return {
        "turn_id": "turn_agent_0001",
        "expected_world_revision": 0,
        "input": {"type": "MESSAGE", "text": "请浇水", "locale": "zh-CN"},
        "skill_bindings": [dict(SKILL_BINDING)],
        "client_state": {"last_event_sequence": 0, "client_turn_sequence": 1},
    }


async def _seed_turn_authority(database_url: str) -> None:
    now = await _postgres_now(database_url)
    image_digest = "sha256:" + "b" * 64
    content = ContentRef("UNIT_TURN_001", "1.0.0", CONTENT_HASH)
    actor = ActorRef(
        "tenant_yaya",
        "student_turn",
        ActorType.STUDENT,
        ("game:player",),
    )
    world_state = cast(
        FrozenJsonObject,
        {
            "clock": {"day": 1, "minute_of_day": 0, "tick": 1},
            "avatar": {
                "entity_id": "avatar_turn_0001",
                "position": {"x": 1, "y": 1},
                "energy": 100,
            },
            "inventory": [],
            "plots": [],
            "agents": [],
        },
    )
    world_snapshot = WorldSnapshot(
        request_context=RequestContext(
            request_id="req_turn_authority_0001",
            correlation_id="corr_turn_authority_0001",
            trace_id="trace_turn_authority_0001",
            requested_at=now,
            actor=actor,
            content_ref=content,
        ),
        world_id="world_turn_0001",
        revision=0,
        last_event_sequence=0,
        state_hash=canonical_json_sha256(world_state),
        generated_at=now,
        world_rules_version="rules-1",
        state=world_state,
    )
    policy_json = {
        "schema_version": "1.0.0",
        "compiler_image": f"ghcr.io/yaya/student-cpp@{image_digest}",
        "compiler_profile": "YAYA_CPP20_SAFE_V1",
        "compiler_version": "gcc-14.2.0",
        "test_suite_version": "test-suite-1",
        "compile_flags": list(CPP20_SAFE_V1_FLAGS),
        "public_tests": [],
        "hidden_tests": [],
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
    agent_profile_json = {
        "agent_profile_id": "agent_turn_0001",
        "provider": "fake-provider",
        "model_version": "fake-model-v1",
        "prompt_version": "prompt-test-v1",
    }
    activation_id = "activation_turn_0001"
    activated_at = now.isoformat().replace("+00:00", "Z")
    activation_origin = request_context_data(world_snapshot.request_context)
    activation_wire = {
        "request_context": activation_origin,
        "activation_id": activation_id,
        **SKILL_BINDING,
        "activation_scope": {
            "world_id": "world_turn_0001",
            "agent_profile_id": "agent_turn_0001",
        },
        "previous_registry_revision": 0,
        "registry_revision": 1,
        "activated_at": activated_at,
    }
    entry_wire = {
        "authority_id": "authority_turn_0001",
        "activation_id": activation_id,
        "actor_id": "student_turn",
        "content_hash": CONTENT_HASH,
        "world_id": "world_turn_0001",
        "agent_profile_id": "agent_turn_0001",
        **SKILL_BINDING,
        "previous_revision": 0,
        "revision": 1,
        "activated_at": activated_at,
    }
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session, session.begin():
            session.add_all(
                [
                    ProductContentUnitRow(
                        tenant_id="tenant_yaya",
                        unit_id="UNIT_TURN_001",
                        version="1.0.0",
                        content_hash=CONTENT_HASH,
                        audiences=["LEARNER"],
                        published_at=now,
                        content_json={"content_ref": {"unit_id": "UNIT_TURN_001"}},
                    ),
                    WorldSnapshotRow(
                        tenant_id="tenant_yaya",
                        world_id="world_turn_0001",
                        actor_id="student_turn",
                        content_hash=CONTENT_HASH,
                        revision=0,
                        last_event_sequence=0,
                        state_hash=world_snapshot.state_hash,
                        generated_at=now,
                        snapshot_json=world_snapshot_data(world_snapshot),
                    ),
                    LearnerProfileRow(
                        tenant_id="tenant_yaya",
                        learner_id="learner_turn_0001",
                        actor_id="student_turn",
                        content_hash=CONTENT_HASH,
                        profile_sha256="4" * 64,
                        profile_json={"learner_id": "learner_turn_0001", "locale": "zh-CN"},
                        created_at=now,
                        updated_at=now,
                    ),
                    AgentProfileRow(
                        tenant_id="tenant_yaya",
                        agent_profile_id="agent_turn_0001",
                        actor_id="student_turn",
                        content_hash=CONTENT_HASH,
                        profile_sha256=canonical_json_sha256(agent_profile_json),
                        profile_json=agent_profile_json,
                        created_at=now,
                    ),
                    BuildPolicyRow(
                        tenant_id="tenant_yaya",
                        build_policy_id="policy-turn-test-1",
                        actor_id="student_turn",
                        content_hash=CONTENT_HASH,
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
                    SkillBuildRow(
                        build_id="build_turn_seed_0001",
                        tenant_id="tenant_yaya",
                        actor_id="student_turn",
                        command_id="cmd_turn_seed_build_0001",
                        skill_id=SKILL_BINDING["skill_id"],
                        status="SUCCEEDED",
                        terminal=True,
                        created_at=now,
                        updated_at=now,
                        build_json={"build_id": "build_turn_seed_0001"},
                        request_json={"source_bundle": {"files": []}},
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    LaunchAuthorityRow(
                        tenant_id="tenant_yaya",
                        authority_id="authority_turn_0001",
                        actor_id="student_turn",
                        content_unit_id="UNIT_TURN_001",
                        content_version="1.0.0",
                        content_hash=CONTENT_HASH,
                        world_id="world_turn_0001",
                        learner_id="learner_turn_0001",
                        agent_profile_id="agent_turn_0001",
                        build_policy_id="policy-turn-test-1",
                        channel="GAME",
                        teaching_spec_version="agent-teaching-v1",
                        authority_sha256="7" * 64,
                        active=True,
                        created_at=now,
                    ),
                    SkillArtifactRow(
                        tenant_id="tenant_yaya",
                        artifact_sha256=ARTIFACT_SHA256,
                        build_id="build_turn_seed_0001",
                        actor_id="student_turn",
                        content_hash=CONTENT_HASH,
                        skill_id=SKILL_BINDING["skill_id"],
                        source_sha256="8" * 64,
                        artifact_uri=f"artifact://sha256/{ARTIFACT_SHA256}",
                        metadata_json={"build_id": "build_turn_seed_0001"},
                        created_at=now,
                    ),
                ]
            )
            await session.flush()
            session.add(
                SkillCertificationRow(
                    certification_id=SKILL_BINDING["certification_id"],
                    tenant_id="tenant_yaya",
                    build_id="build_turn_seed_0001",
                    skill_id=SKILL_BINDING["skill_id"],
                    skill_version_id=SKILL_BINDING["skill_version_id"],
                    artifact_sha256=ARTIFACT_SHA256,
                    actor_id="student_turn",
                    content_hash=CONTENT_HASH,
                    certification_sha256="a" * 64,
                    certification_json={"certification_id": SKILL_BINDING["certification_id"]},
                    certified_at=now,
                )
            )
            await session.flush()
            session.add(
                RegistryHeadRow(
                    tenant_id="tenant_yaya",
                    authority_id="authority_turn_0001",
                    actor_id="student_turn",
                    content_hash=CONTENT_HASH,
                    world_id="world_turn_0001",
                    agent_profile_id="agent_turn_0001",
                    revision=1,
                    updated_at=now,
                )
            )
            await session.flush()
            session.add(
                RegistryEntryRow(
                    tenant_id="tenant_yaya",
                    actor_id="student_turn",
                    content_hash=CONTENT_HASH,
                    world_id="world_turn_0001",
                    agent_profile_id="agent_turn_0001",
                    revision=1,
                    skill_id=SKILL_BINDING["skill_id"],
                    skill_version_id=SKILL_BINDING["skill_version_id"],
                    certification_id=SKILL_BINDING["certification_id"],
                    artifact_sha256=ARTIFACT_SHA256,
                    previous_revision=0,
                    entry_sha256=canonical_json_sha256(entry_wire),
                    entry_json=entry_wire,
                    activated_at=now,
                )
            )
            await session.flush()
            session.add(
                SkillActivationRow(
                    activation_id=activation_id,
                    tenant_id="tenant_yaya",
                    actor_id="student_turn",
                    content_hash=CONTENT_HASH,
                    world_id="world_turn_0001",
                    agent_profile_id="agent_turn_0001",
                    skill_id=SKILL_BINDING["skill_id"],
                    skill_version_id=SKILL_BINDING["skill_version_id"],
                    certification_id=SKILL_BINDING["certification_id"],
                    artifact_sha256=ARTIFACT_SHA256,
                    previous_registry_revision=0,
                    registry_revision=1,
                    activation_sha256=canonical_json_sha256(activation_wire),
                    activation_json=activation_wire,
                    activated_at=now,
                )
            )
    finally:
        await sessions.kw["bind"].dispose()


async def _bind_current_session(database_url: str, session_id: str) -> None:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session, session.begin():
            bound_at = await session.scalar(select(func.clock_timestamp()))
            assert isinstance(bound_at, datetime) and bound_at.tzinfo is not None
            session.add(
                CurrentSessionBindingRow(
                    binding_id=current_session_binding_id(
                        "tenant_yaya",
                        "authority_turn_0001",
                        session_id,
                    ),
                    tenant_id="tenant_yaya",
                    authority_id="authority_turn_0001",
                    session_id=session_id,
                    actor_id="student_turn",
                    content_hash=CONTENT_HASH,
                    world_id="world_turn_0001",
                    learner_id="learner_turn_0001",
                    agent_profile_id="agent_turn_0001",
                    bound_at=bound_at.astimezone(UTC),
                )
            )
    finally:
        await sessions.kw["bind"].dispose()


async def _postgres_now(database_url: str) -> datetime:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session:
            value = await session.scalar(select(func.clock_timestamp()))
            assert isinstance(value, datetime) and value.tzinfo is not None
            return value.astimezone(UTC)
    finally:
        await sessions.kw["bind"].dispose()


async def _tamper_session_last_turn_sequence(database_url: str, session_id: str) -> None:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session, session.begin():
            row = await session.scalar(
                select(AgentSessionRow)
                .where(AgentSessionRow.session_id == session_id)
                .with_for_update()
            )
            assert row is not None
            value = dict(row.session_json)
            value["last_turn_sequence"] = 2
            row.session_json = value
    finally:
        await sessions.kw["bind"].dispose()


async def _revoke_turn_certification(database_url: str) -> None:
    sessions = create_session_factory(database_url)
    now = datetime.now(UTC)
    revocation_json = {
        "schema_version": "1.0.0",
        "revocation_id": "revocation_turn_0001",
        "certification_id": SKILL_BINDING["certification_id"],
        "reason_code": "SECURITY_POLICY_CHANGED",
        "revoked_at": now.isoformat(),
    }
    try:
        async with sessions() as session, session.begin():
            session.add(
                SkillCertificationRevocationRow(
                    revocation_id=revocation_json["revocation_id"],
                    tenant_id="tenant_yaya",
                    certification_id=SKILL_BINDING["certification_id"],
                    revocation_sha256=canonical_json_sha256(revocation_json),
                    reason_code=revocation_json["reason_code"],
                    revocation_json=revocation_json,
                    revoked_at=now,
                )
            )
    finally:
        await sessions.kw["bind"].dispose()


async def _remove_turn_certification_revocation(database_url: str) -> None:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session, session.begin():
            revocation = await session.scalar(
                select(SkillCertificationRevocationRow).where(
                    SkillCertificationRevocationRow.revocation_id == "revocation_turn_0001"
                )
            )
            assert revocation is not None
            await session.delete(revocation)
    finally:
        await sessions.kw["bind"].dispose()


async def _tamper_activation_json_and_hash(database_url: str) -> None:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session, session.begin():
            row = await session.scalar(
                select(SkillActivationRow)
                .where(SkillActivationRow.activation_id == "activation_turn_0001")
                .with_for_update()
            )
            assert row is not None
            value = dict(row.activation_json)
            value["skill_version_id"] = "skillver_coordinated_tamper"
            row.activation_json = value
            row.activation_sha256 = canonical_json_sha256(value)
    finally:
        await sessions.kw["bind"].dispose()
