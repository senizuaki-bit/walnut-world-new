from __future__ import annotations

import asyncio
import hashlib
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
CONTRACTS_ROOT = Path(__file__).resolve().parents[1] / "contracts"
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from a8_state_fingerprint import (  # noqa: E402
    A8_FAILED_BUILD_MUTATION_TABLES,
    A8StateFingerprint,
    a8_state_fingerprint,
    fingerprint_without,
    missing_a8_business_tables,
)
from postgres_test_support import postgres_test_server  # noqa: E402
from psycopg import AsyncConnection  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from test_agent_backend_skill_build_executor import (  # noqa: E402
    PINNED_GCC_IMAGE,
    PINNED_GCC_VERSION,
    TEST_SUITE_VERSION,
    _AuthorityFixture,  # pyright: ignore[reportPrivateUsage]
    _seed_only_build_authority,  # pyright: ignore[reportPrivateUsage]
)
from yaya_agent_backend.application import BackendApplicationError, HttpAttempt  # noqa: E402
from yaya_agent_backend.database import (  # noqa: E402
    PostgresCommitStateUnknown,
    PostgresDatabase,
)
from yaya_agent_backend.skill_builds import PostgresSkillBuildExecutor  # noqa: E402
from yaya_agent_backend.student_skill_chain import (  # noqa: E402
    BuildJobClaim,
    StudentSkillChainApplication,
    StudentSkillChainWorker,
)
from yaya_agent_backend.wire import ContractSchemaValidator  # noqa: E402
from yaya_agent_build import (  # noqa: E402
    CPP20_SAFE_V1_PROFILE,
    BuildDiagnostic,
    DigestPinnedDockerCppBuilder,
    DockerBuildFailure,
    DockerBuildResult,
    DockerTestResult,
    validate_source_bundle,
)
from yaya_agent_contracts import CompileAndTestRequest, canonical_json_sha256  # noqa: E402


class _PostCommitUnknownDatabase(PostgresDatabase):
    """Commit normally, then lose one configured COMMIT acknowledgement."""

    def __init__(self, dsn: str, *, fail_on_commit: int) -> None:
        super().__init__(dsn)
        self._fail_on_commit = fail_on_commit
        self.commit_count = 0
        self.did_fail = False

    @asynccontextmanager
    async def transaction_with_commit_boundary(
        self,
    ) -> AsyncGenerator[AsyncConnection[dict[str, object]]]:
        self.commit_count += 1
        current = self.commit_count
        async with super().transaction_with_commit_boundary() as connection:
            yield connection
        if current == self._fail_on_commit and not self.did_fail:
            self.did_fail = True
            raise PostgresCommitStateUnknown("injected lost terminal COMMIT acknowledgement")


