"""Activation acceptance derives HTTP authority before durable Command creation."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    ContentRef,
    RequestContext,
    WorldSnapshot,
    canonical_json_sha256,
)

from tests.integration.test_terminal_read_closure import (
    _activation_scope,
    _execute_build,
    _portal_call,
    _TerminalBuild,
)
from walnut_backend.adapters.postgres.models import (
    AgentProfileRow,
    BuildPolicyRow,
    CommandRow,
    IdempotencyReceiptRow,
    LaunchAuthorityRow,
    LearnerProfileRow,
    ProductContentUnitRow,
    RegistryHeadRow,
    SkillArtifactRow,
    SkillBuildRow,
    SkillCertificationRevocationRow,
    SkillCertificationRow,
    WorkflowJobRow,
    WorldSnapshotRow,
    world_snapshot_data,
)
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.api.app import create_app
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings

BACKEND_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class _Fixture:
    tenant_id: str
    actor_id: str
    authority_id: str
    content: dict[str, str]
    world_id: str
    agent_profile_id: str
    build_policy_id: str
    skill_id: str
    skill_version_id: str
    artifact_sha256: str
    certification_id: str
    expected_versions: dict[str, str]


def test_http_activation_derives_exact_authority_before_command_acceptance() -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError(
            "set WALNUT_TEST_DATABASE_URL for required PostgreSQL Skill Activation coverage"
        )
    suffix = uuid4().hex[:16]
    fixture = asyncio.run(_seed_activation_authority(database_url, suffix))
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=BACKEND_ROOT / "contract-release.json",
        ),
        database_url=database_url,
    )
    payload = {
        "expected_registry_revision": 0,
        "activation_scope": {
            "world_id": fixture.world_id,
            "agent_profile_id": fixture.agent_profile_id,
        },
        "reason": "certified fixture",
    }
    success_key = f"idem_activation_success_{suffix}"
    with TestClient(create_app(settings)) as client:
        accepted = client.post(
            f"/v1/skill-versions/{fixture.skill_version_id}/activations",
            headers=_headers(fixture, success_key, "accepted"),
            json=payload,
        )
        assert accepted.status_code == 202, accepted.text
        accepted_body = accepted.json()
        assert accepted.headers["idempotency-replayed"] == "false"
        assert accepted.headers["location"] == (f"/v1/commands/{accepted_body['command_id']}")
        asyncio.run(
            _assert_accepted_authority(
                database_url,
                fixture,
                success_key,
                accepted_body["command_id"],
            )
        )

        mismatch_key = f"idem_activation_mismatch_{suffix}"
        mismatch = client.post(
            f"/v1/skill-versions/{fixture.skill_version_id}/activations",
            headers=_headers(fixture, mismatch_key, "mismatch"),
            json={
                **payload,
                "activation_scope": {
                    **payload["activation_scope"],
                    "world_id": f"world_wrong_{suffix}",
                },
            },
        )
        assert mismatch.status_code == 409, mismatch.text
        assert mismatch.json()["error"]["code"] == "CONTENT_VERSION_MISMATCH"
        asyncio.run(_assert_no_new_command(database_url, fixture, mismatch_key))

        stale_key = f"idem_activation_stale_revision_{suffix}"
        asyncio.run(
            _replace_registry_head_revision(
                database_url,
                fixture,
                expected_revision=0,
                replacement_revision=1,
            )
        )
        try:
            stale = client.post(
                f"/v1/skill-versions/{fixture.skill_version_id}/activations",
                headers=_headers(fixture, stale_key, "stale_revision"),
                json=payload,
            )
            assert stale.status_code == 409, stale.text
            assert stale.json()["error"]["code"] == "CONTENT_VERSION_MISMATCH"
            asyncio.run(_assert_no_new_command(database_url, fixture, stale_key))
        finally:
            asyncio.run(
                _replace_registry_head_revision(
                    database_url,
                    fixture,
                    expected_revision=1,
                    replacement_revision=0,
                )
            )

        asyncio.run(_revoke_certification(database_url, fixture, suffix))
        revoked_key = f"idem_activation_revoked_{suffix}"
        revoked = client.post(
            f"/v1/skill-versions/{fixture.skill_version_id}/activations",
            headers=_headers(fixture, revoked_key, "revoked"),
            json=payload,
        )
        assert revoked.status_code == 422, revoked.text
        assert revoked.json()["error"]["code"] == "SKILL_NOT_CERTIFIED"
        asyncio.run(_assert_no_new_command(database_url, fixture, revoked_key))
        asyncio.run(_remove_certification_revocation(database_url, fixture))

        asyncio.run(_add_ambiguous_certification(database_url, fixture, suffix))
        ambiguous_key = f"idem_activation_ambiguous_{suffix}"
        ambiguous = client.post(
            f"/v1/skill-versions/{fixture.skill_version_id}/activations",
            headers=_headers(fixture, ambiguous_key, "ambiguous"),
            json=payload,
        )
        assert ambiguous.status_code == 500, ambiguous.text
        assert ambiguous.json()["error"]["code"] == "INVARIANT_VIOLATION"
        asyncio.run(_assert_no_new_command(database_url, fixture, ambiguous_key))
        asyncio.run(_remove_ambiguous_certification(database_url, fixture, suffix))

        replay = client.post(
            f"/v1/skill-versions/{fixture.skill_version_id}/activations",
            headers=_headers(fixture, success_key, "replayed"),
            json=payload,
        )
        assert replay.status_code == 202, replay.text
        assert replay.headers["idempotency-replayed"] == "true"
        assert replay.json()["command_id"] == accepted_body["command_id"]


def _headers(fixture: _Fixture, idempotency_key: str, attempt: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {fixture.tenant_id}:{fixture.actor_id}",
        "X-Request-Id": f"req_activation_{attempt}_0001",
        "X-Trace-Id": f"trace_activation_{attempt}_0001",
        "X-Correlation-Id": f"corr_activation_{attempt}_0001",
        "X-Schema-Version": "1.0.0",
        "Idempotency-Key": idempotency_key,
    }


async def _seed_activation_authority(
    database_url: str,
    suffix: str,
    *,
    tenant_id: str = "tenant_yaya",
) -> _Fixture:
    del suffix, tenant_id
    return await asyncio.to_thread(_seed_formal_certified_authority, database_url)


def _seed_formal_certified_authority(database_url: str) -> _Fixture:
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=BACKEND_ROOT / "contract-release.json",
        ),
        database_url=database_url,
    )
    monkeypatch = pytest.MonkeyPatch()
    try:
        with TemporaryDirectory(prefix="walnut-activation-authority-") as temporary:
            with TestClient(create_app(settings)) as client:
                terminal = _execute_build(
                    client,
                    database_url=database_url,
                    tmp_path=Path(temporary),
                    monkeypatch=monkeypatch,
                    succeed=True,
                )
                _portal_call(client, _activation_scope, terminal)
                return _portal_call(client, _fixture_from_terminal_build, terminal)
    finally:
        monkeypatch.undo()


async def _fixture_from_terminal_build(terminal: _TerminalBuild) -> _Fixture:
    async with terminal.sessions() as session:
        authority = await session.scalar(
            select(LaunchAuthorityRow).where(
                LaunchAuthorityRow.tenant_id == terminal.tenant_id,
                LaunchAuthorityRow.actor_id == terminal.actor_id,
                LaunchAuthorityRow.active.is_(True),
            )
        )
        policy = await session.scalar(
            select(BuildPolicyRow).where(
                BuildPolicyRow.tenant_id == terminal.tenant_id,
                BuildPolicyRow.actor_id == terminal.actor_id,
                BuildPolicyRow.active.is_(True),
            )
        )
        profile = await session.scalar(
            select(AgentProfileRow).where(
                AgentProfileRow.tenant_id == terminal.tenant_id,
                AgentProfileRow.actor_id == terminal.actor_id,
            )
        )
        world = await session.scalar(
            select(WorldSnapshotRow).where(
                WorldSnapshotRow.tenant_id == terminal.tenant_id,
                WorldSnapshotRow.actor_id == terminal.actor_id,
                WorldSnapshotRow.world_id == authority.world_id,
            )
        ) if authority is not None else None
    assert authority is not None
    assert policy is not None
    assert profile is not None
    assert world is not None
    assert terminal.skill_version_id is not None
    assert terminal.artifact_sha256 is not None
    assert terminal.certification_id is not None
    prompt_version = profile.profile_json.get("prompt_version")
    model_version = profile.profile_json.get("model_version")
    world_rules_version = world.snapshot_json.get("world_rules_version")
    assert isinstance(prompt_version, str)
    assert isinstance(model_version, str)
    assert isinstance(world_rules_version, str)
    content = {
        "unit_id": authority.content_unit_id,
        "version": authority.content_version,
        "content_hash": authority.content_hash,
    }
    return _Fixture(
        tenant_id=terminal.tenant_id,
        actor_id=terminal.actor_id,
        authority_id=authority.authority_id,
        content=content,
        world_id=authority.world_id,
        agent_profile_id=authority.agent_profile_id,
        build_policy_id=policy.build_policy_id,
        skill_id=terminal.skill_id,
        skill_version_id=terminal.skill_version_id,
        artifact_sha256=terminal.artifact_sha256,
        certification_id=terminal.certification_id,
        expected_versions={
            "api_version": "1.0.0",
            "event_version": "1",
            "policy_version": policy.build_policy_id,
            "world_rules_version": world_rules_version,
            "teaching_spec_version": authority.teaching_spec_version,
            "skill_version": terminal.skill_version_id,
            "artifact_sha256": terminal.artifact_sha256,
            "compiler_version": policy.compiler_version,
            "sandbox_image_digest": policy.sandbox_image_digest,
            "test_suite_version": policy.test_suite_version,
            "prompt_version": prompt_version,
            "model_version": model_version,
        },
    )


async def _seed_legacy_activation_authority(
    database_url: str,
    suffix: str,
    *,
    tenant_id: str = "tenant_yaya",
) -> _Fixture:
    actor_id = f"student_activation_{suffix}"
    unit_id = f"UNIT_ACTIVATION_{suffix.upper()}"
    content_version = "1.0.0"
    content_hash = canonical_json_sha256({"fixture": suffix, "kind": "content"})
    content = {
        "unit_id": unit_id,
        "version": content_version,
        "content_hash": content_hash,
    }
    world_id = f"world_activation_{suffix}"
    learner_id = actor_id
    profile_id = f"profile_activation_{suffix}"
    policy_id = f"policy_activation_{suffix}"
    authority_id = f"authority_activation_{suffix}"
    build_id = f"build_activation_{suffix}"
    skill_id = f"skill_activation_{suffix}"
    skill_version_id = f"skillver_activation_{suffix}"
    artifact_sha256 = canonical_json_sha256({"fixture": suffix, "kind": "artifact"})
    source_sha256 = canonical_json_sha256({"fixture": suffix, "kind": "source"})
    certification_id = f"cert_activation_{suffix}"
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session:
            now = await session.scalar(select(func.clock_timestamp()))
        assert isinstance(now, datetime)
        assert now.tzinfo is not None
    finally:
        await sessions.kw["bind"].dispose()
    actor = ActorRef(tenant_id, actor_id, ActorType.STUDENT, ("game:player",))
    content_ref = ContentRef(**content)
    request_context = RequestContext(
        request_id=f"req_authority_{suffix}",
        correlation_id=f"corr_authority_{suffix}",
        trace_id=f"trace_authority_{suffix}",
        requested_at=now,
        actor=actor,
        content_ref=content_ref,
    )
    state = {
        "clock": {"day": 1, "minute_of_day": 480, "tick": 0},
        "avatar": {
            "entity_id": f"avatar_{suffix}",
            "position": {"x": 0, "y": 0},
            "energy": 100,
        },
        "inventory": [],
        "plots": [],
        "agents": [],
    }
    state_hash = canonical_json_sha256(state)
    snapshot = WorldSnapshot(
        request_context=request_context,
        world_id=world_id,
        revision=0,
        last_event_sequence=0,
        state_hash=state_hash,
        generated_at=now,
        world_rules_version="rules-activation-v3",
        state=state,
    )
    learner_json = {
        "schema_version": "1.0.0",
        "learner_id": learner_id,
        "actor_id": actor_id,
        "content": content,
        "locale": "zh-CN",
        "revision": 0,
        "projected_through_sequence": 0,
        "model_version": "learner-projection-v1",
        "review_policy_version": "review-v1",
        "competencies": {},
        "evidence_refs": [],
        "updated_at": now.isoformat(),
    }
    profile_json = {
        "schema_version": "1.0.0",
        "agent_profile_id": profile_id,
        "actor_id": actor_id,
        "content": content,
        "role": "activation-test-tutor",
        "revision": 1,
        "provider": "fixture-provider",
        "model_version": "fixture-model-v7",
        "prompt_version": "fixture-prompt-v5",
    }
    image_digest = "sha256:" + "b" * 64
    compiler_image = f"gcc@{image_digest}"
    policy_json = {
        "schema_version": "1.0.0",
        "compiler_image": compiler_image,
        "compiler_profile": "YAYA_CPP20_SAFE_V1",
        "compiler_version": "gcc-14.2.0",
        "test_suite_version": "test-suite-activation-v2",
        "compile_flags": [],
        "public_tests": [],
        "hidden_tests": [],
        "limits": {},
    }
    policy_sha256 = canonical_json_sha256(policy_json)
    authority_json = {
        "schema_version": "1.0.0",
        "authority_id": authority_id,
        "actor_id": actor_id,
        "content": content,
        "world_id": world_id,
        "learner_id": learner_id,
        "agent_profile_id": profile_id,
        "build_policy_id": policy_id,
        "channel": "GAME",
        "teaching_spec_version": "teaching-activation-v4",
        "active": True,
    }
    artifact_metadata = {
        "schema_version": "1.0.0",
        "artifact_sha256": artifact_sha256,
        "source_sha256": source_sha256,
        "build_identity": f"identity_{suffix}",
        "size_bytes": 42,
        "compiler_profile": "YAYA_CPP20_SAFE_V1",
        "compiler_version": "gcc-14.2.0",
        "compiler_image": compiler_image,
        "test_suite_version": "test-suite-activation-v2",
        "policy_sha256": policy_sha256,
    }
    certification_json = _certification_json(
        certification_id=certification_id,
        build_id=build_id,
        skill_id=skill_id,
        skill_version_id=skill_version_id,
        artifact_sha256=artifact_sha256,
        source_sha256=source_sha256,
        actor_id=actor_id,
        content_hash=content_hash,
        policy_id=policy_id,
        policy_sha256=policy_sha256,
        issued_at=now,
    )
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session, session.begin():
            session.add_all(
                [
                    ProductContentUnitRow(
                        tenant_id=tenant_id,
                        unit_id=unit_id,
                        version=content_version,
                        content_hash=content_hash,
                        audiences=["LEARNER"],
                        published_at=now,
                        content_json={
                            "content_ref": content,
                            "status": "PUBLISHED",
                            "unit_type": "TASK",
                            "audiences": ["LEARNER"],
                            "task": {},
                            "published_at": now.isoformat(),
                            "links": {"self": f"/fixtures/{unit_id}"},
                        },
                    ),
                    WorldSnapshotRow(
                        tenant_id=tenant_id,
                        world_id=world_id,
                        actor_id=actor_id,
                        content_hash=content_hash,
                        revision=0,
                        last_event_sequence=0,
                        state_hash=state_hash,
                        generated_at=now,
                        snapshot_json=world_snapshot_data(snapshot),
                    ),
                    LearnerProfileRow(
                        tenant_id=tenant_id,
                        learner_id=learner_id,
                        actor_id=actor_id,
                        content_hash=content_hash,
                        profile_sha256=canonical_json_sha256(learner_json),
                        profile_json=learner_json,
                        created_at=now,
                        updated_at=now,
                    ),
                    AgentProfileRow(
                        tenant_id=tenant_id,
                        agent_profile_id=profile_id,
                        actor_id=actor_id,
                        content_hash=content_hash,
                        profile_sha256=canonical_json_sha256(profile_json),
                        profile_json=profile_json,
                        created_at=now,
                    ),
                    BuildPolicyRow(
                        tenant_id=tenant_id,
                        build_policy_id=policy_id,
                        actor_id=actor_id,
                        content_hash=content_hash,
                        compiler_profile="YAYA_CPP20_SAFE_V1",
                        compiler_version="gcc-14.2.0",
                        sandbox_image_digest=image_digest,
                        test_suite_version="test-suite-activation-v2",
                        allowed_capabilities=["WORLD_READ"],
                        max_source_files=32,
                        max_source_bytes=1_048_576,
                        policy_json=policy_json,
                        policy_sha256=policy_sha256,
                        active=True,
                        created_at=now,
                    ),
                ]
            )
            await session.flush()
            session.add(
                LaunchAuthorityRow(
                    tenant_id=tenant_id,
                    authority_id=authority_id,
                    actor_id=actor_id,
                    content_unit_id=unit_id,
                    content_version=content_version,
                    content_hash=content_hash,
                    world_id=world_id,
                    learner_id=learner_id,
                    agent_profile_id=profile_id,
                    build_policy_id=policy_id,
                    channel="GAME",
                    teaching_spec_version="teaching-activation-v4",
                    authority_sha256=canonical_json_sha256(authority_json),
                    active=True,
                    created_at=now,
                )
            )
            await session.flush()
            session.add_all(
                [
                    RegistryHeadRow(
                        tenant_id=tenant_id,
                        actor_id=actor_id,
                        content_hash=content_hash,
                        world_id=world_id,
                        agent_profile_id=profile_id,
                        authority_id=authority_id,
                        revision=0,
                        updated_at=now,
                    ),
                    SkillBuildRow(
                        build_id=build_id,
                        tenant_id=tenant_id,
                        actor_id=actor_id,
                        command_id=f"cmd_fixture_build_{suffix}",
                        skill_id=skill_id,
                        status="CERTIFIED",
                        terminal=True,
                        created_at=now,
                        updated_at=now,
                        build_json={"build_id": build_id, "status": "CERTIFIED"},
                        request_json={},
                    ),
                ]
            )
            await session.flush()
            session.add(
                SkillArtifactRow(
                    tenant_id=tenant_id,
                    artifact_sha256=artifact_sha256,
                    build_id=build_id,
                    actor_id=actor_id,
                    content_hash=content_hash,
                    skill_id=skill_id,
                    source_sha256=source_sha256,
                    artifact_uri=f"artifact://fixtures/{artifact_sha256}",
                    metadata_json=artifact_metadata,
                    created_at=now,
                )
            )
            await session.flush()
            session.add(
                SkillCertificationRow(
                    certification_id=certification_id,
                    tenant_id=tenant_id,
                    build_id=build_id,
                    skill_id=skill_id,
                    skill_version_id=skill_version_id,
                    artifact_sha256=artifact_sha256,
                    actor_id=actor_id,
                    content_hash=content_hash,
                    certification_sha256=canonical_json_sha256(certification_json),
                    certification_json=certification_json,
                    certified_at=now,
                )
            )
    finally:
        await sessions.kw["bind"].dispose()
    return _Fixture(
        tenant_id=tenant_id,
        actor_id=actor_id,
        authority_id=authority_id,
        content=content,
        world_id=world_id,
        agent_profile_id=profile_id,
        build_policy_id=policy_id,
        skill_id=skill_id,
        skill_version_id=skill_version_id,
        artifact_sha256=artifact_sha256,
        certification_id=certification_id,
        expected_versions={
            "api_version": "1.0.0",
            "event_version": "1",
            "policy_version": policy_id,
            "world_rules_version": "rules-activation-v3",
            "teaching_spec_version": "teaching-activation-v4",
            "skill_version": skill_version_id,
            "artifact_sha256": artifact_sha256,
            "compiler_version": "gcc-14.2.0",
            "sandbox_image_digest": image_digest,
            "test_suite_version": "test-suite-activation-v2",
            "prompt_version": "fixture-prompt-v5",
            "model_version": "fixture-model-v7",
        },
    )


def _certification_json(
    *,
    certification_id: str,
    build_id: str,
    skill_id: str,
    skill_version_id: str,
    artifact_sha256: str,
    source_sha256: str,
    actor_id: str,
    content_hash: str,
    policy_id: str,
    policy_sha256: str,
    issued_at: datetime,
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "certification_id": certification_id,
        "build_id": build_id,
        "skill_id": skill_id,
        "skill_version_id": skill_version_id,
        "artifact_sha256": artifact_sha256,
        "source_sha256": source_sha256,
        "actor_id": actor_id,
        "content_hash": content_hash,
        "build_policy_id": policy_id,
        "policy_sha256": policy_sha256,
        "capabilities": ["WORLD_READ"],
        "issued_at": issued_at.isoformat(),
    }


async def _replace_registry_head_revision(
    database_url: str,
    fixture: _Fixture,
    *,
    expected_revision: int,
    replacement_revision: int,
) -> None:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session, session.begin():
            head = await session.scalar(
                select(RegistryHeadRow)
                .where(
                    RegistryHeadRow.tenant_id == fixture.tenant_id,
                    RegistryHeadRow.actor_id == fixture.actor_id,
                    RegistryHeadRow.content_hash == fixture.content["content_hash"],
                    RegistryHeadRow.world_id == fixture.world_id,
                    RegistryHeadRow.agent_profile_id == fixture.agent_profile_id,
                    RegistryHeadRow.authority_id == fixture.authority_id,
                )
                .with_for_update()
            )
            assert head is not None
            assert head.revision == expected_revision
            head.revision = replacement_revision
    finally:
        await sessions.kw["bind"].dispose()


async def _add_ambiguous_certification(database_url: str, fixture: _Fixture, suffix: str) -> None:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session, session.begin():
            original = await session.scalar(
                select(SkillCertificationRow).where(
                    SkillCertificationRow.certification_id == fixture.certification_id
                )
            )
            assert original is not None
            second_id = f"cert_activation_second_{suffix}"
            second_json = dict(original.certification_json)
            second_json["certification_id"] = second_id
            session.add(
                SkillCertificationRow(
                    certification_id=second_id,
                    tenant_id=original.tenant_id,
                    build_id=original.build_id,
                    skill_id=original.skill_id,
                    skill_version_id=original.skill_version_id,
                    artifact_sha256=original.artifact_sha256,
                    actor_id=original.actor_id,
                    content_hash=original.content_hash,
                    certification_sha256=canonical_json_sha256(second_json),
                    certification_json=second_json,
                    certified_at=original.certified_at,
                )
            )
    finally:
        await sessions.kw["bind"].dispose()


async def _remove_ambiguous_certification(
    database_url: str, fixture: _Fixture, suffix: str
) -> None:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session, session.begin():
            certification = await session.scalar(
                select(SkillCertificationRow).where(
                    SkillCertificationRow.tenant_id == fixture.tenant_id,
                    SkillCertificationRow.certification_id
                    == f"cert_activation_second_{suffix}",
                )
            )
            assert certification is not None
            await session.delete(certification)
    finally:
        await sessions.kw["bind"].dispose()


async def _revoke_certification(database_url: str, fixture: _Fixture, suffix: str) -> None:
    sessions = create_session_factory(database_url)
    now = datetime.now(UTC)
    revocation_json = {
        "schema_version": "1.0.0",
        "revocation_id": f"revocation_activation_{suffix}",
        "certification_id": fixture.certification_id,
        "reason_code": "SECURITY_POLICY_CHANGED",
        "revoked_at": now.isoformat(),
    }
    try:
        async with sessions() as session, session.begin():
            session.add(
                SkillCertificationRevocationRow(
                    revocation_id=revocation_json["revocation_id"],
                    tenant_id=fixture.tenant_id,
                    certification_id=fixture.certification_id,
                    revocation_sha256=canonical_json_sha256(revocation_json),
                    reason_code=revocation_json["reason_code"],
                    revocation_json=revocation_json,
                    revoked_at=now,
                )
            )
    finally:
        await sessions.kw["bind"].dispose()


async def _remove_certification_revocation(database_url: str, fixture: _Fixture) -> None:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session, session.begin():
            revocation = await session.scalar(
                select(SkillCertificationRevocationRow).where(
                    SkillCertificationRevocationRow.tenant_id == fixture.tenant_id,
                    SkillCertificationRevocationRow.certification_id == fixture.certification_id,
                )
            )
            assert revocation is not None
            await session.delete(revocation)
    finally:
        await sessions.kw["bind"].dispose()


async def _assert_accepted_authority(
    database_url: str,
    fixture: _Fixture,
    idempotency_key: str,
    command_id: str,
) -> None:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session:
            receipt = await session.scalar(
                select(IdempotencyReceiptRow).where(
                    IdempotencyReceiptRow.tenant_id == fixture.tenant_id,
                    IdempotencyReceiptRow.actor_id == fixture.actor_id,
                    IdempotencyReceiptRow.operation == "ACTIVATE_SKILL_VERSION",
                    IdempotencyReceiptRow.idempotency_key == idempotency_key,
                )
            )
            command = await session.scalar(
                select(CommandRow).where(CommandRow.command_id == command_id)
            )
            job = await session.scalar(
                select(WorkflowJobRow).where(WorkflowJobRow.command_id == command_id)
            )
            assert receipt is not None
            assert command is not None
            assert job is not None
            assert receipt.command_id == command_id
            assert command.record_json["request_context"]["content_ref"] == fixture.content
            assert command.record_json["versions"] == fixture.expected_versions
            assert job.job_json["request_context"]["content_ref"] == fixture.content
            assert job.job_json["authority_id"] == fixture.authority_id
            assert job.job_json["skill"] == {
                "skill_id": fixture.skill_id,
                "skill_version_id": fixture.skill_version_id,
                "certification_id": fixture.certification_id,
                "artifact_sha256": fixture.artifact_sha256,
            }
    finally:
        await sessions.kw["bind"].dispose()


async def _assert_no_new_command(
    database_url: str, fixture: _Fixture, idempotency_key: str
) -> None:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session:
            receipt = await session.scalar(
                select(IdempotencyReceiptRow).where(
                    IdempotencyReceiptRow.tenant_id == fixture.tenant_id,
                    IdempotencyReceiptRow.actor_id == fixture.actor_id,
                    IdempotencyReceiptRow.operation == "ACTIVATE_SKILL_VERSION",
                    IdempotencyReceiptRow.idempotency_key == idempotency_key,
                )
            )
            command_count = await session.scalar(
                select(func.count(CommandRow.command_id)).where(
                    CommandRow.tenant_id == fixture.tenant_id,
                    CommandRow.actor_id == fixture.actor_id,
                    CommandRow.command_type == "ACTIVATE_SKILL_VERSION",
                )
            )
            job_count = await session.scalar(
                select(func.count(WorkflowJobRow.job_id))
                .join(CommandRow, CommandRow.command_id == WorkflowJobRow.command_id)
                .where(
                    WorkflowJobRow.tenant_id == fixture.tenant_id,
                    WorkflowJobRow.operation == "ACTIVATE_SKILL_VERSION",
                    CommandRow.tenant_id == fixture.tenant_id,
                    CommandRow.actor_id == fixture.actor_id,
                )
            )
            assert receipt is None
            assert command_count == 1
            assert job_count == 1
    finally:
        await sessions.kw["bind"].dispose()
