"""The additive student bootstrap is closed over one durable authority value."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from yaya_agent_contracts import ActorRef, ActorType, ContentRef, OperationContext, Success

from walnut_backend.application.game.student_bootstrap import (
    ActiveSkillAuthority,
    StudentBootstrapQueries,
    StudentLaunchAuthority,
)
from walnut_backend.bootstrap import ContractRelease, Settings

AGENT_ROOT = Path(__file__).resolve().parents[3] / "agent"
NOW = datetime(2026, 8, 12, 1, 2, 3, tzinfo=UTC)


class StaticReader:
    async def resolve(self, context: OperationContext) -> Success[StudentLaunchAuthority]:
        return Success(authority())


def test_student_bootstrap_serializes_and_validates_the_exact_active_tuple() -> None:
    result = asyncio.run(StudentBootstrapQueries(StaticReader(), clock=lambda: NOW).get(context()))
    assert isinstance(result, Success)
    payload = result.value
    assert payload["request_context"]["content_ref"] == payload["content"]
    assert payload["session"]["create_request"]["learner_id"] == "student_bootstrap_0001"
    assert payload["session"]["teaching_spec_version"] == "agent-teaching-v1"
    assert payload["session"]["create_request"] == {
        "world_id": "world_bootstrap_0001",
        "learner_id": "student_bootstrap_0001",
        "agent_profile_id": "profile_bootstrap_0001",
        "channel": "GAME",
        "locale": "zh-CN",
        "content": payload["content"],
        "expected_world_revision": 4,
    }
    assert payload["activation"]["registry_revision"] == 7
    assert payload["activation"]["active"] == {
        "activation_id": "activation_bootstrap_0001",
        "skill_id": "skill_bootstrap_0001",
        "skill_version_id": "skillver_bootstrap_0001",
        "artifact_sha256": "c" * 64,
        "certification_id": "cert_bootstrap_0001",
        "registry_revision": 7,
        "activated_at": "2026-08-12T01:02:03Z",
    }
    release = ContractRelease(Settings.for_test(contract_path=AGENT_ROOT))
    assert release.validate(
        "contracts/schemas/game/agent-session-create-request.schema.json",
        dict(payload["session"]["create_request"]),
    ) == []
    assert release.validate(
        "contracts/schemas/game/student-bootstrap-v2.schema.json", dict(payload)
    ) == []


def authority() -> StudentLaunchAuthority:
    return StudentLaunchAuthority(
        content_unit_id="UNIT_BOOTSTRAP_001",
        content_version="1.0.0",
        content_hash="a" * 64,
        world_id="world_bootstrap_0001",
        world_revision=4,
        last_event_sequence=9,
        state_hash="d" * 64,
        learner_id="student_bootstrap_0001",
        agent_profile_id="profile_bootstrap_0001",
        channel="GAME",
        locale="zh-CN",
        teaching_spec_version="agent-teaching-v1",
        current_session_id="session_bootstrap_0001",
        build_policy_id="student-cpp-v1",
        compiler_profile="cpp20-restricted-v1",
        compiler_version="gcc-14.2.0",
        sandbox_image_digest="sha256:" + "b" * 64,
        test_suite_version="student-skill-v1",
        allowed_capabilities=("WORLD_READ", "MOVE"),
        max_source_files=32,
        max_source_bytes=1_048_576,
        registry_revision=7,
        active_skill=ActiveSkillAuthority(
            activation_id="activation_bootstrap_0001",
            skill_id="skill_bootstrap_0001",
            skill_version_id="skillver_bootstrap_0001",
            artifact_sha256="c" * 64,
            certification_id="cert_bootstrap_0001",
            registry_revision=7,
            activated_at=NOW,
        ),
    )


def context() -> OperationContext:
    return OperationContext(
        request_id="req_student_bootstrap_0001",
        correlation_id="corr_student_bootstrap_0001",
        trace_id="trace_student_bootstrap_0001",
        requested_at=NOW,
        actor=ActorRef(
            "tenant_yaya",
            "student_bootstrap_0001",
            ActorType.STUDENT,
            ("game:player",),
        ),
        content_ref=ContentRef("UNIT_TRANSPORT", "1.0.0", "0" * 64),
        schema_version="1.0.0",
        command_id="cmd_student_bootstrap_0001",
        causation_id=None,
    )