def _json_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class PostgresSkillBuildFailureMatrixTests(unittest.IsolatedAsyncioTestCase):
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
        self.authority: _AuthorityFixture = await _seed_only_build_authority(self.database)
        self.validator = ContractSchemaValidator(CONTRACTS_ROOT)
        self._artifact_directory = tempfile.TemporaryDirectory(
            prefix="yaya-build-failure-artifacts-"
        )
        self._workspace_directory = tempfile.TemporaryDirectory(
            prefix="yaya-build-failure-workspaces-"
        )
        self.artifact_root = Path(self._artifact_directory.name).resolve()
        self.workspace_root = Path(self._workspace_directory.name).resolve()
        self.application = StudentSkillChainApplication(
            self.database,
            self.validator,
            self.authority.versions,
            artifact_root=self.artifact_root,
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
            request_id=f"req_build_failure_{suffix}",
            trace_id=f"trace_build_failure_{suffix}",
            correlation_id=f"corr_build_failure_{suffix}",
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
            "display_name": "Build failure matrix",
            "client_draft_revision": 11,
            "source_bundle": self._bundle(source),
            "compiler_profile": CPP20_SAFE_V1_PROFILE,
            "test_suite_version": TEST_SUITE_VERSION,
            "requested_capabilities": ["WORLD_READ"],
        }

    def _executor(
        self,
        *,
        database: PostgresDatabase | None = None,
        runtime_image: str = PINNED_GCC_IMAGE,
        docker_executable: str = "docker",
    ) -> PostgresSkillBuildExecutor:
        return PostgresSkillBuildExecutor(
            database=self.database if database is None else database,
            validator=self.validator,
            artifact_root=self.artifact_root,
            workspace_root=self.workspace_root,
            runtime_image=runtime_image,
            docker_executable=docker_executable,
        )

    def _worker(
        self,
        worker_id: str,
        executor: PostgresSkillBuildExecutor,
    ) -> StudentSkillChainWorker:
        return StudentSkillChainWorker(
            database=self.database,
            application=self.application,
            validator=self.validator,
            worker_id=worker_id,
            artifact_root=self.artifact_root,
            lease_seconds=120,
            build_executor=executor,
        )

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

    async def _state(self) -> A8StateFingerprint:
        fingerprint = await a8_state_fingerprint(self.database)
        self.assertEqual(missing_a8_business_tables(fingerprint), ())
        return fingerprint

    async def _assert_only_failed_build_execution_changed(
        self,
        before: A8StateFingerprint,
        *,
        expected_history_delta: int,
        expected_receipt_delta: int,
    ) -> None:
        after = await self._state()
        self.assertEqual(
            fingerprint_without(after, A8_FAILED_BUILD_MUTATION_TABLES),
            fingerprint_without(before, A8_FAILED_BUILD_MUTATION_TABLES),
        )
        for table_name in ("yaya_commands", "yaya_control_jobs", "yaya_skill_builds"):
            self.assertEqual(after[table_name].row_count, before[table_name].row_count)
        self.assertEqual(
            after["yaya_skill_build_history"].row_count,
            before["yaya_skill_build_history"].row_count + expected_history_delta,
        )
        self.assertEqual(
            after["yaya_build_step_receipts"].row_count,
            before["yaya_build_step_receipts"].row_count + expected_receipt_delta,
        )
        self.assertEqual([path for path in self.artifact_root.rglob("*") if path.is_file()], [])

    async def _terminal_details(self, build_id: str) -> dict[str, object]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT b.status,b.terminal,b.resource_json,b.resource_sha256,
                       j.state AS job_state,j.attempt,j.fencing_token,
                       c.status AS command_status,
                       (SELECT array_agg(h.status ORDER BY h.sequence)
                          FROM yaya_skill_build_history h
                         WHERE h.tenant_id=b.tenant_id AND h.build_id=b.build_id)
                         AS history_statuses,
                       (SELECT array_agg(r.step ORDER BY array_position(
                           ARRAY['VALIDATE_SOURCE','COMPILE','PUBLIC_TEST',
                                 'HIDDEN_TEST','CERTIFY'],r.step))
                          FROM yaya_build_step_receipts r
                         WHERE r.tenant_id=b.tenant_id AND r.build_id=b.build_id)
                         AS receipt_steps,
                       (SELECT array_agg(r.outcome ORDER BY array_position(
                           ARRAY['VALIDATE_SOURCE','COMPILE','PUBLIC_TEST',
                                 'HIDDEN_TEST','CERTIFY'],r.step))
                          FROM yaya_build_step_receipts r
                         WHERE r.tenant_id=b.tenant_id AND r.build_id=b.build_id)
                         AS receipt_outcomes,
                       (SELECT array_agg(r.attempt ORDER BY array_position(
                           ARRAY['VALIDATE_SOURCE','COMPILE','PUBLIC_TEST',
                                 'HIDDEN_TEST','CERTIFY'],r.step))
                          FROM yaya_build_step_receipts r
                         WHERE r.tenant_id=b.tenant_id AND r.build_id=b.build_id)
                         AS receipt_attempts
                FROM yaya_skill_builds b
                JOIN yaya_control_jobs j
                  ON j.tenant_id=b.tenant_id AND j.command_id=b.command_id
                JOIN yaya_commands c
                  ON c.tenant_id=b.tenant_id AND c.command_id=b.command_id
                WHERE b.tenant_id=%s AND b.build_id=%s
                """,
                (self.authority.context.actor.tenant_id, build_id),
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            raise AssertionError("terminal Build row was not found")
        result = dict(row)
        resource = cast(dict[str, object], result["resource_json"])
        self.assertEqual(canonical_json_sha256(resource), result["resource_sha256"])
        self.assertIs(result["terminal"], True)
        self.assertEqual(result["job_state"], "SUCCEEDED")
        self.assertEqual(result["command_status"], "APPLIED")
        return result

    async def test_setup_authority_drift_is_a_queryable_terminal_failure(self) -> None:
        invalid_suite = "build-invalid-schema-1"
        invalid_policy = dict(self.authority.policy)
        invalid_policy.update(
            {
                "build_policy_id": "build_policy_invalid_schema_0001",
                "test_suite_version": invalid_suite,
                "parameter_schema": {"type": "not-a-real-json-schema-type"},
            }
        )
        connection = await self.database.connect(autocommit=True)
        try:
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
                    self.authority.context.actor.tenant_id,
                    invalid_policy["build_policy_id"],
                    self.authority.context.actor.actor_id,
                    self.authority.context.content_ref.content_hash,
                    invalid_policy["compiler_profile"],
                    invalid_suite,
                    invalid_policy["compiler_image"],
                    invalid_policy["compiler_version"],
                    Jsonb(invalid_policy["compile_flags"]),
                    Jsonb(invalid_policy["public_tests"]),
                    Jsonb(invalid_policy["hidden_tests"]),
                    Jsonb(invalid_policy["approved_capabilities"]),
                    Jsonb(invalid_policy["limits"]),
                    Jsonb(invalid_policy["parameter_schema"]),
                    invalid_policy["semantic_version_major"],
                    invalid_policy["semantic_version_minor"],
                    invalid_policy["runtime_abi_version"],
                    canonical_json_sha256(invalid_policy),
                ),
            )
        finally:
            await connection.close()
        body = self._request(
            "int main() { return 0; }\n",
            skill_id="skill_setup_authority_failure_0001",
        )
        body["test_suite_version"] = invalid_suite
        build_id, _ = await self._accept(
            body,
            suffix="setup_authority_0001",
            idempotency_key="idem-build-setup-authority-0001",
        )
        before = await self._state()

        worker = self._worker("failure-setup-worker-0001", self._executor())
        self.assertTrue(await worker.run_once())
        resource = (
            await self.application.get_build(build_id, self.authority.context.actor)
        ).payload
        self.assertEqual(resource["status"], "FAILED", resource)
        self.assertIs(resource["terminal"], True)
        self.assertIsNone(resource["artifact"])
        self.assertIsNone(resource["skill_version_id"])
        self.assertIsNone(resource["certification"])
        self.assertEqual(resource["evidence_refs"], [])
        failure = cast(dict[str, object], resource["failure"])
        self.assertEqual(failure["code"], "INVARIANT_VIOLATION")
        self.assertEqual(failure["stage"], "VALIDATE_SOURCE")
        details = await self._terminal_details(build_id)
        self.assertEqual(details["history_statuses"], ["ACCEPTED", "FAILED"])
        self.assertEqual(details["receipt_steps"], ["VALIDATE_SOURCE"])
        self.assertEqual(details["receipt_outcomes"], ["FAILED"])
        self.assertEqual(details["receipt_attempts"], [1])
        await self._assert_only_failed_build_execution_changed(
            before,
            expected_history_delta=1,
            expected_receipt_delta=1,
        )

    async def test_hidden_test_rejection_discloses_no_hidden_output_or_authority(self) -> None:
        source = """#include <iostream>
