from __future__ import annotations

import asyncio
import hashlib
import json
import stat
import sys
import tempfile
import unittest
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
CONTRACTS_ROOT = Path(__file__).resolve().parents[1] / "contracts"
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from agent_runtime_fixtures import (  # noqa: E402
    NOW,
    TASK_ID,
    WORLD_ID,
    make_operation,
    make_task,
    make_versions,
    make_world_state,
)
from postgres_test_support import postgres_test_server  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from yaya_agent_backend.application import AgentTurnApplication  # noqa: E402
from yaya_agent_backend.auth import JwtAuthenticator  # noqa: E402
from yaya_agent_backend.codec import encode, plain  # noqa: E402
from yaya_agent_backend.database import PostgresDatabase  # noqa: E402
from yaya_agent_backend.http_api import AgentHttpApi, HttpResponse  # noqa: E402
from yaya_agent_backend.student_skill_chain import (  # noqa: E402
    StudentSkillChainApplication,
    StudentSkillChainWorker,
)
from yaya_agent_backend.wire import ContractSchemaValidator  # noqa: E402
from yaya_agent_build import (  # noqa: E402
    CPP20_SAFE_V1_FLAGS,
    CPP20_SAFE_V1_PROFILE,
    canonical_source_bundle_sha256,
)
from yaya_agent_contracts import (  # noqa: E402
    ActorRef,
    ActorType,
    BuildArtifact,
    CertifiedSkill,
    ContentRef,
    EvidenceRef,
    EvidenceType,
    OperationContext,
    RequestContext,
    SkillRef,
    VersionSet,
    canonical_json_sha256,
)
from yaya_agent_runtime import CompileResultSnapshot, SkillSnapshot  # noqa: E402

LEARNER_ID = "learner_student_0001"
AGENT_PROFILE_ID = "agent_profile_watering_0001"
AUTHORITY_ID = "authority_watering_0001"
BUILD_POLICY_ID = "build_policy_watering_0001"
BUILD_ID = "build_watering_0001"
SKILL_ID = "skill_watering_0001"
SKILL_VERSION_ID = "skillver_watering_0001"
CERTIFICATION_ID = "cert_watering_0001"


def _json_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _content_wire(content: ContentRef) -> dict[str, object]:
    return {
        "unit_id": content.unit_id,
        "version": content.version,
        "content_hash": content.content_hash,
    }


def _context_wire(context: RequestContext | OperationContext) -> dict[str, object]:
    return {
        "schema_version": context.schema_version,
        "request_id": context.request_id,
        "correlation_id": context.correlation_id,
        "trace_id": context.trace_id,
        "requested_at": _iso(context.requested_at),
        "actor": {
            "tenant_id": context.actor.tenant_id,
            "actor_id": context.actor.actor_id,
            "actor_type": context.actor.actor_type.value,
            "roles": list(context.actor.roles),
        },
        "content_ref": _content_wire(context.content_ref),
    }


def _versions_wire(versions: VersionSet) -> dict[str, object]:
    value = plain(versions)
    if not isinstance(value, dict):
        raise AssertionError("VersionSet did not convert to an object")
    mapping = cast(dict[str, object], value)
    return {key: item for key, item in mapping.items() if item is not None}


def _request_context(context: OperationContext) -> RequestContext:
    return RequestContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        trace_id=context.trace_id,
        requested_at=context.requested_at,
        actor=context.actor,
        content_ref=context.content_ref,
        schema_version=context.schema_version,
    )


@dataclass(frozen=True, slots=True)
class _CanonicalAuthority:
    context: OperationContext
    versions: VersionSet
    learner_id: str
    agent_profile_id: str
    authority_id: str
    world_revision: int

    @property
    def session_request(self) -> dict[str, object]:
        return {
            "world_id": WORLD_ID,
            "learner_id": self.learner_id,
            "agent_profile_id": self.agent_profile_id,
            "channel": "GAME",
            "locale": "zh-CN",
            "content": _content_wire(self.context.content_ref),
            "expected_world_revision": self.world_revision,
        }


