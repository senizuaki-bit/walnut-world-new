from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import stat
import subprocess
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
    TASK_ID,
    WORLD_ID,
    make_operation,
    make_task,
    make_versions,
    make_world_state,
)
from postgres_test_support import postgres_test_server  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from yaya_agent_backend.application import HttpAttempt  # noqa: E402
from yaya_agent_backend.codec import decode_as, encode  # noqa: E402
from yaya_agent_backend.database import PostgresDatabase  # noqa: E402
from yaya_agent_backend.skill_builds import PostgresSkillBuildExecutor  # noqa: E402
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
    ContentRef,
    OperationContext,
    RequestContext,
    VersionSet,
    canonical_json_sha256,
)
from yaya_agent_runtime import CompileResultSnapshot  # noqa: E402

PINNED_GCC_IMAGE = "gcc@sha256:b99b86a28812b1e6453a231a947dc43d76fe192788a12f344a9b568bf9f5d24c"
PINNED_GCC_VERSION = "14.2.0"
LEARNER_ID = "learner_build_e2e_0001"
AGENT_PROFILE_ID = "agent_profile_build_e2e_0001"
AUTHORITY_ID = "authority_build_e2e_0001"
BUILD_POLICY_ID = "build_policy_e2e_0001"
TEST_SUITE_VERSION = "build-e2e-1"
RUNTIME_ABI_VERSION = "yaya-skill-json-stdio-v1"


def _json_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _content_wire(content: ContentRef) -> dict[str, object]:
    return {
        "unit_id": content.unit_id,
        "version": content.version,
        "content_hash": content.content_hash,
    }


def _request_context(context: OperationContext) -> RequestContext:
    return RequestContext(
        request_id=context.request_id,
        trace_id=context.trace_id,
        correlation_id=context.correlation_id,
        requested_at=context.requested_at,
        actor=context.actor,
        content_ref=context.content_ref,
        schema_version=context.schema_version,
    )


def _stdout_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True, slots=True)
class _AuthorityFixture:
    context: OperationContext
    versions: VersionSet
    policy: dict[str, object]