#include <string>

int main(int argc, char* argv[]) {
    if (argc != 2) return 2;
    std::string input;
    if (!std::getline(std::cin, input)) return 3;
    if (std::string(argv[1]) == "public-argument") {
        std::cout << argv[1] << ':' << input << '\\n';
    } else {
        std::cout << "incorrect-hidden-result\\n";
    }
    return 0;
}
"""
        body = self._request(source, skill_id="skill_hidden_rejection_0001")
        build_id, _ = await self._accept(
            body,
            suffix="hidden_rejection_0001",
            idempotency_key="idem-build-hidden-rejection-0001",
        )
        before = await self._state()

        worker = self._worker("hidden-rejection-worker-0001", self._executor())
        self.assertTrue(await worker.run_once())
        resource = (
            await self.application.get_build(build_id, self.authority.context.actor)
        ).payload
        self.assertEqual(resource["status"], "REJECTED", resource)
        failure = cast(dict[str, object], resource["failure"])
        self.assertEqual(failure["code"], "SANDBOX_COMPILE_ERROR")
        self.assertEqual(failure["stage"], "HIDDEN_TEST")
        failure_details = cast(dict[str, object], failure["details"])
        self.assertEqual(failure_details["pipeline_code"], "HIDDEN_TEST_OUTPUT_MISMATCH")
        diagnostics = cast(list[dict[str, object]], failure_details["diagnostics"])
        self.assertEqual(
            diagnostics,
            [
                {
                    "code": "HIDDEN_TEST_OUTPUT_MISMATCH",
                    "message": "A hidden certification test did not pass.",
                }
            ],
        )
        self.assertIsNone(resource["artifact"])
        self.assertIsNone(resource["skill_version_id"])
        self.assertIsNone(resource["certification"])
        self.assertEqual(resource["evidence_refs"], [])
        details = await self._terminal_details(build_id)
        self.assertEqual(details["history_statuses"], ["ACCEPTED", "COMPILING", "REJECTED"])
        self.assertEqual(
            details["receipt_steps"],
            ["VALIDATE_SOURCE", "COMPILE", "PUBLIC_TEST", "HIDDEN_TEST"],
        )
        self.assertEqual(details["receipt_outcomes"], ["PASSED", "PASSED", "PASSED", "FAILED"])
        await self._assert_only_failed_build_execution_changed(
            before,
            expected_history_delta=2,
            expected_receipt_delta=4,
        )

    async def test_docker_dependency_and_runtime_invariant_are_not_student_rejections(self) -> None:
        source = """#include <iostream>
