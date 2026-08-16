from __future__ import annotations

import asyncio
import copy
import hashlib
import sys
import unittest
from collections.abc import Mapping
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
CONTRACTS_ROOT = Path(__file__).resolve().parents[1] / "contracts"
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from agent_runtime_fixtures import (  # noqa: E402
    NOW,
    WORLD_ID,
    make_operation,
    make_session,
    make_versions,
)
from psycopg import AsyncConnection  # noqa: E402
from psycopg.errors import ObjectNotInPrerequisiteState  # noqa: E402
from yaya_agent_backend.application import (  # noqa: E402
    AgentTurnApplication,
    BackendApplicationError,
    _request_context_wire,
)
from yaya_agent_backend.codec import decode_as, encode, plain  # noqa: E402
from yaya_agent_backend.invocation import PostgresSkillInvocationService  # noqa: E402
from yaya_agent_backend.repositories import (  # noqa: E402
    PostgresSkillRepository,
    RepositoryAuthorityError,
)
from yaya_agent_build import (  # noqa: E402
    canonical_source_bundle_sha256,
)
from yaya_agent_contracts import (  # noqa: E402
    ActiveSkill,
    BuildArtifact,
    CertifiedSkill,
    OperationContext,
    SkillRef,
    VersionSet,
    canonical_json_sha256,
)
from yaya_agent_runtime import (  # noqa: E402
    AgentToolExecutionError,
    SessionSnapshot,
    SkillInvocationRequest,
    SkillSnapshot,
    skill_invocation_request_sha256,
)

BUILD_ID = "build_runtime_closure_0001"
BUILD_COMMAND_ID = "cmd_runtime_build_0001"
BUILD_POLICY_ID = "policy_runtime_closure_0001"
SKILL_ID = "skill_runtime_closure_0001"
SKILL_VERSION_ID = "skillver_runtime_closure_0001"
CERTIFICATION_ID = "cert_runtime_closure_0001"
LEARNER_ID = "learner_runtime_closure_0001"


def _versions_wire(versions: VersionSet) -> dict[str, object]:
    value = plain(versions)
    if not isinstance(value, Mapping):
        raise AssertionError("VersionSet did not render as an object")
    return {
        cast(str, key): item
        for key, item in value.items()
        if isinstance(key, str) and item is not None
    }


class _ConnectionTimeZoneDatabase:
    def __init__(self, database: Any, time_zone: str) -> None:
        self._database = database
        self._time_zone = time_zone

    async def connect(self, *, autocommit: bool = False) -> AsyncConnection[dict[str, object]]:
        connection = await self._database.connect(autocommit=autocommit)
        await connection.execute(
            "SELECT set_config('TimeZone', %s, false)",
            (self._time_zone,),
        )
        return cast(AsyncConnection[dict[str, object]], connection)


def _encoded(value: object) -> object:
    return encode(value)


def _plain_mapping(value: object) -> dict[str, object]:
    rendered = plain(value)
    if not isinstance(rendered, Mapping):
        raise AssertionError("contract model did not render as an object")
    return {cast(str, key): item for key, item in rendered.items() if isinstance(key, str)}


class _Cursor:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    async def fetchone(self) -> dict[str, object] | None:
        return self._row


class _RepositoryConnection:
    """Small repository seam that returns durable rows and records writes."""

    def __init__(
        self,
        *,
        scoped_row: dict[str, object] | None,
        public_session: bool = True,
        legacy_row: dict[str, object] | None = None,
    ) -> None:
        self.scoped_row = scoped_row
        self.public_session = public_session
        self.legacy_row = legacy_row
        self.binding: dict[str, object] | None = None
        self.binding_writes = 0

    async def execute(
        self,
        query: object,
        params: object = None,
    ) -> _Cursor:
        sql = str(query)
        if "full_c.record_json AS full_certification_json" in sql:
            return _Cursor(self.scoped_row)
        if "INSERT INTO yaya_session_skill_versions" in sql:
            if not isinstance(params, tuple) or len(params) != 11:
                raise AssertionError("session binding parameters drifted")
            self.binding_writes += 1
            self.binding = {
                "binding_id": params[1],
                "certification_id": params[5],
                "artifact_sha256": params[6],
                "actor_id": params[7],
                "content_hash": params[8],
                "binding_sha256": params[9],
            }
            return _Cursor(None)
        if "FROM yaya_session_skill_versions" in sql:
            return _Cursor(self.binding)
        if "SELECT 1 FROM yaya_public_agent_sessions" in sql:
            return _Cursor({"present": 1} if self.public_session else None)
        if "JOIN yaya_registry_active a" in sql:
            return _Cursor(self.legacy_row)
        raise AssertionError(f"unexpected repository query: {sql[:80]}")