async def _seed_only_build_authority(
    database: PostgresDatabase,
    *,
    context_override: OperationContext | None = None,
    versions_override: VersionSet | None = None,
    public_tests_override: list[dict[str, object]] | None = None,
    hidden_tests_override: list[dict[str, object]] | None = None,
    parameter_schema_override: dict[str, object] | None = None,
) -> _AuthorityFixture:
    """Seed exactly one Task/World/Learner/Profile/launch/build-policy authority graph."""

    context = context_override or make_operation(command_id="cmd_build_authority_seed_0001")
    versions = versions_override or replace(make_versions(), sandbox_image_digest=PINNED_GCC_IMAGE)
    task = make_task(context)
    state = make_world_state()
    learner: dict[str, object] = {
        "learner_id": LEARNER_ID,
        "actor_id": context.actor.actor_id,
        "content": _content_wire(context.content_ref),
        "revision": 0,
    }
    profile: dict[str, object] = {
        "agent_profile_id": AGENT_PROFILE_ID,
        "actor_id": context.actor.actor_id,
        "content": _content_wire(context.content_ref),
        "role": "farmer_build_tutor",
        "revision": 1,
    }
    raw_versions = encode(versions)
    if not isinstance(raw_versions, dict):
        raise AssertionError("VersionSet did not encode as an object")
    versions_json = cast(dict[str, object], raw_versions)
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

    public_stdin = b"public-input\n"
    hidden_stdin = b"hidden-input\n"
    public_tests: list[dict[str, object]] = [
        {
            "test_case_id": "public_exact_io_0001",
            "visibility": "PUBLIC",
            "arguments": ["public-argument"],
            "stdin_base64": base64.b64encode(public_stdin).decode("ascii"),
            "expected_stdout_sha256": _stdout_sha256(b"public-argument:public-input\n"),
        }
    ]
    hidden_tests: list[dict[str, object]] = [
        {
            "test_case_id": "hidden_exact_io_0001",
            "visibility": "HIDDEN",
            "arguments": ["hidden-argument"],
            "stdin_base64": base64.b64encode(hidden_stdin).decode("ascii"),
            "expected_stdout_sha256": _stdout_sha256(b"hidden-argument:hidden-input\n"),
        }
    ]
    if public_tests_override is not None:
        public_tests = [dict(item) for item in public_tests_override]
    if hidden_tests_override is not None:
        hidden_tests = [dict(item) for item in hidden_tests_override]
    limits: dict[str, object] = {
        "compile_wall_ms": 120_000,
        "test_wall_ms": 15_000,
        "memory_bytes": 536_870_912,
        "max_processes": 64,
        "cpu_millis": 1000,
        "tmpfs_bytes": 67_108_864,
        "max_output_bytes": 65_536,
        "max_artifact_bytes": 16_777_216,
    }
    parameter_schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["plot_count"],
        "properties": {"plot_count": {"type": "integer", "minimum": 1, "maximum": 8}},
    }
    if parameter_schema_override is not None:
        parameter_schema = dict(parameter_schema_override)
    policy: dict[str, object] = {
        "build_policy_id": BUILD_POLICY_ID,
        "actor_id": context.actor.actor_id,
        "content_hash": context.content_ref.content_hash,
        "compiler_profile": CPP20_SAFE_V1_PROFILE,
        "test_suite_version": TEST_SUITE_VERSION,
        "compiler_image": PINNED_GCC_IMAGE,
        "compiler_version": PINNED_GCC_VERSION,
        "compile_flags": list(CPP20_SAFE_V1_FLAGS),
        "public_tests": public_tests,
        "hidden_tests": hidden_tests,
        "approved_capabilities": ["WATER", "WORLD_READ"],
        "limits": limits,
        "parameter_schema": parameter_schema,
        "semantic_version_major": 1,
        "semantic_version_minor": 2,
        "runtime_abi_version": RUNTIME_ABI_VERSION,
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
            ) VALUES (%s,%s,%s,%s,%s,5,0,%s,%s,%s,%s)
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
                canonical_json_sha256(learner),
                Jsonb(learner),
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
                canonical_json_sha256(profile),
                Jsonb(profile),
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
                TEST_SUITE_VERSION,
                PINNED_GCC_IMAGE,
                PINNED_GCC_VERSION,
                Jsonb(policy["compile_flags"]),
                Jsonb(public_tests),
                Jsonb(hidden_tests),
                Jsonb(policy["approved_capabilities"]),
                Jsonb(limits),
                Jsonb(parameter_schema),
                policy["semantic_version_major"],
                policy["semantic_version_minor"],
                RUNTIME_ABI_VERSION,
                canonical_json_sha256(policy),
            ),
        )
    return _AuthorityFixture(context, versions, policy)