#include <string>
int main(int argc, char* argv[]) {
    std::string input;
    if (argc != 2 || !std::getline(std::cin, input)) return 2;
    std::cout << argv[1] << ':' << input << '\\n';
    return 0;
}
"""
        unavailable_body = self._request(
            source,
            skill_id="skill_docker_dependency_failure_0001",
        )
        unavailable_id, _ = await self._accept(
            unavailable_body,
            suffix="docker_dependency_0001",
            idempotency_key="idem-build-docker-dependency-0001",
        )
        unavailable_before = await self._state()
        unavailable_worker = self._worker(
            "docker-dependency-worker-0001",
            self._executor(docker_executable="yaya-docker-command-missing-0001"),
        )
        self.assertTrue(await unavailable_worker.run_once())
        unavailable = (
            await self.application.get_build(unavailable_id, self.authority.context.actor)
        ).payload
        self.assertEqual(unavailable["status"], "FAILED", unavailable)
        unavailable_failure = cast(dict[str, object], unavailable["failure"])
        self.assertEqual(unavailable_failure["code"], "DEPENDENCY_UNAVAILABLE")
        self.assertEqual(unavailable_failure["category"], "DEPENDENCY")
        self.assertIs(unavailable_failure["retryable"], True)
        unavailable_details = cast(dict[str, object], unavailable_failure["details"])
        self.assertEqual(unavailable_details["pipeline_code"], "DOCKER_UNAVAILABLE")
        await self._assert_only_failed_build_execution_changed(
            unavailable_before,
            expected_history_delta=2,
            expected_receipt_delta=2,
        )

        invariant_body = self._request(
            source,
            skill_id="skill_runtime_invariant_failure_0001",
        )
        invariant_id, _ = await self._accept(
            invariant_body,
            suffix="runtime_invariant_0001",
            idempotency_key="idem-build-runtime-invariant-0001",
        )
        invariant_before = await self._state()
        fake_runtime_image = "gcc@sha256:" + "e" * 64
        invariant_worker = self._worker(
            "runtime-invariant-worker-0001",
            self._executor(runtime_image=fake_runtime_image),
        )
        self.assertTrue(await invariant_worker.run_once())
        invariant = (
            await self.application.get_build(invariant_id, self.authority.context.actor)
        ).payload
        self.assertEqual(invariant["status"], "FAILED", invariant)
        invariant_failure = cast(dict[str, object], invariant["failure"])
        self.assertEqual(invariant_failure["code"], "INVARIANT_VIOLATION")
        self.assertEqual(invariant_failure["category"], "INVARIANT")
        self.assertEqual(invariant_failure["stage"], "VALIDATE_SOURCE")

        await self._assert_only_failed_build_execution_changed(
            invariant_before,
            expected_history_delta=1,
            expected_receipt_delta=1,
        )
        for build_id in (unavailable_id, invariant_id):
            resource = (
                await self.application.get_build(build_id, self.authority.context.actor)
            ).payload
            self.assertIsNone(resource["artifact"])
            self.assertIsNone(resource["skill_version_id"])
            self.assertIsNone(resource["certification"])
            self.assertEqual(resource["evidence_refs"], [])

    async def test_takeover_fences_stale_lease_and_commit_loss_does_not_duplicate(self) -> None:
        source = """#include <iostream>