class _ClosureFixture:
    def __init__(self) -> None:
        self.build_context = make_operation(command_id=BUILD_COMMAND_ID)
        self.turn_context = make_operation(command_id="cmd_runtime_turn_0001")
        self.session: SessionSnapshot = make_session(operation=self.turn_context)
        self.versions = make_versions()
        self.skill_ref = SkillRef(
            skill_id=SKILL_ID,
            skill_version_id=SKILL_VERSION_ID,
            artifact_sha256="b" * 64,
            certification_id=CERTIFICATION_ID,
        )
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
        compiler_image = f"gcc@sha256:{'c' * 64}"
        compiler_version = "gcc-14.2.0"
        compiler_profile = "YAYA_CPP20_SAFE_V1"
        test_suite_version = "suite-runtime-1"
        requested_capabilities: list[object] = ["world.read"]
        approved_capabilities: list[object] = ["world.read"]
        public_tests: list[object] = [
            {"test_case_id": "public_runtime_0001", "visibility": "PUBLIC"}
        ]
        hidden_tests: list[object] = [
            {"test_case_id": "hidden_runtime_0001", "visibility": "HIDDEN"}
        ]
        expected_tests: list[dict[str, object]] = [
            {
                "test_case_id": "public_runtime_0001",
                "visibility": "PUBLIC",
                "status": "PASSED",
                "diagnostic_codes": [],
            },
            {
                "test_case_id": "hidden_runtime_0001",
                "visibility": "HIDDEN",
                "status": "PASSED",
                "diagnostic_codes": [],
            },
        ]
        base_parameter_schema: dict[str, object] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        }
        client_draft_revision = 1
        runtime_abi_version = "yaya-skill-runtime-v1"
        semantic_version = "1.0.1"
        parameter_schema = dict(base_parameter_schema)
        parameter_schema["x-yaya-certification"] = {
            "semantic_version": semantic_version,
            "capabilities": requested_capabilities,
            "runtime_abi_version": runtime_abi_version,
        }
        policy_projection: dict[str, object] = {
            "build_policy_id": BUILD_POLICY_ID,
            "actor_id": self.build_context.actor.actor_id,
            "content_hash": self.build_context.content_ref.content_hash,
            "compiler_profile": compiler_profile,
            "test_suite_version": test_suite_version,
            "compiler_image": compiler_image,
            "compiler_version": compiler_version,
            "compile_flags": ["-std=c++20", "-Werror"],
            "public_tests": public_tests,
            "hidden_tests": hidden_tests,
            "approved_capabilities": approved_capabilities,
            "limits": {"compile_timeout_ms": 10000},
            "parameter_schema": base_parameter_schema,
            "semantic_version_major": 1,
            "semantic_version_minor": 0,
            "runtime_abi_version": runtime_abi_version,
        }
        policy_sha256 = canonical_json_sha256(policy_projection)
        artifact_uri = f"artifact://sha256/{self.skill_ref.artifact_sha256}"
        evidence_ref: dict[str, object] = {
            "evidence_id": "evidence_runtime_build_0001",
            "evidence_type": "TEST_REPORT",
            "created_at": "2026-08-08T12:00:00Z",
            "sha256": "e" * 64,
        }
        versions_wire = _versions_wire(self.versions)
        certification_record: dict[str, object] = {
            "request_context": _request_context_wire(self.build_context),
            "certification_id": CERTIFICATION_ID,
            "build_id": BUILD_ID,
            "command_id": BUILD_COMMAND_ID,
            "skill_id": SKILL_ID,
            "skill_version_id": SKILL_VERSION_ID,
            "learner_id": LEARNER_ID,
            "world_id": WORLD_ID,
            "source_bundle_sha256": source_sha256,
            "build_policy_id": BUILD_POLICY_ID,
            "policy_sha256": policy_sha256,
            "client_draft_revision": client_draft_revision,
            "display_name": "Runtime Closure Skill",
            "parameter_schema": parameter_schema,
            "artifact_sha256": self.skill_ref.artifact_sha256,
            "compiler_profile": compiler_profile,
            "compiler_version": compiler_version,
            "compiler_image": compiler_image,
            "test_suite_version": test_suite_version,
            "semantic_version": semantic_version,
            "runtime_abi_version": runtime_abi_version,
            "tests": expected_tests,
            "requested_capabilities": requested_capabilities,
            "approved_capabilities": approved_capabilities,
            "evidence_ref": evidence_ref,
            "certified_at": "2026-08-08T12:00:00Z",
            "versions": versions_wire,
        }
        artifact = BuildArtifact(
            artifact_sha256=self.skill_ref.artifact_sha256,
            source_sha256=source_sha256,
            compiler_profile=compiler_profile,
            compiler_version=compiler_version,
            sandbox_image_digest=compiler_image,
            test_suite_version=test_suite_version,
            artifact_uri=artifact_uri,
        )
        certification_metadata: dict[str, object] = {
            "build_id": BUILD_ID,
            "client_draft_revision": client_draft_revision,
            "display_name": "Runtime Closure Skill",
            "evidence_id": "evidence_runtime_build_0001",
            "source_bundle_sha256": source_sha256,
            "build_policy_id": BUILD_POLICY_ID,
            "policy_sha256": policy_sha256,
        }
        self.certification = CertifiedSkill(
            certification_id=CERTIFICATION_ID,
            skill_id=SKILL_ID,
            skill_version_id=SKILL_VERSION_ID,
            semantic_version=semantic_version,
            artifact=artifact,
            capabilities=("world.read",),
            certified_at=NOW,
            revoked_at=None,
            metadata=certification_metadata,
        )
        self.skill = SkillSnapshot(
            ref=self.skill_ref,
            source_code=source,
            source_sha256=source_file_sha256,
            entrypoint="main.cpp",
            parameter_schema=parameter_schema,
            request_context=self.build_context,
        )
        self.active = ActiveSkill(self.certification, 1, NOW)
        build_resource: dict[str, object] = {
            "request_context": _request_context_wire(self.build_context),
            "build_id": BUILD_ID,
            "skill_id": SKILL_ID,
            "skill_version_id": SKILL_VERSION_ID,
            "status": "CERTIFIED",
            "terminal": True,
            "artifact": {
                "artifact_sha256": self.skill_ref.artifact_sha256,
                "source_sha256": source_sha256,
                "compiler_profile": compiler_profile,
                "compiler_version": compiler_version,
                "test_suite_version": test_suite_version,
            },
            "certification": {
                "certification_id": CERTIFICATION_ID,
                "issued_at": "2026-08-08T12:00:00Z",
                "capabilities": requested_capabilities,
            },
            "versions": versions_wire,
        }
        artifact_metadata: dict[str, object] = {
            "artifact_sha256": self.skill_ref.artifact_sha256,
            "artifact_uri": artifact_uri,
            "size_bytes": 4096,
            "source_sha256": source_sha256,
            "build_policy_id": BUILD_POLICY_ID,
            "policy_sha256": policy_sha256,
            "compiler_profile": compiler_profile,
            "compiler_version": compiler_version,
            "compiler_image": compiler_image,
            "test_suite_version": test_suite_version,
            "build_identity": "build-identity-runtime-0001",
        }
        self.row: dict[str, object] = {
            "snapshot_json": _encoded(self.skill),
            "active_json": _plain_mapping(self.active),
            "entry_sha256": canonical_json_sha256(_plain_mapping(self.active)),
            "active_revision": 1,
            "active_activated_at": NOW,
            "certification_json": _encoded(self.certification),
            "agent_profile_id": "profile_runtime_closure_0001",
            "public_world_id": WORLD_ID,
            "public_learner_id": LEARNER_ID,
            "certification_actor_id": self.build_context.actor.actor_id,
            "certification_content_hash": self.build_context.content_ref.content_hash,
            "build_id": BUILD_ID,
            "certification_sha256": canonical_json_sha256(certification_record),
            "full_certification_json": certification_record,
            "issued_at": NOW,
            "build_command_id": BUILD_COMMAND_ID,
            "build_status": "CERTIFIED",
            "build_terminal": True,
            "build_json": build_resource,
            "build_resource_sha256": canonical_json_sha256(build_resource),
            "source_bundle_json": source_bundle,
            "source_bundle_sha256": source_sha256,
            "build_policy_id": BUILD_POLICY_ID,
            "client_draft_revision": client_draft_revision,
            "build_compiler_profile": compiler_profile,
            "build_test_suite_version": test_suite_version,
            "requested_capabilities_json": requested_capabilities,
            "policy_compiler_profile": compiler_profile,
            "policy_test_suite_version": test_suite_version,
            "compiler_image": compiler_image,
            "compiler_version": compiler_version,
            "compile_flags_json": policy_projection["compile_flags"],
            "public_tests_json": public_tests,
            "hidden_tests_json": hidden_tests,
            "approved_capabilities_json": approved_capabilities,
            "limits_json": policy_projection["limits"],
            "parameter_schema_json": base_parameter_schema,
            "semantic_version_major": 1,
            "semantic_version_minor": 0,
            "runtime_abi_version": runtime_abi_version,
            "policy_sha256": policy_sha256,
            "artifact_source_sha256": source_sha256,
            "artifact_uri": artifact_uri,
            "artifact_metadata_json": artifact_metadata,
        }