async def _seed_canonical_launch_authority(
    database: PostgresDatabase,
    context: OperationContext,
    versions: VersionSet,
) -> _CanonicalAuthority:
    """Seed the one canonical Task/World/Learner/Profile/launch authority graph.

    All public Session and Activation tests start from this helper.  Deliberately corrupt
    cases mutate or add a row only after this valid authority has been established.
    """

    task = make_task(context)
    state = make_world_state()
    learner_record: dict[str, object] = {
        "learner_id": LEARNER_ID,
        "actor_id": context.actor.actor_id,
        "content": _content_wire(context.content_ref),
        "revision": 0,
    }
    profile_record: dict[str, object] = {
        "agent_profile_id": AGENT_PROFILE_ID,
        "actor_id": context.actor.actor_id,
        "content": _content_wire(context.content_ref),
        "role": "farmer_tutor",
        "revision": 1,
    }
    encoded_versions = encode(versions)
    if not isinstance(encoded_versions, dict):
        raise AssertionError("VersionSet did not encode to an object")
    versions_json = cast(dict[str, object], encoded_versions)
    authority_projection: dict[str, object] = {
        "authority_id": AUTHORITY_ID,
        "learner_id": LEARNER_ID,
        "agent_profile_id": AGENT_PROFILE_ID,
        "world_id": WORLD_ID,
        "task_id": TASK_ID,
        "content_unit_id": context.content_ref.unit_id,
        "content_version": context.content_ref.version,
        "content_hash": context.content_ref.content_hash,
        "versions": versions_json,
    }
    async with database.transaction_with_commit_boundary() as connection:
        await connection.execute(
            """
            INSERT INTO yaya_tasks(tenant_id,task_id,actor_id,content_hash,snapshot_json)
            VALUES (%s,%s,%s,%s,%s)
            """,
            (
                context.actor.tenant_id,
                TASK_ID,
                context.actor.actor_id,
                context.content_ref.content_hash,
                Jsonb(encode(task)),
            ),
        )
        await connection.execute(
            """
            INSERT INTO yaya_worlds(
                tenant_id,world_id,actor_id,content_hash,stream_id,revision,
                last_event_sequence,state_hash,world_rules_version,state_json,
                request_context_json
            ) VALUES (%s,%s,%s,%s,%s,5,40,%s,%s,%s,%s)
            """,
            (
                context.actor.tenant_id,
                WORLD_ID,
                context.actor.actor_id,
                context.content_ref.content_hash,
                f"world:{WORLD_ID}",
                canonical_json_sha256(state),
                versions.world_rules_version,
                Jsonb(state),
                Jsonb(encode(_request_context(context))),
            ),
        )
        await connection.execute(
            """
            INSERT INTO yaya_learners(
                tenant_id,learner_id,actor_id,content_hash,record_sha256,record_json
            ) VALUES (%s,%s,%s,%s,%s,%s)
            """,
            (
                context.actor.tenant_id,
                LEARNER_ID,
                context.actor.actor_id,
                context.content_ref.content_hash,
                canonical_json_sha256(learner_record),
                Jsonb(learner_record),
            ),
        )
        await connection.execute(
            """
            INSERT INTO yaya_agent_profiles(
                tenant_id,agent_profile_id,actor_id,content_hash,record_sha256,record_json
            ) VALUES (%s,%s,%s,%s,%s,%s)
            """,
            (
                context.actor.tenant_id,
                AGENT_PROFILE_ID,
                context.actor.actor_id,
                context.content_ref.content_hash,
                canonical_json_sha256(profile_record),
                Jsonb(profile_record),
            ),
        )
        await connection.execute(
            """
            INSERT INTO yaya_launch_authorities(
                tenant_id,authority_id,actor_id,learner_id,content_unit_id,
                content_version,content_hash,world_id,agent_profile_id,task_id,
                active,versions_json,snapshot_sha256
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s,%s)
            """,
            (
                context.actor.tenant_id,
                AUTHORITY_ID,
                context.actor.actor_id,
                LEARNER_ID,
                context.content_ref.unit_id,
                context.content_ref.version,
                context.content_ref.content_hash,
                WORLD_ID,
                AGENT_PROFILE_ID,
                TASK_ID,
                Jsonb(versions_json),
                canonical_json_sha256(authority_projection),
            ),
        )
    return _CanonicalAuthority(
        context,
        versions,
        LEARNER_ID,
        AGENT_PROFILE_ID,
        AUTHORITY_ID,
        5,
    )


@dataclass(frozen=True, slots=True)
class _CertifiedFixture:
    artifact_path: Path
    artifact_sha256: str
    source_sha256: str


