"""Explicit durable launch authority used by Session acceptance integration tests."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy import select
from yaya_agent_build import CPP20_SAFE_V1_FLAGS
from yaya_agent_contracts import canonical_json_sha256

from walnut_backend.adapters.postgres.models import (
    AgentProfileRow,
    BuildPolicyRow,
    LaunchAuthorityRow,
    LearnerProfileRow,
    ProductContentUnitRow,
    WorldSnapshotRow,
)
from walnut_backend.adapters.postgres.session import create_session_factory


async def seed_session_launch_authority(
    database_url: str,
    *,
    tenant_id: str,
    actor_id: str,
    request: Mapping[str, object],
) -> dict[str, str]:
    content = request.get("content")
    if not isinstance(content, Mapping):
        raise TypeError("Session test request must contain content")
    unit_id = _text(content, "unit_id")
    content_version = _text(content, "version")
    content_hash = _text(content, "content_hash")
    world_id = _text(request, "world_id")
    learner_id = _text(request, "learner_id")
    profile_id = _text(request, "agent_profile_id")
    channel = _text(request, "channel")
    locale = _text(request, "locale")
    suffix = canonical_json_sha256(
        {
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "content_hash": content_hash,
            "world_id": world_id,
        }
    )[:20]
    authority_id = f"authority_{suffix}"
    policy_id = f"policy_{suffix}"
    expected_versions = {
        "api_version": "1.0.0",
        "event_version": "1",
        "policy_version": policy_id,
        "world_rules_version": "rules-test-v1",
        "teaching_spec_version": "agent-teaching-v1",
        "compiler_version": "gcc-14.2.0",
        "sandbox_image_digest": "sha256:" + "b" * 64,
        "test_suite_version": "test-suite-1",
        "prompt_version": "prompt-test-v1",
        "model_version": "fake-model-v1",
    }
    now = datetime.now(UTC)
    state = {"schema_version": "1.0.0", "fixture": suffix}
    state_hash = canonical_json_sha256(state)
    request_context = {
        "request_id": f"req_authority_{suffix}",
        "correlation_id": f"corr_authority_{suffix}",
        "trace_id": f"trace_authority_{suffix}",
        "requested_at": now.isoformat(),
        "actor": {
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "actor_type": "student",
            "roles": ["game:player"],
        },
        "content_ref": {
            "unit_id": unit_id,
            "version": content_version,
            "content_hash": content_hash,
        },
        "schema_version": "1.0.0",
    }
    world_json = {
        "request_context": request_context,
        "world_id": world_id,
        "revision": 0,
        "last_event_sequence": 0,
        "state_hash": state_hash,
        "generated_at": now.isoformat(),
        "world_rules_version": "rules-test-v1",
        "state": state,
        "state_schema_version": "1.0.0",
    }
    profile_json = {
        "agent_profile_id": profile_id,
        "provider": "fake-provider",
        "model_version": "fake-model-v1",
        "prompt_version": "prompt-test-v1",
    }
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
            active = await session.scalar(
                select(LaunchAuthorityRow).where(
                    LaunchAuthorityRow.tenant_id == tenant_id,
                    LaunchAuthorityRow.actor_id == actor_id,
                    LaunchAuthorityRow.active.is_(True),
                )
            )
            if active is not None:
                if (
                    active.authority_id != authority_id
                    or active.content_unit_id != unit_id
                    or active.content_version != content_version
                    or active.content_hash != content_hash
                    or active.world_id != world_id
                    or active.learner_id != learner_id
                    or active.agent_profile_id != profile_id
                    or active.channel != channel
                ):
                    raise AssertionError("test actor already has a different active authority")
                return expected_versions
            published = await session.scalar(
                select(ProductContentUnitRow).where(
                    ProductContentUnitRow.tenant_id == tenant_id,
                    ProductContentUnitRow.unit_id == unit_id,
                    ProductContentUnitRow.version == content_version,
                    ProductContentUnitRow.content_hash == content_hash,
                )
            )
            if published is None:
                session.add(
                    ProductContentUnitRow(
                        tenant_id=tenant_id,
                        unit_id=unit_id,
                        version=content_version,
                        content_hash=content_hash,
                        audiences=["LEARNER"],
                        published_at=now,
                        content_json={"content_ref": dict(content)},
                    )
                )
            elif "LEARNER" not in published.audiences:
                raise AssertionError("existing test content is not learner-published")
            session.add_all(
                [
                    WorldSnapshotRow(
                        tenant_id=tenant_id,
                        world_id=world_id,
                        actor_id=actor_id,
                        content_hash=content_hash,
                        revision=0,
                        last_event_sequence=0,
                        state_hash=state_hash,
                        generated_at=now,
                        snapshot_json=world_json,
                    ),
                    LearnerProfileRow(
                        tenant_id=tenant_id,
                        learner_id=learner_id,
                        actor_id=actor_id,
                        content_hash=content_hash,
                        profile_sha256="3" * 64,
                        profile_json={"learner_id": learner_id, "locale": locale},
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
                    channel=channel,
                    teaching_spec_version="agent-teaching-v1",
                    authority_sha256="7" * 64,
                    active=True,
                    created_at=now,
                )
            )
    finally:
        await sessions.kw["bind"].dispose()
    return expected_versions


def _text(value: Mapping[str, object], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str):
        raise TypeError(f"{name} must be text")
    return item


__all__ = ["seed_session_launch_authority"]