#include <string>

int main(int argc, char* argv[]) {
    if (argc != 2) return 2;
    std::string input;
    if (!std::getline(std::cin, input)) return 3;
    std::cout << argv[1] << ':' << input << '\\n';
    return 0;
}
"""
        body = self._request(source, skill_id="skill_takeover_commit_loss_0001")
        idempotency_key = "idem-build-takeover-commit-loss-0001"
        build_id, raw_body = await self._accept(
            body,
            suffix="takeover_commit_loss_0001",
            idempotency_key=idempotency_key,
        )

        stale_worker = self._worker("stale-build-worker-0001", self._executor())
        stale_claim = await stale_worker._claim_one()  # pyright: ignore[reportPrivateUsage]
        self.assertIsNotNone(stale_claim)
        claim = cast(BuildJobClaim, stale_claim)
        self.assertEqual(claim.attempt, 1)
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                UPDATE yaya_control_jobs
                   SET heartbeat_at=clock_timestamp() - interval '2 seconds',
                       lease_expires_at=clock_timestamp() - interval '1 second'
                 WHERE tenant_id=%s AND job_id=%s AND worker_id=%s AND fencing_token=%s
                """,
                (claim.tenant_id, claim.job_id, claim.worker_id, claim.fencing_token),
            )
        finally:
            await connection.close()

        commit_loss_database = _PostCommitUnknownDatabase(
            self.server.dsn,
            fail_on_commit=2,
        )
        takeover_worker = self._worker(
            "takeover-build-worker-0001",
            self._executor(database=commit_loss_database),
        )
        self.assertTrue(await takeover_worker.run_once())
        self.assertTrue(commit_loss_database.did_fail)
        self.assertEqual(commit_loss_database.commit_count, 2)

        resource = (
            await self.application.get_build(build_id, self.authority.context.actor)
        ).payload
        self.assertEqual(resource["status"], "CERTIFIED", resource)
        self.assertIs(resource["terminal"], True)
        self.assertIsNone(resource["failure"])
        artifact = cast(dict[str, object], resource["artifact"])
        artifact_sha256 = cast(str, artifact["artifact_sha256"])
        artifact_path = self.artifact_root / artifact_sha256[:2] / artifact_sha256
        self.assertTrue(artifact_path.is_file())
        self.assertEqual(hashlib.sha256(artifact_path.read_bytes()).hexdigest(), artifact_sha256)

        before_stale_heartbeat = await self._state()
        with self.assertRaises(BackendApplicationError) as stale:
            await stale_worker.heartbeat(claim)
        self.assertEqual(stale.exception.code, "INVARIANT_VIOLATION")
        self.assertEqual(await self._state(), before_stale_heartbeat)

        before_replay = await self._state()
        replay = await self.application.accept_build(
            actor=self.authority.context.actor,
            attempt=self._attempt("takeover_replay_0001"),
            idempotency_key=idempotency_key,
            raw_body=raw_body,
            body=body,
        )
        self.assertTrue(replay.replayed)
        restarted_worker = self._worker("restarted-build-worker-0001", self._executor())
        self.assertFalse(await restarted_worker.run_once())
        self.assertEqual(await self._state(), before_replay)

        details = await self._terminal_details(build_id)
        self.assertEqual(details["attempt"], 2)
        self.assertEqual(details["fencing_token"], 2)
        self.assertEqual(details["history_statuses"], ["ACCEPTED", "COMPILING", "CERTIFIED"])
        self.assertEqual(
            details["receipt_steps"],
            ["VALIDATE_SOURCE", "COMPILE", "PUBLIC_TEST", "HIDDEN_TEST", "CERTIFY"],
        )
        self.assertEqual(details["receipt_outcomes"], ["PASSED"] * 5)
        self.assertEqual(details["receipt_attempts"], [2] * 5)

        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM yaya_artifacts WHERE build_id=%s)::integer
                    AS artifacts,
                  (SELECT count(*) FROM yaya_skills WHERE skill_id=%s)::integer
                    AS skill_versions,
                  (SELECT count(*) FROM yaya_skill_certifications WHERE build_id=%s)::integer
                    AS certifications,
                  (SELECT count(*) FROM yaya_compile_results WHERE build_id=%s)::integer
                    AS compile_results,
                  (SELECT count(*) FROM yaya_evidence
                    WHERE evidence_json #>> '{source,source_id}'=%s)::integer AS evidence,
                  (SELECT count(*) FROM yaya_build_step_receipts WHERE build_id=%s)::integer
                    AS receipts
                """,
                (
                    build_id,
                    body["skill_id"],
                    build_id,
                    build_id,
                    build_id,
                    build_id,
                ),
            )
            counts = await cursor.fetchone()
        finally:
            await connection.close()
        self.assertEqual(
            counts,
            {
                "artifacts": 1,
                "skill_versions": 1,
                "certifications": 1,
                "compile_results": 1,
                "evidence": 1,
                "receipts": 5,
            },
        )
        self.assertEqual(
            [path for path in self.artifact_root.rglob("*") if path.is_file()],
            [artifact_path],
        )

    async def test_compiling_restart_config_failure_closes_three_step_history(self) -> None:
        body = self._request(
            "int main() { return 0; }\n",
            skill_id="skill_compiling_restart_failure_0001",
        )
        build_id, _ = await self._accept(
            body,
            suffix="compiling_restart_failure_0001",
            idempotency_key="idem-build-compiling-restart-failure-0001",
        )

        first_executor = self._executor()
        first_worker = self._worker("compiling-first-worker-0001", first_executor)
        raw_claim = await first_worker._claim_one()  # pyright: ignore[reportPrivateUsage]
        self.assertIsNotNone(raw_claim)
        first_claim = cast(BuildJobClaim, raw_claim)
        await first_executor._prepare(  # pyright: ignore[reportPrivateUsage]
            first_claim,
            first_worker,
        )
        compiling = (
            await self.application.get_build(build_id, self.authority.context.actor)
        ).payload
        self.assertEqual(compiling["status"], "COMPILING")
        self.assertIs(compiling["terminal"], False)

        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                UPDATE yaya_control_jobs
                   SET heartbeat_at=clock_timestamp() - interval '2 seconds',
                       lease_expires_at=clock_timestamp() - interval '1 second'
                 WHERE tenant_id=%s AND job_id=%s AND worker_id=%s AND fencing_token=%s
                """,
                (
                    first_claim.tenant_id,
                    first_claim.job_id,
                    first_claim.worker_id,
                    first_claim.fencing_token,
                ),
            )
        finally:
            await connection.close()

        before = await self._state()
        mismatched_runtime = "gcc@sha256:" + "d" * 64
        restarted_worker = self._worker(
            "compiling-restart-worker-0001",
            self._executor(runtime_image=mismatched_runtime),
        )
        self.assertTrue(await restarted_worker.run_once())
        after_failure = await self._state()
        with self.assertRaises(BackendApplicationError):
            await first_worker.heartbeat(first_claim)
        self.assertEqual(await self._state(), after_failure)

        resource = (
            await self.application.get_build(build_id, self.authority.context.actor)
        ).payload
        self.assertEqual(resource["status"], "FAILED", resource)
        self.assertIs(resource["terminal"], True)
        self.assertIsNone(resource["artifact"])
        self.assertIsNone(resource["skill_version_id"])
        self.assertIsNone(resource["certification"])
        self.assertEqual(resource["evidence_refs"], [])
        failure = cast(dict[str, object], resource["failure"])
        self.assertEqual(failure["code"], "INVARIANT_VIOLATION")
        self.assertEqual(failure["stage"], "VALIDATE_SOURCE")

        details = await self._terminal_details(build_id)
        self.assertEqual(details["attempt"], 2)
        self.assertEqual(details["fencing_token"], 2)
        self.assertEqual(
            details["history_statuses"],
            ["ACCEPTED", "COMPILING", "FAILED"],
        )
        self.assertEqual(details["receipt_steps"], ["VALIDATE_SOURCE"])
        self.assertEqual(details["receipt_outcomes"], ["FAILED"])
        self.assertEqual(details["receipt_attempts"], [2])
        await self._assert_only_failed_build_execution_changed(
            before,
            expected_history_delta=1,
            expected_receipt_delta=1,
        )

    async def test_malformed_docker_test_identity_becomes_internal_failure(self) -> None:
        body = self._request(
            "int main() { return 0; }\n",
            skill_id="skill_forged_docker_result_0001",
        )
        build_id, _ = await self._accept(
            body,
            suffix="forged_docker_result_0001",
            idempotency_key="idem-build-forged-docker-result-0001",
        )
        before = await self._state()

        def forged_result(
            builder: DigestPinnedDockerCppBuilder,
            raw_request: CompileAndTestRequest,
        ) -> DockerBuildResult:
            validated = validate_source_bundle(raw_request.source_bundle)
            diagnostic = BuildDiagnostic(
                "PUBLIC_TEST_FAILED",
                "forged result must not become certification authority",
            )
            return DockerBuildResult(
                build_id=raw_request.build_id,
                status="FAILED",
                source_sha256=validated.source_sha256,
                compiler_profile=raw_request.compiler_profile,
                compiler_version=PINNED_GCC_VERSION,
                test_suite_version=raw_request.test_suite_version,
                build_identity=builder.build_identity(raw_request),
                workspace=None,
                staged_artifact=None,
                artifact_sha256=None,
                tests=(
                    DockerTestResult(
                        test_case_id="forged_public_test_0001",
                        visibility="PUBLIC",
                        status="FAILED",
                        diagnostic_codes=("PUBLIC_TEST_FAILED",),
                    ),
                ),
                diagnostics=(diagnostic,),
                failure=DockerBuildFailure(
                    code="PUBLIC_TEST_FAILED",
                    stage="PUBLIC_TEST",
                    diagnostics=(diagnostic,),
                ),
            )

        worker = self._worker("forged-result-worker-0001", self._executor())
        with patch.object(
            DigestPinnedDockerCppBuilder,
            "build",
            autospec=True,
            side_effect=forged_result,
        ) as build:
            self.assertTrue(await worker.run_once())
        build.assert_called_once()

        resource = (
            await self.application.get_build(build_id, self.authority.context.actor)
        ).payload
        self.assertEqual(resource["status"], "FAILED", resource)
        self.assertIs(resource["terminal"], True)
        self.assertIsNone(resource["artifact"])
        self.assertIsNone(resource["skill_version_id"])
        self.assertIsNone(resource["certification"])
        self.assertEqual(resource["evidence_refs"], [])
        failure = cast(dict[str, object], resource["failure"])
        self.assertEqual(failure["code"], "INTERNAL_ERROR")
        self.assertEqual(failure["category"], "INTERNAL")
        self.assertEqual(failure["stage"], "COMPILE")
        failure_details = cast(dict[str, object], failure["details"])
        self.assertEqual(
            failure_details["pipeline_code"],
            "BUILD_RESULT_AUTHORITY_DRIFT",
        )

        details = await self._terminal_details(build_id)
        self.assertEqual(
            details["history_statuses"],
            ["ACCEPTED", "COMPILING", "FAILED"],
        )
        self.assertEqual(details["receipt_steps"], ["VALIDATE_SOURCE", "COMPILE"])
        self.assertEqual(details["receipt_outcomes"], ["PASSED", "FAILED"])
        self.assertEqual(details["receipt_attempts"], [1, 1])
        await self._assert_only_failed_build_execution_changed(
            before,
            expected_history_delta=2,
            expected_receipt_delta=2,
        )


if __name__ == "__main__":
    unittest.main()