async def _seed_pre_certified_skill_closure(
    database: PostgresDatabase,
    validator: ContractSchemaValidator,
    authority: _CanonicalAuthority,
    artifact_root: Path,
) -> _CertifiedFixture:
    """Seed a contract-valid, already-certified 0003 closure without running Build.

    This is intentionally an Activation fixture, not a Build E2E shortcut.  It validates the
    frozen certified Build resource and writes every legacy/new certification edge consumed by
    ``StudentSkillChainWorker._activate_skill``.
    """

    source = '#include <iostream>\nint main(){ std::cout << "ok\\n"; }\n'
    source_file_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    source_bundle: dict[str, object] = {
        "language": "CPP20",
        "entrypoint": "main.cpp",
        "files": [
            {
                "path": "main.cpp",
                "content": source,
                "content_sha256": source_file_sha256,
            }
        ],
    }
    source_sha256 = canonical_source_bundle_sha256(source_bundle)
    artifact_bytes = b"yaya-certified-skill-fixture-v1\x00"
    artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    artifact_path = artifact_root / artifact_sha256[:2] / artifact_sha256
    artifact_path.parent.mkdir()
    artifact_path.write_bytes(artifact_bytes)
    artifact_path.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH)
    if artifact_path.stat().st_mode & 0o222:
        raise AssertionError("test platform did not materialize an immutable artifact fixture")

    context = authority.context
    operation = make_operation(command_id="cmd_certified_fixture_0001")
    request_context = operation
    issued_at = NOW
    compiler_image = "fixture/cpp@sha256:" + "d" * 64
    artifact_uri = f"artifact://sha256/{artifact_sha256}"
    certified_versions = replace(
        authority.versions,
        skill_version=SKILL_VERSION_ID,
        artifact_sha256=artifact_sha256,
        compiler_version="fixture-cpp-20",
        sandbox_image_digest=compiler_image,
        test_suite_version="watering-1",
    )
    parameter_schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": True,
        "properties": {},
    }
    certified_parameter_schema = {
        **parameter_schema,
        "x-yaya-certification": {
            "semantic_version": "1.0.0",
            "capabilities": ["WATER", "WORLD_READ"],
            "runtime_abi_version": "yaya-skill-json-stdio-v1",
        },
    }
    evidence_payload: dict[str, object] = {
        "evidence_kind": "BUILD_CERTIFICATION",
        "build_id": BUILD_ID,
        "skill_id": SKILL_ID,
        "skill_version_id": SKILL_VERSION_ID,
        "artifact_sha256": artifact_sha256,
        "test_suite_version": "watering-1",
        "outcome": "CERTIFIED",
    }
    evidence = EvidenceRef(
        evidence_id="evidence_build_fixture_0001",
        evidence_type=EvidenceType.TEST_REPORT,
        created_at=issued_at,
        sha256=canonical_json_sha256(evidence_payload),
    )
    evidence_wire = {
        "evidence_id": evidence.evidence_id,
        "evidence_type": evidence.evidence_type.value,
        "created_at": _iso(evidence.created_at),
        "sha256": evidence.sha256,
    }
    phases: list[dict[str, object]] = [
        {
            "name": name,
            "status": "PASSED",
            "started_at": _iso(issued_at),
            "finished_at": _iso(issued_at),
            "diagnostic_codes": [],
        }
        for name in (
            "VALIDATE_SOURCE",
            "COMPILE",
            "PUBLIC_TEST",
            "HIDDEN_TEST",
            "CERTIFY",
        )
    ]
    build_resource: dict[str, object] = {
        "request_context": _context_wire(request_context),
        "build_id": BUILD_ID,
        "skill_id": SKILL_ID,
        "skill_version_id": SKILL_VERSION_ID,
        "status": "CERTIFIED",
        "terminal": True,
        "created_at": _iso(issued_at),
        "updated_at": _iso(issued_at),
        "artifact": {
            "artifact_sha256": artifact_sha256,
            "source_sha256": source_sha256,
            "compiler_profile": CPP20_SAFE_V1_PROFILE,
            "compiler_version": "fixture-cpp-20",
            "test_suite_version": "watering-1",
        },
        "certification": {
            "certification_id": CERTIFICATION_ID,
            "issued_at": _iso(issued_at),
            "capabilities": ["WATER", "WORLD_READ"],
        },
        "phases": phases,
        "failure": None,
        "evidence_refs": [evidence_wire],
        "versions": _versions_wire(certified_versions),
    }
    validator.validate("schemas/game/skill-build.schema.json", build_resource)

    skill_ref = SkillRef(
        skill_id=SKILL_ID,
        skill_version_id=SKILL_VERSION_ID,
        artifact_sha256=artifact_sha256,
        certification_id=CERTIFICATION_ID,
    )
    skill = SkillSnapshot(
        ref=skill_ref,
        source_code=source,
        source_sha256=source_file_sha256,
        entrypoint="main.cpp",
        parameter_schema=certified_parameter_schema,
        request_context=operation,
    )
    legacy_artifact = BuildArtifact(
        artifact_sha256=artifact_sha256,
        source_sha256=source_sha256,
        compiler_profile=CPP20_SAFE_V1_PROFILE,
        compiler_version="fixture-cpp-20",
        sandbox_image_digest=compiler_image,
        test_suite_version="watering-1",
        artifact_uri=artifact_uri,
    )
    certified = CertifiedSkill(
        certification_id=CERTIFICATION_ID,
        skill_id=SKILL_ID,
        skill_version_id=SKILL_VERSION_ID,
        semantic_version="1.0.0",
        artifact=legacy_artifact,
        capabilities=("WATER", "WORLD_READ"),
        certified_at=issued_at,
        revoked_at=None,
        metadata={
            "build_id": BUILD_ID,
            "client_draft_revision": 0,
            "display_name": "Fixture watering Skill",
            "evidence_id": evidence.evidence_id,
            "source_bundle_sha256": source_sha256,
            "build_policy_id": BUILD_POLICY_ID,
            "policy_sha256": "0" * 64,
        },
    )
    policy_projection: dict[str, object] = {
        "build_policy_id": BUILD_POLICY_ID,
        "actor_id": context.actor.actor_id,
        "content_hash": context.content_ref.content_hash,
        "compiler_profile": CPP20_SAFE_V1_PROFILE,
        "test_suite_version": "watering-1",
        "compiler_image": "fixture/cpp@sha256:" + "d" * 64,
        "compiler_version": "fixture-cpp-20",
        "compile_flags": list(CPP20_SAFE_V1_FLAGS),
        "public_tests": [
            {
                "test_case_id": "public_0001",
                "visibility": "PUBLIC",
                "arguments": [],
                "stdin_base64": "",
                "expected_stdout_sha256": None,
            }
        ],
        "hidden_tests": [
            {
                "test_case_id": "hidden_0001",
                "visibility": "HIDDEN",
                "arguments": [],
                "stdin_base64": "",
                "expected_stdout_sha256": None,
            }
        ],
        "approved_capabilities": ["WATER", "WORLD_READ"],
        "limits": {
            "compile_wall_ms": 120_000,
            "test_wall_ms": 15_000,
            "memory_bytes": 536_870_912,
            "max_processes": 64,
            "cpu_millis": 1_000,
            "tmpfs_bytes": 67_108_864,
            "max_output_bytes": 65_536,
            "max_artifact_bytes": 16_777_216,
        },
        "parameter_schema": parameter_schema,
        "semantic_version_major": 1,
        "semantic_version_minor": 0,
        "runtime_abi_version": "yaya-skill-json-stdio-v1",
    }
    policy_sha256 = canonical_json_sha256(policy_projection)
    certified = replace(
        certified,
        metadata={
            **dict(certified.metadata),
            "policy_sha256": policy_sha256,
        },
    )
    tests_wire: list[dict[str, object]] = [
        {
            "test_case_id": "public_0001",
            "visibility": "PUBLIC",
            "status": "PASSED",
            "diagnostic_codes": [],
        },
        {
            "test_case_id": "hidden_0001",
            "visibility": "HIDDEN",
            "status": "PASSED",
            "diagnostic_codes": [],
        },
    ]
    certification_record: dict[str, object] = {
        "request_context": _context_wire(request_context),
        "certification_id": CERTIFICATION_ID,
        "build_id": BUILD_ID,
        "command_id": operation.command_id,
        "skill_id": SKILL_ID,
        "skill_version_id": SKILL_VERSION_ID,
        "learner_id": authority.learner_id,
        "world_id": WORLD_ID,
        "source_bundle_sha256": source_sha256,
        "build_policy_id": BUILD_POLICY_ID,
        "policy_sha256": policy_sha256,
        "client_draft_revision": 0,
        "display_name": "Fixture watering Skill",
        "parameter_schema": certified_parameter_schema,
        "artifact_sha256": artifact_sha256,
        "compiler_profile": CPP20_SAFE_V1_PROFILE,
        "compiler_version": "fixture-cpp-20",
        "compiler_image": compiler_image,
        "test_suite_version": "watering-1",
        "semantic_version": "1.0.0",
        "runtime_abi_version": "yaya-skill-json-stdio-v1",
        "tests": tests_wire,
        "requested_capabilities": ["WATER", "WORLD_READ"],
        "approved_capabilities": ["WATER", "WORLD_READ"],
        "evidence_ref": evidence_wire,
        "certified_at": _iso(issued_at),
        "versions": _versions_wire(certified_versions),
    }
    compile_result = CompileResultSnapshot(
        build_id=BUILD_ID,
        skill_ref=skill_ref,
        succeeded=True,
        diagnostics=(),
        evidence_refs=(evidence,),
        request_context=operation,
    )
    evidence_document: dict[str, object] = {
        "request_context": _context_wire(operation),
        "evidence_ref": evidence_wire,
        "subject": {"learner_id": authority.learner_id},
        "source": {
            "source_type": "SKILL_BUILD",
            "source_id": BUILD_ID,
            "command_id": operation.command_id,
            "world_id": WORLD_ID,
        },
        "occurred_at": _iso(issued_at),
        "recorded_at": _iso(issued_at),
        "integrity": {
            "payload_sha256": evidence.sha256,
            "previous_evidence_sha256": None,
        },
        "payload": evidence_payload,
        "related_evidence": [],
        "versions": _versions_wire(certified_versions),
    }
    validator.validate("schemas/game/evidence.schema.json", evidence_document)
    accepted_resource: dict[str, object] = {
        **build_resource,
        "skill_version_id": None,
        "status": "ACCEPTED",
        "terminal": False,
        "artifact": None,
        "certification": None,
        "phases": [
            {
                "name": phase,
                "status": "PENDING",
                "started_at": None,
                "finished_at": None,
                "diagnostic_codes": [],
            }
            for phase in (
                "VALIDATE_SOURCE",
                "COMPILE",
                "PUBLIC_TEST",
                "HIDDEN_TEST",
                "CERTIFY",
            )
        ],
        "evidence_refs": [],
        "versions": _versions_wire(authority.versions),
    }
    compiling_resource: dict[str, object] = {
        **accepted_resource,
        "status": "COMPILING",
        "phases": [
            {
                "name": phase,
                "status": (
                    "PASSED"
                    if phase == "VALIDATE_SOURCE"
                    else "RUNNING"
                    if phase == "COMPILE"
                    else "PENDING"
                ),
                "started_at": _iso(issued_at) if phase in {"VALIDATE_SOURCE", "COMPILE"} else None,
                "finished_at": _iso(issued_at) if phase == "VALIDATE_SOURCE" else None,
                "diagnostic_codes": [],
            }
            for phase in (
                "VALIDATE_SOURCE",
                "COMPILE",
                "PUBLIC_TEST",
                "HIDDEN_TEST",
                "CERTIFY",
            )
        ],
    }
    validator.validate("schemas/game/skill-build.schema.json", accepted_resource)
    validator.validate("schemas/game/skill-build.schema.json", compiling_resource)
    build_identity = hashlib.sha256(b"fixture-build-identity").hexdigest()
    async with database.transaction_with_commit_boundary() as connection:
        await connection.execute(
            """
            INSERT INTO yaya_build_policies(
                tenant_id,build_policy_id,actor_id,content_hash,compiler_profile,
                test_suite_version,compiler_image,compiler_version,compile_flags_json,
                public_tests_json,hidden_tests_json,approved_capabilities_json,
                limits_json,parameter_schema_json,semantic_version_major,
                semantic_version_minor,runtime_abi_version,policy_sha256,active
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE)
            """,
            (
                context.actor.tenant_id,
                BUILD_POLICY_ID,
                context.actor.actor_id,
                context.content_ref.content_hash,
                CPP20_SAFE_V1_PROFILE,
                "watering-1",
                policy_projection["compiler_image"],
                "fixture-cpp-20",
                Jsonb(policy_projection["compile_flags"]),
                Jsonb(policy_projection["public_tests"]),
                Jsonb(policy_projection["hidden_tests"]),
                Jsonb(policy_projection["approved_capabilities"]),
                Jsonb(policy_projection["limits"]),
                Jsonb(policy_projection["parameter_schema"]),
                policy_projection["semantic_version_major"],
                policy_projection["semantic_version_minor"],
                policy_projection["runtime_abi_version"],
                policy_sha256,
            ),
        )
        await connection.execute(
            """
            INSERT INTO yaya_skill_builds(
                tenant_id,build_id,authority_id,skill_id,actor_id,content_hash,
                client_draft_revision,source_bundle_sha256,source_bundle_json,
                build_policy_id,compiler_profile,test_suite_version,
                requested_capabilities_json,command_id,status,terminal,
                resource_sha256,resource_json,created_at,updated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,0,%s,%s,%s,%s,%s,%s,%s,
                      'CERTIFIED',TRUE,%s,%s,%s,%s)
            """,
            (
                context.actor.tenant_id,
                BUILD_ID,
                authority.authority_id,
                SKILL_ID,
                context.actor.actor_id,
                context.content_ref.content_hash,
                source_sha256,
                Jsonb(source_bundle),
                BUILD_POLICY_ID,
                CPP20_SAFE_V1_PROFILE,
                "watering-1",
                Jsonb(["WATER", "WORLD_READ"]),
                "cmd_certified_fixture_0001",
                canonical_json_sha256(build_resource),
                Jsonb(build_resource),
                issued_at,
                issued_at,
            ),
        )
        await connection.execute(
            """
            INSERT INTO yaya_artifacts(
                tenant_id,artifact_sha256,build_id,skill_id,actor_id,content_hash,
                source_sha256,artifact_uri,metadata_json,created_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                context.actor.tenant_id,
                artifact_sha256,
                BUILD_ID,
                SKILL_ID,
                context.actor.actor_id,
                context.content_ref.content_hash,
                source_sha256,
                artifact_uri,
                Jsonb(
                    {
                        "artifact_sha256": artifact_sha256,
                        "artifact_uri": artifact_uri,
                        "size_bytes": len(artifact_bytes),
                        "source_sha256": source_sha256,
                        "build_policy_id": BUILD_POLICY_ID,
                        "policy_sha256": policy_sha256,
                        "compiler_profile": CPP20_SAFE_V1_PROFILE,
                        "compiler_version": "fixture-cpp-20",
                        "compiler_image": compiler_image,
                        "test_suite_version": "watering-1",
                        "build_identity": build_identity,
                    }
                ),
                issued_at,
            ),
        )
        await connection.execute(
            """
            INSERT INTO yaya_skills(
                tenant_id,skill_id,skill_version_id,certification_id,actor_id,
                session_id,content_hash,artifact_sha256,snapshot_json,active
            ) VALUES (%s,%s,%s,%s,%s,NULL,%s,%s,%s,FALSE)
            """,
            (
                context.actor.tenant_id,
                SKILL_ID,
                SKILL_VERSION_ID,
                CERTIFICATION_ID,
                context.actor.actor_id,
                context.content_ref.content_hash,
                artifact_sha256,
                Jsonb(encode(skill)),
            ),
        )
        await connection.execute(
            """
            INSERT INTO yaya_registry_certifications(
                tenant_id,certification_id,skill_id,skill_version_id,
                artifact_sha256,record_json,rejected
            ) VALUES (%s,%s,%s,%s,%s,%s,FALSE)
            """,
            (
                context.actor.tenant_id,
                CERTIFICATION_ID,
                SKILL_ID,
                SKILL_VERSION_ID,
                artifact_sha256,
                Jsonb(encode(certified)),
            ),
        )
        await connection.execute(
            """
            INSERT INTO yaya_skill_certifications(
                tenant_id,certification_id,build_id,skill_id,skill_version_id,
                artifact_sha256,actor_id,content_hash,certification_sha256,
                record_json,issued_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                context.actor.tenant_id,
                CERTIFICATION_ID,
                BUILD_ID,
                SKILL_ID,
                SKILL_VERSION_ID,
                artifact_sha256,
                context.actor.actor_id,
                context.content_ref.content_hash,
                canonical_json_sha256(certification_record),
                Jsonb(certification_record),
                issued_at,
            ),
        )
        for sequence, status, resource in (
            (1, "ACCEPTED", accepted_resource),
            (2, "COMPILING", compiling_resource),
            (3, "CERTIFIED", build_resource),
        ):
            digest = canonical_json_sha256(resource)
            await connection.execute(
                """
                INSERT INTO yaya_skill_build_history(
                    tenant_id,build_id,sequence,status,record_sha256,record_json,recorded_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    BUILD_ID,
                    sequence,
                    status,
                    digest,
                    Jsonb(resource),
                    issued_at,
                ),
            )
        for phase in (
            "VALIDATE_SOURCE",
            "COMPILE",
            "PUBLIC_TEST",
            "HIDDEN_TEST",
            "CERTIFY",
        ):
            phase_tests: list[dict[str, object]] = (
                tests_wire[:1]
                if phase == "PUBLIC_TEST"
                else tests_wire[1:]
                if phase == "HIDDEN_TEST"
                else []
            )
            receipt: dict[str, object] = {
                "build_id": BUILD_ID,
                "build_identity": build_identity,
                "step": phase,
                "attempt": 1,
                "source_sha256": source_sha256,
                "build_policy_id": BUILD_POLICY_ID,
                "policy_sha256": policy_sha256,
                "outcome": "PASSED",
                "pipeline_status": "SUCCEEDED",
                "terminal_failure_code": None,
                "artifact_sha256": artifact_sha256,
                "test_results": phase_tests,
            }
            receipt_input = {
                "build_id": BUILD_ID,
                "step": phase,
                "source_sha256": source_sha256,
                "build_policy_id": BUILD_POLICY_ID,
                "policy_sha256": policy_sha256,
            }
            await connection.execute(
                """
                INSERT INTO yaya_build_step_receipts(
                    tenant_id,build_id,step,attempt,input_sha256,output_sha256,
                    outcome,receipt_json,completed_at
                ) VALUES (%s,%s,%s,1,%s,%s,'PASSED',%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    BUILD_ID,
                    phase,
                    canonical_json_sha256(receipt_input),
                    canonical_json_sha256(receipt),
                    Jsonb(receipt),
                    issued_at,
                ),
            )
        await connection.execute(
            """
            INSERT INTO yaya_compile_results(
                tenant_id,build_id,actor_id,content_hash,snapshot_json
            ) VALUES (%s,%s,%s,%s,%s)
            """,
            (
                context.actor.tenant_id,
                BUILD_ID,
                context.actor.actor_id,
                context.content_ref.content_hash,
                Jsonb(encode(compile_result)),
            ),
        )
        await connection.execute(
            """
            INSERT INTO yaya_evidence(
                tenant_id,evidence_id,actor_id,content_hash,evidence_type,
                payload_sha256,evidence_json,recorded_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                context.actor.tenant_id,
                evidence.evidence_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
                evidence.evidence_type.value,
                evidence.sha256,
                Jsonb(evidence_document),
                issued_at,
            ),
        )
    return _CertifiedFixture(artifact_path, artifact_sha256, source_sha256)


class StudentSkillChainSurfaceTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._server_context = postgres_test_server()
        try:
            cls.server = cls._server_context.__enter__()
            cls.database = PostgresDatabase(cls.server.dsn)
            asyncio.run(cls.database.migrate())
        except BaseException:
            cls._server_context.__exit__(*sys.exc_info())
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server_context.__exit__(None, None, None)

    async def asyncSetUp(self) -> None:
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                TRUNCATE yaya_skill_activations,yaya_registry_entries,yaya_registry_heads,
                    yaya_certification_revocations,yaya_skill_certifications,yaya_artifacts,
                    yaya_build_step_receipts,yaya_skill_build_history,yaya_skill_builds,
                    yaya_build_policies,yaya_compile_results,yaya_evidence,
                    yaya_control_jobs,yaya_public_agent_sessions,yaya_launch_authorities,
                    yaya_agent_profiles,yaya_learners,yaya_registry_certifications,
                    yaya_skills,yaya_commands,yaya_agent_sessions,yaya_worlds,yaya_tasks CASCADE
                """
            )
        finally:
            await connection.close()

        self.context = make_operation()
        self.versions = make_versions()
        self.validator = ContractSchemaValidator(CONTRACTS_ROOT)
        self.authority = await _seed_canonical_launch_authority(
            self.database,
            self.context,
            self.versions,
        )
        self._artifact_directory = tempfile.TemporaryDirectory(prefix="yaya-chain-artifacts-")
        self.artifact_root = Path(self._artifact_directory.name).resolve()
        self.chain = StudentSkillChainApplication(
            self.database,
            self.validator,
            self.versions,
            artifact_root=self.artifact_root,
        )
        self.authenticator = JwtAuthenticator(
            hmac_secret="student-chain-http-secret-0000000000000000",
            issuer="yaya-student-chain-test",
            audience="yaya-agent-test",
        )
        self.token = self.authenticator.issue_for_test(
            self.context.actor,
            now=datetime.now(UTC),
        )
        self.http = AgentHttpApi(
            application=AgentTurnApplication(
                self.database,
                CONTRACTS_ROOT,
                self.versions,
            ),
            authenticator=self.authenticator,
            validator=self.validator,
            student_chain=self.chain,
        )
        self.worker = StudentSkillChainWorker(
            database=self.database,
            application=self.chain,
            validator=self.validator,
            worker_id="student-chain-test-worker",
            artifact_root=self.artifact_root,
        )

    async def asyncTearDown(self) -> None:
        for candidate in self.artifact_root.rglob("*"):
            if candidate.is_file():
                candidate.chmod(stat.S_IWRITE | stat.S_IREAD)
        self._artifact_directory.cleanup()

    def _headers(
        self,
        raw_body: bytes,
        *,
        suffix: str,
        idempotency_key: str,
        token: str | None = None,
    ) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token or self.token}",
            "X-Schema-Version": "1.0.0",
            "X-Request-Id": f"req_chain_{suffix}",
            "X-Trace-Id": f"trace_chain_{suffix}",
            "X-Correlation-Id": f"corr_chain_{suffix}",
            "Idempotency-Key": idempotency_key,
            "Content-Type": "application/json",
            "Content-Length": str(len(raw_body)),
        }

    def _get_headers(self, *, suffix: str, token: str | None = None) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token or self.token}",
            "X-Schema-Version": "1.0.0",
            "X-Request-Id": f"req_chain_{suffix}",
            "X-Trace-Id": f"trace_chain_{suffix}",
            "X-Correlation-Id": f"corr_chain_{suffix}",
        }

    async def _post(
        self,
        target: str,
        body: dict[str, object],
        *,
        suffix: str,
        idempotency_key: str,
    ) -> tuple[HttpResponse, dict[str, object], bytes]:
        raw_body = _json_bytes(body)
        response = await self.http.handle(
            "POST",
            target,
            self._headers(
                raw_body,
                suffix=suffix,
                idempotency_key=idempotency_key,
            ),
            raw_body,
        )
        payload = cast(dict[str, object], json.loads(response.body))
        return response, payload, raw_body

    async def _job_row(self, command_id: str) -> dict[str, object]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT j.*,c.status AS command_status,c.revision AS command_revision,
                       c.record_json AS command_json
                FROM yaya_control_jobs j
                JOIN yaya_commands c
                  ON c.tenant_id=j.tenant_id AND c.command_id=j.command_id
                WHERE j.tenant_id=%s AND j.command_id=%s
                """,
                (self.context.actor.tenant_id, command_id),
            )
            row = await cursor.fetchone()
            if row is None:
                self.fail("accepted control job disappeared")
            return row
        finally:
            await connection.close()

    async def test_session_http_accept_replay_worker_and_get_are_one_chain(self) -> None:
        body = self.authority.session_request
        first, first_payload, raw = await self._post(
            "/v1/agent-sessions",
            body,
            suffix="session_accept_0001",
            idempotency_key="session-chain-accept-0001",
        )
        self.assertEqual(first.status, 202)
        self.assertEqual(first.headers["Idempotency-Replayed"], "false")
        self.assertEqual(first.headers["X-Trace-Id"], "trace_chain_session_accept_0001")
        command_id = cast(str, first_payload["command_id"])
        job_id = cast(str, first_payload["job_id"])
        self.assertEqual(first.headers["Location"], f"/v1/commands/{command_id}")

        replay, replay_payload, _ = await self._post(
            "/v1/agent-sessions",
            body,
            suffix="session_replay_0002",
            idempotency_key="session-chain-accept-0001",
        )
        self.assertEqual(replay.status, 202)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(replay.headers["X-Trace-Id"], "trace_chain_session_replay_0002")
        self.assertEqual(replay_payload, first_payload)

        accepted = await self._job_row(command_id)
        self.assertEqual(accepted["job_id"], job_id)
        self.assertEqual(accepted["request_body"], raw)
        self.assertEqual(
            accepted["request_sha256"],
            hashlib.sha256(raw).hexdigest(),
        )
        self.assertEqual(accepted["state"], "READY")
        self.assertEqual(accepted["command_status"], "ACCEPTED")

        self.assertTrue(await self.worker.run_once())
        self.assertFalse(await self.worker.run_once())
        terminal = await self._job_row(command_id)
        self.assertEqual(terminal["state"], "SUCCEEDED")
        self.assertEqual(terminal["command_status"], "APPLIED")
        self.assertEqual(terminal["attempt"], 1)
        resource_id = cast(str, terminal["resource_id"])

        before_get = (
            terminal["state"],
            terminal["command_status"],
            terminal["command_revision"],
            terminal["updated_at"],
        )
        response = await self.http.handle(
            "GET",
            f"/v1/agent-sessions/{resource_id}",
            self._get_headers(suffix="session_get_0003"),
        )
        self.assertEqual(response.status, 200)
        session = cast(dict[str, object], json.loads(response.body))
        self.validator.validate("schemas/game/agent-session.schema.json", session)
        self.assertEqual(session["session_id"], resource_id)
        self.assertEqual(session["world_id"], WORLD_ID)
        self.assertEqual(session["learner_id"], LEARNER_ID)
        self.assertEqual(session["agent_profile_id"], AGENT_PROFILE_ID)
        self.assertEqual(session["last_turn_sequence"], 0)

        after_get = await self._job_row(command_id)
        self.assertEqual(
            (
                after_get["state"],
                after_get["command_status"],
                after_get["command_revision"],
                after_get["updated_at"],
            ),
            before_get,
        )
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM yaya_commands) AS commands,
                    (SELECT count(*) FROM yaya_control_jobs) AS jobs,
                    (SELECT count(*) FROM yaya_agent_sessions) AS legacy_sessions,
                    (SELECT count(*) FROM yaya_public_agent_sessions) AS public_sessions
                """
            )
            counts = await cursor.fetchone()
            self.assertEqual(
                counts,
                {
                    "commands": 1,
                    "jobs": 1,
                    "legacy_sessions": 1,
                    "public_sessions": 1,
                },
            )
        finally:
            await connection.close()

    async def test_session_rejects_reused_key_duplicate_json_and_unknown_fields(self) -> None:
        body = self.authority.session_request
        accepted, _, _ = await self._post(
            "/v1/agent-sessions",
            body,
            suffix="session_strict_accept_0001",
            idempotency_key="session-chain-strict-0001",
        )
        self.assertEqual(accepted.status, 202)

        changed = dict(body)
        changed["locale"] = "en-US"
        reused, reused_payload, _ = await self._post(
            "/v1/agent-sessions",
            changed,
            suffix="session_reused_0002",
            idempotency_key="session-chain-strict-0001",
        )
        self.assertEqual(reused.status, 409)
        self.assertEqual(
            cast(dict[str, object], reused_payload["error"])["code"], "IDEMPOTENCY_KEY_REUSED"
        )

        valid_raw = _json_bytes(body)
        duplicate_raw = valid_raw.replace(
            b'"world_id":',
            b'"world_id":"world_duplicate_0001","world_id":',
            1,
        )
        duplicate = await self.http.handle(
            "POST",
            "/v1/agent-sessions",
            self._headers(
                duplicate_raw,
                suffix="session_duplicate_0003",
                idempotency_key="session-chain-duplicate-0001",
            ),
            duplicate_raw,
        )
        self.assertEqual(duplicate.status, 400)
        duplicate_payload = cast(dict[str, object], json.loads(duplicate.body))
        self.assertEqual(
            cast(dict[str, object], duplicate_payload["error"])["code"],
            "INVALID_REQUEST",
        )

        unknown = dict(body)
        unknown["unexpected"] = True
        unknown_response, unknown_payload, _ = await self._post(
            "/v1/agent-sessions",
            unknown,
            suffix="session_unknown_0004",
            idempotency_key="session-chain-unknown-0001",
        )
        self.assertEqual(unknown_response.status, 400)
        self.assertEqual(
            cast(dict[str, object], unknown_payload["error"])["code"],
            "INVALID_REQUEST",
        )

        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute("SELECT count(*) AS count FROM yaya_control_jobs")
            row = await cursor.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(cast(dict[str, object], row)["count"], 1)
        finally:
            await connection.close()

    async def test_session_worker_rejects_world_drift_without_partial_resource(self) -> None:
        accepted, payload, _ = await self._post(
            "/v1/agent-sessions",
            self.authority.session_request,
            suffix="session_drift_accept_0001",
            idempotency_key="session-chain-drift-0001",
        )
        self.assertEqual(accepted.status, 202)
        command_id = cast(str, payload["command_id"])
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                UPDATE yaya_worlds SET revision=6,updated_at=clock_timestamp()
                WHERE tenant_id=%s AND world_id=%s
                """,
                (self.context.actor.tenant_id, WORLD_ID),
            )
        finally:
            await connection.close()

        self.assertTrue(await self.worker.run_once())
        terminal = await self._job_row(command_id)
        self.assertEqual(terminal["state"], "FAILED")
        self.assertEqual(terminal["last_error_code"], "WORLD_REVISION_CONFLICT")
        self.assertEqual(terminal["command_status"], "REJECTED")
        resource_id = cast(str, terminal["resource_id"])

        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM yaya_agent_sessions) AS legacy_sessions,
                    (SELECT count(*) FROM yaya_public_agent_sessions) AS public_sessions
                """
            )
            counts = await cursor.fetchone()
            self.assertEqual(
                counts,
                {"legacy_sessions": 0, "public_sessions": 0},
            )
        finally:
            await connection.close()
        missing = await self.http.handle(
            "GET",
            f"/v1/agent-sessions/{resource_id}",
            self._get_headers(suffix="session_drift_get_0002"),
        )
        self.assertEqual(missing.status, 404)

    async def test_session_get_hides_other_actor_and_fails_closed_on_hash_drift(self) -> None:
        accepted, payload, _ = await self._post(
            "/v1/agent-sessions",
            self.authority.session_request,
            suffix="session_corrupt_accept_0001",
            idempotency_key="session-chain-corrupt-0001",
        )
        self.assertEqual(accepted.status, 202)
        command_id = cast(str, payload["command_id"])
        self.assertTrue(await self.worker.run_once())
        terminal = await self._job_row(command_id)
        resource_id = cast(str, terminal["resource_id"])

        other_actor = ActorRef(
            tenant_id=self.context.actor.tenant_id,
            actor_id="student_other_0001",
            actor_type=ActorType.STUDENT,
            roles=("game:player",),
        )
        other_token = self.authenticator.issue_for_test(other_actor, now=datetime.now(UTC))
        hidden = await self.http.handle(
            "GET",
            f"/v1/agent-sessions/{resource_id}",
            self._get_headers(suffix="session_hidden_0002", token=other_token),
        )
        self.assertEqual(hidden.status, 404)

        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                UPDATE yaya_public_agent_sessions SET resource_sha256=%s
                WHERE tenant_id=%s AND session_id=%s
                """,
                ("f" * 64, self.context.actor.tenant_id, resource_id),
            )
        finally:
            await connection.close()
        corrupt = await self.http.handle(
            "GET",
            f"/v1/agent-sessions/{resource_id}",
            self._get_headers(suffix="session_corrupt_get_0003"),
        )
        self.assertEqual(corrupt.status, 500)
        corrupt_payload = cast(dict[str, object], json.loads(corrupt.body))
        self.assertIsNone(corrupt_payload["data"])
        self.assertEqual(
            cast(dict[str, object], corrupt_payload["error"])["code"],
            "INVARIANT_VIOLATION",
        )
        self.assertNotIn(resource_id, corrupt.body.decode("utf-8"))

    async def test_activation_http_replay_worker_get_and_registry_cas(self) -> None:
        fixture = await _seed_pre_certified_skill_closure(
            self.database,
            self.validator,
            self.authority,
            self.artifact_root,
        )
        self.assertTrue(fixture.artifact_path.is_file())
        body: dict[str, object] = {
            "expected_registry_revision": 0,
            "activation_scope": {
                "world_id": WORLD_ID,
                "agent_profile_id": AGENT_PROFILE_ID,
            },
            "reason": "Activate the already-certified fixture SkillVersion.",
        }
        target = f"/v1/skill-versions/{SKILL_VERSION_ID}/activations"
        accepted, payload, raw = await self._post(
            target,
            body,
            suffix="activation_accept_0001",
            idempotency_key="activation-chain-accept-0001",
        )
        self.assertEqual(accepted.status, 202)
        self.assertEqual(accepted.headers["Idempotency-Replayed"], "false")
        command_id = cast(str, payload["command_id"])

        replay, replay_payload, _ = await self._post(
            target,
            body,
            suffix="activation_replay_0002",
            idempotency_key="activation-chain-accept-0001",
        )
        self.assertEqual(replay.status, 202)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(replay_payload, payload)
        self.assertEqual((await self._job_row(command_id))["request_body"], raw)

        self.assertTrue(await self.worker.run_once())
        terminal = await self._job_row(command_id)
        self.assertEqual(terminal["state"], "SUCCEEDED")
        self.assertEqual(terminal["command_status"], "APPLIED")
        activation_id = cast(str, terminal["resource_id"])
        response = await self.http.handle(
            "GET",
            f"/v1/skill-activations/{activation_id}",
            self._get_headers(suffix="activation_get_0003"),
        )
        self.assertEqual(response.status, 200)
        activation = cast(dict[str, object], json.loads(response.body))
        self.validator.validate("schemas/game/skill-activation.schema.json", activation)
        self.assertEqual(activation["skill_version_id"], SKILL_VERSION_ID)
        self.assertEqual(activation["artifact_sha256"], fixture.artifact_sha256)
        self.assertEqual(activation["previous_registry_revision"], 0)
        self.assertEqual(activation["registry_revision"], 1)

        stale, stale_payload, _ = await self._post(
            target,
            body,
            suffix="activation_stale_accept_0004",
            idempotency_key="activation-chain-stale-0001",
        )
        self.assertEqual(stale.status, 409)
        self.assertEqual(
            cast(dict[str, object], stale_payload["error"])["code"],
            "CONTENT_VERSION_MISMATCH",
        )

        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM yaya_registry_entries) AS entries,
                    (SELECT count(*) FROM yaya_skill_activations) AS activations,
                    (SELECT revision FROM yaya_registry_heads
                     WHERE tenant_id=%s AND actor_id=%s AND skill_id=%s) AS revision
                """,
                (
                    self.context.actor.tenant_id,
                    self.context.actor.actor_id,
                    SKILL_ID,
                ),
            )
            counts = await cursor.fetchone()
            self.assertEqual(
                counts,
                {"entries": 1, "activations": 1, "revision": 1},
            )
        finally:
            await connection.close()

    async def test_activation_rejects_artifact_drift_without_registry_write(self) -> None:
        fixture = await _seed_pre_certified_skill_closure(
            self.database,
            self.validator,
            self.authority,
            self.artifact_root,
        )
        body: dict[str, object] = {
            "expected_registry_revision": 0,
            "activation_scope": {
                "world_id": WORLD_ID,
                "agent_profile_id": AGENT_PROFILE_ID,
            },
        }
        accepted, payload, _ = await self._post(
            f"/v1/skill-versions/{SKILL_VERSION_ID}/activations",
            body,
            suffix="activation_drift_accept_0001",
            idempotency_key="activation-chain-drift-0001",
        )
        self.assertEqual(accepted.status, 202)
        fixture.artifact_path.chmod(stat.S_IWRITE | stat.S_IREAD)
        fixture.artifact_path.write_bytes(b"drifted-after-acceptance")

        self.assertTrue(await self.worker.run_once())
        terminal = await self._job_row(cast(str, payload["command_id"]))
        self.assertEqual(terminal["state"], "FAILED")
        self.assertEqual(terminal["last_error_code"], "INVARIANT_VIOLATION")
        self.assertEqual(terminal["command_status"], "FAILED")
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM yaya_registry_heads) AS heads,
                    (SELECT count(*) FROM yaya_registry_entries) AS entries,
                    (SELECT count(*) FROM yaya_skill_activations) AS activations
                """
            )
            counts = await cursor.fetchone()
            self.assertEqual(counts, {"heads": 0, "entries": 0, "activations": 0})
        finally:
            await connection.close()


if __name__ == "__main__":
    unittest.main()