class PostgresSkillBuildExecutorE2ETests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            subprocess.run(
                ["docker", "version"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            subprocess.run(
                ["docker", "image", "inspect", PINNED_GCC_IMAGE],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.SubprocessError) as error:
            raise AssertionError(
                "real digest-pinned GCC Docker dependency is unavailable"
            ) from error
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
                    yaya_session_skill_versions,yaya_certification_revocations,
                    yaya_skill_certifications,yaya_artifacts,yaya_build_step_receipts,
                    yaya_skill_build_history,yaya_skill_builds,yaya_build_policies,
                    yaya_control_jobs,yaya_public_agent_sessions,yaya_launch_authorities,
                    yaya_agent_profiles,yaya_learners,yaya_registry_certifications,
                    yaya_skills,yaya_compile_results,yaya_evidence,yaya_commands,
                    yaya_agent_sessions,yaya_worlds,yaya_tasks CASCADE
                """
            )
        finally:
            await connection.close()
        self.authority = await _seed_only_build_authority(self.database)
        self.validator = ContractSchemaValidator(CONTRACTS_ROOT)
        self._artifact_directory = tempfile.TemporaryDirectory(prefix="yaya-build-e2e-artifacts-")
        self._workspace_directory = tempfile.TemporaryDirectory(prefix="yaya-build-e2e-workspaces-")
        self.artifact_root = Path(self._artifact_directory.name).resolve()
        self.workspace_root = Path(self._workspace_directory.name).resolve()
        self.application = StudentSkillChainApplication(
            self.database,
            self.validator,
            self.authority.versions,
            artifact_root=self.artifact_root,
        )
        self.executor = PostgresSkillBuildExecutor(
            database=self.database,
            validator=self.validator,
            artifact_root=self.artifact_root,
            workspace_root=self.workspace_root,
            runtime_image=PINNED_GCC_IMAGE,
        )
        self.worker = StudentSkillChainWorker(
            database=self.database,
            application=self.application,
            validator=self.validator,
            worker_id="real-build-e2e-worker-0001",
            artifact_root=self.artifact_root,
            build_executor=self.executor,
        )

    async def asyncTearDown(self) -> None:
        for root in (self.artifact_root, self.workspace_root):
            for candidate in root.rglob("*"):
                if candidate.is_file() and not candidate.is_symlink():
                    candidate.chmod(stat.S_IWRITE | stat.S_IREAD)
        self._workspace_directory.cleanup()
        self._artifact_directory.cleanup()

    def _attempt(self, suffix: str) -> HttpAttempt:
        return HttpAttempt(
            request_id=f"req_build_e2e_{suffix}",
            trace_id=f"trace_build_e2e_{suffix}",
            correlation_id=f"corr_build_e2e_{suffix}",
            requested_at=datetime.now(UTC),
        )

    @staticmethod
    def _bundle(source: str) -> dict[str, object]:
        return {
            "language": "CPP20",
            "entrypoint": "main.cpp",
            "files": [
                {
                    "path": "main.cpp",
                    "content": source,
                    "content_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                }
            ],
        }

    def _request(self, source: str, *, skill_id: str) -> dict[str, object]:
        return {
            "skill_id": skill_id,
            "display_name": "Pinned Docker Build E2E",
            "client_draft_revision": 7,
            "source_bundle": self._bundle(source),
            "compiler_profile": CPP20_SAFE_V1_PROFILE,
            "test_suite_version": TEST_SUITE_VERSION,
            "requested_capabilities": ["WORLD_READ"],
        }

    async def _accept(
        self,
        body: dict[str, object],
        *,
        suffix: str,
        idempotency_key: str,
    ) -> tuple[str, bytes]:
        raw_body = _json_bytes(body)
        accepted = await self.application.accept_build(
            actor=self.authority.context.actor,
            attempt=self._attempt(suffix),
            idempotency_key=idempotency_key,
            raw_body=raw_body,
            body=body,
        )
        self.assertFalse(accepted.replayed)
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT resource_id FROM yaya_control_jobs
                WHERE tenant_id=%s AND command_id=%s
                """,
                (self.authority.context.actor.tenant_id, accepted.command.command_id),
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        self.assertIsNotNone(row)
        return cast(str, cast(dict[str, object], row)["resource_id"]), raw_body

    async def _closure_counts(self) -> dict[str, int]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM yaya_artifacts)::integer AS artifacts,
                  (SELECT count(*) FROM yaya_skills)::integer AS skill_versions,
                  (SELECT count(*) FROM yaya_skill_certifications)::integer AS certifications,
                  (SELECT count(*) FROM yaya_registry_certifications)::integer AS legacy_certifications,
                  (SELECT count(*) FROM yaya_compile_results)::integer AS compile_results,
                  (SELECT count(*) FROM yaya_evidence WHERE evidence_type='TEST_REPORT')::integer
                    AS test_report_evidence
                """
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            raise AssertionError("closure count query returned no row")
        return {key: cast(int, value) for key, value in row.items()}

    async def test_real_pinned_build_certifies_once_and_closes_every_hash(self) -> None:
        source = """#include <iostream>
#include <string>

int main(int argc, char* argv[]) {
    if (argc != 2) {
        return 2;
    }
    std::string input;
    if (!std::getline(std::cin, input)) {
        return 3;
    }
    std::cout << argv[1] << ':' << input << '\\n';
    return 0;
}
"""
        body = self._request(source, skill_id="skill_build_success_0001")
        build_id, raw_body = await self._accept(
            body,
            suffix="success_0001",
            idempotency_key="idem-build-success-0001",
        )

        replay_before = await self.application.accept_build(
            actor=self.authority.context.actor,
            attempt=self._attempt("success_replay_before_0001"),
            idempotency_key="idem-build-success-0001",
            raw_body=raw_body,
            body=body,
        )
        self.assertTrue(replay_before.replayed)
        self.assertTrue(await self.worker.run_once())
        resource = (
            await self.application.get_build(build_id, self.authority.context.actor)
        ).payload
        self.assertEqual(resource["status"], "CERTIFIED", resource)
        self.assertIs(resource["terminal"], True)
        self.assertIsNone(resource["failure"])
        self.assertEqual(
            [phase["status"] for phase in cast(list[dict[str, object]], resource["phases"])],
            ["PASSED"] * 5,
        )
        self.assertEqual(
            await self._closure_counts(),
            {
                "artifacts": 1,
                "skill_versions": 1,
                "certifications": 1,
                "legacy_certifications": 1,
                "compile_results": 1,
                "test_report_evidence": 1,
            },
        )

        artifact = cast(dict[str, object], resource["artifact"])
        certification = cast(dict[str, object], resource["certification"])
        evidence_refs = cast(list[dict[str, object]], resource["evidence_refs"])
        artifact_sha256 = cast(str, artifact["artifact_sha256"])
        artifact_path = self.artifact_root / artifact_sha256[:2] / artifact_sha256
        self.assertTrue(artifact_path.is_file())
        self.assertFalse(artifact_path.is_symlink())
        self.assertEqual(hashlib.sha256(artifact_path.read_bytes()).hexdigest(), artifact_sha256)
        self.assertEqual(artifact_path.stat().st_mode & 0o222, 0)
        self.assertEqual(
            artifact["source_sha256"],
            canonical_source_bundle_sha256(cast(dict[str, object], body["source_bundle"])),
        )
        self.assertEqual(len(evidence_refs), 1)
        self.assertEqual(evidence_refs[0]["evidence_type"], "TEST_REPORT")

        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT b.resource_sha256,b.resource_json,b.status,b.terminal,
                       c.certification_sha256,c.record_json AS certification_json,
                       e.payload_sha256,e.evidence_json,cr.snapshot_json AS compile_json,
                       s.snapshot_json AS skill_json,lc.record_json AS legacy_json,
                       j.state AS job_state,j.attempt,cmd.status AS command_status,
                       (SELECT count(*) FROM yaya_build_step_receipts r
                         WHERE r.tenant_id=b.tenant_id AND r.build_id=b.build_id) AS receipt_count
                FROM yaya_skill_builds b
                JOIN yaya_skill_certifications c
                  ON c.tenant_id=b.tenant_id AND c.build_id=b.build_id
                JOIN yaya_evidence e
                  ON e.tenant_id=b.tenant_id
                 AND e.evidence_id=c.record_json #>> '{evidence_ref,evidence_id}'
                JOIN yaya_compile_results cr
                  ON cr.tenant_id=b.tenant_id AND cr.build_id=b.build_id
                JOIN yaya_skills s
                  ON s.tenant_id=b.tenant_id AND s.skill_version_id=c.skill_version_id
                JOIN yaya_registry_certifications lc
                  ON lc.tenant_id=b.tenant_id AND lc.certification_id=c.certification_id
                JOIN yaya_control_jobs j
                  ON j.tenant_id=b.tenant_id AND j.command_id=b.command_id
                JOIN yaya_commands cmd
                  ON cmd.tenant_id=b.tenant_id AND cmd.command_id=b.command_id
                WHERE b.tenant_id=%s AND b.build_id=%s
                """,
                (self.authority.context.actor.tenant_id, build_id),
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        self.assertIsNotNone(row)
        closure = cast(dict[str, object], row)
        self.assertEqual(closure["status"], "CERTIFIED")
        self.assertIs(closure["terminal"], True)
        stored_resource = cast(dict[str, object], closure["resource_json"])
        certification_record = cast(dict[str, object], closure["certification_json"])
        self.assertEqual(canonical_json_sha256(stored_resource), closure["resource_sha256"])
        self.assertEqual(
            canonical_json_sha256(certification_record),
            closure["certification_sha256"],
        )
        evidence_document = cast(dict[str, object], closure["evidence_json"])
        evidence_payload = cast(dict[str, object], evidence_document["payload"])
        self.assertEqual(
            canonical_json_sha256(evidence_payload),
            closure["payload_sha256"],
        )
        self.assertEqual(
            certification_record["certification_id"], certification["certification_id"]
        )
        self.assertEqual(certification_record["compiler_image"], PINNED_GCC_IMAGE)
        self.assertEqual(certification_record["runtime_abi_version"], RUNTIME_ABI_VERSION)
        certification_tests = cast(list[dict[str, object]], certification_record["tests"])
        self.assertEqual(
            [(item["visibility"], item["status"]) for item in certification_tests],
            [("PUBLIC", "PASSED"), ("HIDDEN", "PASSED")],
        )
        compile_result = decode_as(closure["compile_json"], CompileResultSnapshot)
        self.assertTrue(compile_result.succeeded)
        self.assertEqual(closure["job_state"], "SUCCEEDED")
        self.assertEqual(closure["command_status"], "APPLIED")
        self.assertEqual(closure["attempt"], 1)
        self.assertEqual(closure["receipt_count"], 5)

        replay_after = await self.application.accept_build(
            actor=self.authority.context.actor,
            attempt=self._attempt("success_replay_after_0001"),
            idempotency_key="idem-build-success-0001",
            raw_body=raw_body,
            body=body,
        )
        self.assertTrue(replay_after.replayed)
        restarted_worker = StudentSkillChainWorker(
            database=self.database,
            application=self.application,
            validator=self.validator,
            worker_id="real-build-e2e-restarted-worker-0001",
            artifact_root=self.artifact_root,
            build_executor=PostgresSkillBuildExecutor(
                database=self.database,
                validator=self.validator,
                artifact_root=self.artifact_root,
                workspace_root=self.workspace_root,
                runtime_image=PINNED_GCC_IMAGE,
            ),
        )
        self.assertFalse(await restarted_worker.run_once())
        self.assertEqual((await self._closure_counts())["artifacts"], 1)

    async def test_real_compile_rejection_creates_no_certification_authority(self) -> None:
        source = "int main() { return missing_symbol; }\n"
        body = self._request(source, skill_id="skill_build_rejected_0001")
        build_id, _ = await self._accept(
            body,
            suffix="rejected_0001",
            idempotency_key="idem-build-rejected-0001",
        )

        self.assertTrue(await self.worker.run_once())
        resource = (
            await self.application.get_build(build_id, self.authority.context.actor)
        ).payload
        self.assertEqual(resource["status"], "REJECTED", resource)
        self.assertIs(resource["terminal"], True)
        self.assertIsNone(resource["artifact"])
        self.assertIsNone(resource["certification"])
        self.assertIsNone(resource["skill_version_id"])
        self.assertEqual(resource["evidence_refs"], [])
        failure = cast(dict[str, object], resource["failure"])
        self.assertEqual(failure["code"], "SANDBOX_COMPILE_ERROR")
        self.assertEqual(failure["stage"], "COMPILE")
        self.assertEqual(
            await self._closure_counts(),
            {
                "artifacts": 0,
                "skill_versions": 0,
                "certifications": 0,
                "legacy_certifications": 0,
                "compile_results": 0,
                "test_report_evidence": 0,
            },
        )

        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT j.state AS job_state,cmd.status AS command_status,
                       array_agg(r.step ORDER BY array_position(
                         ARRAY['VALIDATE_SOURCE','COMPILE','PUBLIC_TEST','HIDDEN_TEST','CERTIFY'],
                         r.step
                       )) AS receipt_steps,
                       array_agg(r.outcome ORDER BY array_position(
                         ARRAY['VALIDATE_SOURCE','COMPILE','PUBLIC_TEST','HIDDEN_TEST','CERTIFY'],
                         r.step
                       )) AS receipt_outcomes,
                       b.resource_sha256,b.resource_json
                FROM yaya_skill_builds b
                JOIN yaya_control_jobs j
                  ON j.tenant_id=b.tenant_id AND j.command_id=b.command_id
                JOIN yaya_commands cmd
                  ON cmd.tenant_id=b.tenant_id AND cmd.command_id=b.command_id
                JOIN yaya_build_step_receipts r
                  ON r.tenant_id=b.tenant_id AND r.build_id=b.build_id
                WHERE b.tenant_id=%s AND b.build_id=%s
                GROUP BY j.state,cmd.status,b.resource_sha256,b.resource_json
                """,
                (self.authority.context.actor.tenant_id, build_id),
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        self.assertIsNotNone(row)
        rejection = cast(dict[str, object], row)
        self.assertEqual(rejection["job_state"], "SUCCEEDED")
        self.assertEqual(rejection["command_status"], "APPLIED")
        self.assertEqual(rejection["receipt_steps"], ["VALIDATE_SOURCE", "COMPILE"])
        self.assertEqual(rejection["receipt_outcomes"], ["PASSED", "FAILED"])
        rejection_resource = cast(dict[str, object], rejection["resource_json"])
        self.assertEqual(
            canonical_json_sha256(rejection_resource),
            rejection["resource_sha256"],
        )


if __name__ == "__main__":
    unittest.main()