async def _validate(
    fixture: _ClosureFixture,
    connection: _RepositoryConnection,
) -> None:
    await AgentTurnApplication._validate_active_skill(
        cast(AsyncConnection[dict[str, object]], connection),
        fixture.session,
        fixture.skill_ref,
        fixture.turn_context,
    )


class RuntimeCertificationClosureTests(unittest.TestCase):
    def test_exact_public_closure_binds_the_session_once(self) -> None:
        fixture = _ClosureFixture()
        connection = _RepositoryConnection(scoped_row=fixture.row)

        asyncio.run(_validate(fixture, connection))

        self.assertEqual(connection.binding_writes, 1)
        self.assertIsNotNone(connection.binding)
        assert connection.binding is not None
        self.assertEqual(connection.binding["certification_id"], CERTIFICATION_ID)
        self.assertEqual(connection.binding["artifact_sha256"], fixture.skill_ref.artifact_sha256)

        non_utc_row = copy.deepcopy(fixture.row)
        non_utc_row["active_activated_at"] = NOW.astimezone(timezone(timedelta(hours=8)))
        non_utc_connection = _RepositoryConnection(scoped_row=non_utc_row)
        asyncio.run(_validate(fixture, non_utc_connection))
        self.assertEqual(non_utc_connection.binding_writes, 1)

    def test_public_authority_drift_fails_before_session_binding_write(self) -> None:
        fixture = _ClosureFixture()

        def certification_hash(row: dict[str, object]) -> None:
            row["certification_sha256"] = "0" * 64

        def certification_identity(row: dict[str, object]) -> None:
            record = cast(dict[str, object], row["full_certification_json"])
            record["certification_id"] = "cert_runtime_forged_0001"
            row["certification_sha256"] = canonical_json_sha256(record)

        def terminal_build(row: dict[str, object]) -> None:
            row["build_terminal"] = False

        def source_bundle(row: dict[str, object]) -> None:
            bundle = cast(dict[str, object], row["source_bundle_json"])
            files = cast(list[object], bundle["files"])
            source_file = cast(dict[str, object], files[0])
            source_file["content"] = "int main(){ return 7; }\n"
            source_file["content_sha256"] = hashlib.sha256(
                cast(str, source_file["content"]).encode("utf-8")
            ).hexdigest()

        def policy(row: dict[str, object]) -> None:
            row["compiler_version"] = "forged-compiler"

        def artifact(row: dict[str, object]) -> None:
            metadata = cast(dict[str, object], row["artifact_metadata_json"])
            metadata["compiler_image"] = f"gcc@sha256:{'f' * 64}"

        mutations = {
            "canonical certification hash": certification_hash,
            "canonical but wrong certification identity": certification_identity,
            "terminal Build": terminal_build,
            "source bundle": source_bundle,
            "policy": policy,
            "Artifact metadata": artifact,
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                row = copy.deepcopy(fixture.row)
                mutate(row)
                connection = _RepositoryConnection(scoped_row=row)
                with self.assertRaises(BackendApplicationError) as raised:
                    asyncio.run(_validate(fixture, connection))
                self.assertEqual(raised.exception.code, "SKILL_VERSION_MISMATCH")
                self.assertEqual(connection.binding_writes, 0)
                self.assertIsNone(connection.binding)

    def test_pre_a8_session_keeps_explicit_legacy_fallback(self) -> None:
        fixture = _ClosureFixture()
        legacy_row: dict[str, object] = {
            "snapshot_json": _encoded(fixture.skill),
            "active_json": _encoded(fixture.active),
            "active_revision": 1,
            "certification_json": _encoded(fixture.certification),
        }
        connection = _RepositoryConnection(
            scoped_row=None,
            public_session=False,
            legacy_row=legacy_row,
        )

        asyncio.run(_validate(fixture, connection))

        self.assertEqual(connection.binding_writes, 0)
        self.assertIsNone(connection.binding)

    def test_real_postgres_public_session_activation_and_turn_acceptance(self) -> None:
        from test_agent_backend_student_skill_chain_surfaces import (
            AGENT_PROFILE_ID,
            StudentSkillChainSurfaceTests,
            _seed_pre_certified_skill_closure,
        )
        from test_agent_backend_student_skill_chain_surfaces import (
            CERTIFICATION_ID as DATABASE_CERTIFICATION_ID,
        )
        from test_agent_backend_student_skill_chain_surfaces import (
            SKILL_ID as DATABASE_SKILL_ID,
        )
        from test_agent_backend_student_skill_chain_surfaces import (
            SKILL_VERSION_ID as DATABASE_SKILL_VERSION_ID,
        )
        from test_agent_backend_student_skill_chain_surfaces import (
            WORLD_ID as DATABASE_WORLD_ID,
        )

        case_type = StudentSkillChainSurfaceTests
        case_type.setUpClass()
        case = case_type("test_activation_http_replay_worker_get_and_registry_cas")

        async def scenario() -> None:
            await case.asyncSetUp()
            try:
                session_response, session_receipt, _ = await case._post(
                    "/v1/agent-sessions",
                    case.authority.session_request,
                    suffix="runtime_closure_session_0001",
                    idempotency_key="runtime-closure-session-0001",
                )
                self.assertEqual(session_response.status, 202)
                self.assertTrue(await case.worker.run_once())
                session_job = await case._job_row(cast(str, session_receipt["command_id"]))
                session_id = cast(str, session_job["resource_id"])

                fixture = await _seed_pre_certified_skill_closure(
                    case.database,
                    case.validator,
                    case.authority,
                    case.artifact_root,
                )
                activation_body: dict[str, object] = {
                    "expected_registry_revision": 0,
                    "activation_scope": {
                        "world_id": DATABASE_WORLD_ID,
                        "agent_profile_id": AGENT_PROFILE_ID,
                    },
                    "reason": "Exercise exact runtime Certification closure.",
                }
                activation_response, activation_receipt, _ = await case._post(
                    f"/v1/skill-versions/{DATABASE_SKILL_VERSION_ID}/activations",
                    activation_body,
                    suffix="runtime_closure_activation_0001",
                    idempotency_key="runtime-closure-activation-0001",
                )
                self.assertEqual(activation_response.status, 202)
                self.assertTrue(await case.worker.run_once())
                activation_job = await case._job_row(cast(str, activation_receipt["command_id"]))
                self.assertEqual(activation_job["state"], "SUCCEEDED")

                turn_body: dict[str, object] = {
                    "turn_id": "turn_runtime_closure_0001",
                    "expected_world_revision": 5,
                    "input": {
                        "type": "MESSAGE",
                        "text": "Use the exact certified runtime Skill.",
                        "locale": "en-US",
                    },
                    "skill_bindings": [
                        {
                            "skill_id": DATABASE_SKILL_ID,
                            "skill_version_id": DATABASE_SKILL_VERSION_ID,
                            "artifact_sha256": fixture.artifact_sha256,
                            "certification_id": DATABASE_CERTIFICATION_ID,
                        }
                    ],
                    "client_state": {
                        "last_event_sequence": 40,
                        "client_turn_sequence": 1,
                    },
                }
                turn_response, turn_receipt, _ = await case._post(
                    f"/v1/agent-sessions/{session_id}/turns",
                    turn_body,
                    suffix="runtime_closure_turn_0001",
                    idempotency_key="runtime-closure-turn-0001",
                )
                self.assertEqual(turn_response.status, 202, turn_response.body)

                context_connection = await case.database.connect(autocommit=True)
                try:
                    context_cursor = await context_connection.execute(
                        """
                        SELECT operation_context_json FROM yaya_command_jobs
                        WHERE tenant_id=%s AND command_id=%s
                        """,
                        (
                            case.context.actor.tenant_id,
                            cast(str, turn_receipt["command_id"]),
                        ),
                    )
                    context_row = await context_cursor.fetchone()
                finally:
                    await context_connection.close()
                if context_row is None:
                    self.fail("accepted Turn operation context disappeared")
                turn_context = decode_as(context_row["operation_context_json"], OperationContext)
                non_utc_database = _ConnectionTimeZoneDatabase(
                    case.database,
                    "Asia/Shanghai",
                )
                active_skills = await PostgresSkillRepository(
                    cast(Any, non_utc_database)
                ).list_active_skills(case.context.actor.actor_id, turn_context)
                expected_skill_ref = SkillRef(
                    skill_id=DATABASE_SKILL_ID,
                    skill_version_id=DATABASE_SKILL_VERSION_ID,
                    artifact_sha256=fixture.artifact_sha256,
                    certification_id=DATABASE_CERTIFICATION_ID,
                )
                self.assertEqual(tuple(skill.ref for skill in active_skills), (expected_skill_ref,))
                bound_skill = await PostgresSkillRepository(
                    cast(Any, non_utc_database)
                ).get_bound_skill(expected_skill_ref, turn_context)
                self.assertEqual(bound_skill.ref, expected_skill_ref)

                invocation_arguments: dict[str, object] = {}
                invocation_id = "invocation_runtime_scope_0001"
                invocation_sha256 = skill_invocation_request_sha256(
                    tenant_id=case.context.actor.tenant_id,
                    invocation_id=invocation_id,
                    session_id=session_id,
                    turn_id=cast(str, turn_body["turn_id"]),
                    command_id=cast(str, turn_receipt["command_id"]),
                    world_id=DATABASE_WORLD_ID,
                    expected_world_revision=5,
                    skill_ref=expected_skill_ref,
                    arguments=invocation_arguments,
                )
                invocation_request = SkillInvocationRequest(
                    invocation_id=invocation_id,
                    tenant_id=case.context.actor.tenant_id,
                    session_id=session_id,
                    turn_id=cast(str, turn_body["turn_id"]),
                    command_id=cast(str, turn_receipt["command_id"]),
                    world_id=DATABASE_WORLD_ID,
                    expected_world_revision=5,
                    skill_ref=expected_skill_ref,
                    arguments=invocation_arguments,
                    request_sha256=invocation_sha256,
                )
                invocation_service = PostgresSkillInvocationService(
                    database=case.database,
                    sandbox=cast(Any, None),
                    world_engine=cast(Any, None),
                    world_uow=cast(Any, None),
                    limits=cast(Any, None),
                    versions=make_versions(),
                    contracts_root=CONTRACTS_ROOT,
                )
                invocation_connection = await case.database.connect(autocommit=True)
                try:
                    await invocation_connection.execute("SET TIME ZONE 'Asia/Shanghai'")
                    loaded_skill = await cast(Any, invocation_service)._load_active_skill(
                        invocation_connection,
                        invocation_request,
                        turn_context,
                    )
                finally:
                    await invocation_connection.close()
                self.assertEqual(loaded_skill.ref, expected_skill_ref)

                status_change = await case.database.connect(autocommit=True)
                try:
                    await status_change.execute(
                        """
                        UPDATE yaya_public_agent_sessions SET status='CLOSED'
                        WHERE tenant_id=%s AND session_id=%s
                        """,
                        (case.context.actor.tenant_id, session_id),
                    )
                finally:
                    await status_change.close()
                try:
                    with self.assertRaises(RepositoryAuthorityError):
                        await PostgresSkillRepository(case.database).list_active_skills(
                            case.context.actor.actor_id,
                            turn_context,
                        )
                    invocation_connection = await case.database.connect(autocommit=True)
                    try:
                        with self.assertRaises(AgentToolExecutionError):
                            await cast(Any, invocation_service)._load_active_skill(
                                invocation_connection,
                                invocation_request,
                                turn_context,
                                for_update=True,
                            )
                    finally:
                        await invocation_connection.close()
                finally:
                    status_restore = await case.database.connect(autocommit=True)
                    try:
                        await status_restore.execute("SET session_replication_role = replica")
                        try:
                            await status_restore.execute(
                                """
                                UPDATE yaya_public_agent_sessions SET status='ACTIVE'
                                WHERE tenant_id=%s AND session_id=%s
                                """,
                                (case.context.actor.tenant_id, session_id),
                            )
                        finally:
                            await status_restore.execute("SET session_replication_role = origin")
                    finally:
                        await status_restore.close()

                scope_guard = await case.database.connect(autocommit=True)
                try:
                    with self.assertRaises(ObjectNotInPrerequisiteState) as guarded:
                        await scope_guard.execute(
                            """
                            UPDATE yaya_public_agent_sessions
                            SET agent_profile_id=%s
                            WHERE tenant_id=%s AND session_id=%s
                            """,
                            (
                                "profile_runtime_scope_drift_0001",
                                case.context.actor.tenant_id,
                                session_id,
                            ),
                        )
                    self.assertIn("scope is immutable", str(guarded.exception))
                finally:
                    await scope_guard.close()

                drifted_world_id = "world_runtime_scope_drift_0001"
                scope_corruption = await case.database.connect(autocommit=True)
                try:
                    await scope_corruption.execute("SET session_replication_role = replica")
                    try:
                        await scope_corruption.execute(
                            """
                            UPDATE yaya_public_agent_sessions SET world_id=%s
                            WHERE tenant_id=%s AND session_id=%s
                            """,
                            (
                                drifted_world_id,
                                case.context.actor.tenant_id,
                                session_id,
                            ),
                        )
                    finally:
                        await scope_corruption.execute("SET session_replication_role = origin")
                finally:
                    await scope_corruption.close()
                try:
                    with self.assertRaises(RepositoryAuthorityError):
                        await PostgresSkillRepository(case.database).list_active_skills(
                            case.context.actor.actor_id,
                            turn_context,
                        )
                    invocation_connection = await case.database.connect(autocommit=True)
                    try:
                        with self.assertRaises(AgentToolExecutionError) as invocation_drift:
                            await cast(Any, invocation_service)._load_active_skill(
                                invocation_connection,
                                invocation_request,
                                turn_context,
                                for_update=True,
                            )
                        self.assertEqual(
                            invocation_drift.exception.code,
                            "TOOL_SKILL_BINDING_MISMATCH",
                        )
                    finally:
                        await invocation_connection.close()
                finally:
                    scope_restore = await case.database.connect(autocommit=True)
                    try:
                        await scope_restore.execute("SET session_replication_role = replica")
                        try:
                            await scope_restore.execute(
                                """
                                UPDATE yaya_public_agent_sessions SET world_id=%s
                                WHERE tenant_id=%s AND session_id=%s
                                """,
                                (
                                    DATABASE_WORLD_ID,
                                    case.context.actor.tenant_id,
                                    session_id,
                                ),
                            )
                        finally:
                            await scope_restore.execute("SET session_replication_role = origin")
                    finally:
                        await scope_restore.close()

                history_repository = PostgresSkillRepository(case.database)
                skill_history = await history_repository.list_skill_history(
                    DATABASE_SKILL_ID,
                    session_id,
                    turn_context,
                )
                self.assertEqual(
                    tuple(item.skill_version_id for item in skill_history),
                    (DATABASE_SKILL_VERSION_ID,),
                )
                binding_corruption = await case.database.connect(autocommit=True)
                try:
                    binding_cursor = await binding_corruption.execute(
                        """
                        SELECT binding_sha256 FROM yaya_session_skill_versions
                        WHERE tenant_id=%s AND session_id=%s AND skill_id=%s
                          AND skill_version_id=%s
                        """,
                        (
                            case.context.actor.tenant_id,
                            session_id,
                            DATABASE_SKILL_ID,
                            DATABASE_SKILL_VERSION_ID,
                        ),
                    )
                    binding_row = await binding_cursor.fetchone()
                    if binding_row is None:
                        self.fail("public Session SkillVersion binding disappeared")
                    original_binding_sha256 = cast(str, binding_row["binding_sha256"])
                    await binding_corruption.execute("SET session_replication_role = replica")
                    try:
                        await binding_corruption.execute(
                            """
                            UPDATE yaya_session_skill_versions SET binding_sha256=%s
                            WHERE tenant_id=%s AND session_id=%s AND skill_id=%s
                              AND skill_version_id=%s
                            """,
                            (
                                "0" * 64,
                                case.context.actor.tenant_id,
                                session_id,
                                DATABASE_SKILL_ID,
                                DATABASE_SKILL_VERSION_ID,
                            ),
                        )
                    finally:
                        await binding_corruption.execute("SET session_replication_role = origin")
                finally:
                    await binding_corruption.close()
                try:
                    with self.assertRaises(RepositoryAuthorityError):
                        await history_repository.list_skill_history(
                            DATABASE_SKILL_ID,
                            session_id,
                            turn_context,
                        )
                    with self.assertRaises(RepositoryAuthorityError):
                        await history_repository.get_bound_skill(
                            expected_skill_ref,
                            turn_context,
                        )
                    invocation_connection = await case.database.connect(autocommit=True)
                    try:
                        with self.assertRaises(AgentToolExecutionError):
                            await cast(Any, invocation_service)._load_active_skill(
                                invocation_connection,
                                invocation_request,
                                turn_context,
                                for_update=True,
                            )
                    finally:
                        await invocation_connection.close()
                finally:
                    binding_restore = await case.database.connect(autocommit=True)
                    try:
                        await binding_restore.execute("SET session_replication_role = replica")
                        try:
                            await binding_restore.execute(
                                """
                                UPDATE yaya_session_skill_versions SET binding_sha256=%s
                                WHERE tenant_id=%s AND session_id=%s AND skill_id=%s
                                  AND skill_version_id=%s
                                """,
                                (
                                    original_binding_sha256,
                                    case.context.actor.tenant_id,
                                    session_id,
                                    DATABASE_SKILL_ID,
                                    DATABASE_SKILL_VERSION_ID,
                                ),
                            )
                        finally:
                            await binding_restore.execute("SET session_replication_role = origin")
                    finally:
                        await binding_restore.close()

                connection = await case.database.connect(autocommit=True)
                try:
                    cursor = await connection.execute(
                        """
                        SELECT
                          (SELECT count(*)::integer
                           FROM yaya_session_skill_versions
                           WHERE tenant_id=%s AND session_id=%s AND skill_id=%s
                             AND skill_version_id=%s) AS bindings,
                          (SELECT count(*)::integer FROM yaya_commands
                           WHERE tenant_id=%s AND operation='EXECUTE_AGENT_TURN') AS turns,
                          (SELECT client_turn_sequence FROM yaya_agent_sessions
                           WHERE tenant_id=%s AND session_id=%s) AS client_turn_sequence
                        """,
                        (
                            case.context.actor.tenant_id,
                            session_id,
                            DATABASE_SKILL_ID,
                            DATABASE_SKILL_VERSION_ID,
                            case.context.actor.tenant_id,
                            case.context.actor.tenant_id,
                            session_id,
                        ),
                    )
                    row = await cursor.fetchone()
                finally:
                    await connection.close()
                self.assertEqual(
                    row,
                    {"bindings": 1, "turns": 1, "client_turn_sequence": 1},
                )

                corruption = await case.database.connect(autocommit=True)
                try:
                    await corruption.execute("SET session_replication_role = replica")
                    try:
                        await corruption.execute(
                            """
                            UPDATE yaya_skill_certifications
                            SET certification_sha256=%s
                            WHERE tenant_id=%s AND certification_id=%s
                            """,
                            (
                                "0" * 64,
                                case.context.actor.tenant_id,
                                DATABASE_CERTIFICATION_ID,
                            ),
                        )
                    finally:
                        await corruption.execute("SET session_replication_role = origin")
                finally:
                    await corruption.close()

                drifted_turn = copy.deepcopy(turn_body)
                drifted_turn["turn_id"] = "turn_runtime_closure_0002"
                drifted_state = cast(dict[str, object], drifted_turn["client_state"])
                drifted_state["client_turn_sequence"] = 2
                rejected, rejected_payload, _ = await case._post(
                    f"/v1/agent-sessions/{session_id}/turns",
                    drifted_turn,
                    suffix="runtime_closure_turn_drift_0002",
                    idempotency_key="runtime-closure-turn-drift-0002",
                )
                self.assertEqual(rejected.status, 409)
                self.assertEqual(
                    cast(dict[str, object], rejected_payload["error"])["code"],
                    "SKILL_VERSION_MISMATCH",
                )

                connection = await case.database.connect(autocommit=True)
                try:
                    cursor = await connection.execute(
                        """
                        SELECT
                          (SELECT count(*)::integer
                           FROM yaya_session_skill_versions
                           WHERE tenant_id=%s AND session_id=%s AND skill_id=%s
                             AND skill_version_id=%s) AS bindings,
                          (SELECT count(*)::integer FROM yaya_commands
                           WHERE tenant_id=%s AND operation='EXECUTE_AGENT_TURN') AS turns,
                          (SELECT client_turn_sequence FROM yaya_agent_sessions
                           WHERE tenant_id=%s AND session_id=%s) AS client_turn_sequence
                        """,
                        (
                            case.context.actor.tenant_id,
                            session_id,
                            DATABASE_SKILL_ID,
                            DATABASE_SKILL_VERSION_ID,
                            case.context.actor.tenant_id,
                            case.context.actor.tenant_id,
                            session_id,
                        ),
                    )
                    after_rejection = await cursor.fetchone()
                finally:
                    await connection.close()
                self.assertEqual(after_rejection, row)
            finally:
                await case.asyncTearDown()

        try:
            asyncio.run(scenario())
        finally:
            case_type.tearDownClass()


if __name__ == "__main__":
    unittest.main()
