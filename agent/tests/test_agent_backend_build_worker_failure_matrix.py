from __future__ import annotations

import asyncio
import hashlib
import json
import stat
import sys
import tempfile
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from unittest.mock import patch

import psycopg

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
from test_agent_backend_skill_build_executor import (  # noqa: E402
    BUILD_POLICY_ID,
    PINNED_GCC_IMAGE,
    PINNED_GCC_VERSION,
    TEST_SUITE_VERSION,
    _AuthorityFixture,  # pyright: ignore[reportPrivateUsage]
    _seed_only_build_authority,  # pyright: ignore[reportPrivateUsage]
)
from yaya_agent_backend.application import HttpAttempt  # noqa: E402
from yaya_agent_backend.codec import decode_as  # noqa: E402
from yaya_agent_backend.database import PostgresDatabase  # noqa: E402
from yaya_agent_backend.skill_builds import PostgresSkillBuildExecutor  # noqa: E402
from yaya_agent_backend.student_skill_chain import (  # noqa: E402
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
    canonical_source_bundle_sha256,
    validate_source_bundle,
)
from yaya_agent_contracts import (  # noqa: E402
    CommandRecord,
    CommandStatus,
    CompileAndTestRequest,
    canonical_json_sha256,
)

_PHASES = ("VALIDATE_SOURCE", "COMPILE", "PUBLIC_TEST", "HIDDEN_TEST", "CERTIFY")
_PUBLIC_TEST_ID = "public_exact_io_0001"
_HIDDEN_TEST_ID = "hidden_exact_io_0001"


@dataclass(frozen=True, slots=True)
class _FailureScenario:
    suffix: str
    pipeline_code: str
    stage: Literal["VALIDATE_SOURCE", "COMPILE", "PUBLIC_TEST", "HIDDEN_TEST"]
    expected_status: Literal["REJECTED", "FAILED"]
    expected_contract_code: str
    expected_category: str
    failed_test_status: Literal["FAILED", "TIMEOUT", "ERROR"] | None = None
    source_sha256_override: str | None = None
    corrupt_phase: bool = False
    corrupt_diagnostics: bool = False
    expected_pipeline_code: str | None = None


def _json_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class PostgresBuildWorkerFailureMatrixTests(unittest.IsolatedAsyncioTestCase):
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
            prefix="yaya-build-worker-matrix-artifacts-"
        )
        self._workspace_directory = tempfile.TemporaryDirectory(
            prefix="yaya-build-worker-matrix-workspaces-"
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
            request_id=f"req_build_worker_matrix_{suffix}",
            trace_id=f"trace_build_worker_matrix_{suffix}",
            correlation_id=f"corr_build_worker_matrix_{suffix}",
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

    def _request(self, suffix: str) -> dict[str, object]:
        source = "int main() { return 0; }\n"
        return {
            "skill_id": f"skill_worker_matrix_{suffix}",
            "display_name": f"Worker matrix {suffix}",
            "client_draft_revision": 19,
            "source_bundle": self._bundle(source),
            "compiler_profile": CPP20_SAFE_V1_PROFILE,
            "test_suite_version": TEST_SUITE_VERSION,
            "requested_capabilities": ["WORLD_READ"],
        }

    def _executor(self) -> PostgresSkillBuildExecutor:
        return PostgresSkillBuildExecutor(
            database=self.database,
            validator=self.validator,
            artifact_root=self.artifact_root,
            workspace_root=self.workspace_root,
            runtime_image=PINNED_GCC_IMAGE,
        )

    def _worker(self, suffix: str) -> StudentSkillChainWorker:
        return StudentSkillChainWorker(
            database=self.database,
            application=self.application,
            validator=self.validator,
            worker_id=f"build-worker-matrix-{suffix}",
            artifact_root=self.artifact_root,
            lease_seconds=120,
            build_executor=self._executor(),
        )

    async def _accept(self, suffix: str) -> tuple[str, dict[str, object]]:
        body = self._request(suffix)
        accepted = await self.application.accept_build(
            actor=self.authority.context.actor,
            attempt=self._attempt(suffix),
            idempotency_key=f"idem-build-worker-matrix-{suffix}",
            raw_body=_json_bytes(body),
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
        return cast(str, cast(dict[str, object], row)["resource_id"]), body

    @staticmethod
    def _test_results(
        scenario: _FailureScenario,
    ) -> tuple[DockerTestResult, ...]:
        if scenario.stage in {"VALIDATE_SOURCE", "COMPILE"}:
            return ()
        failed_status = scenario.failed_test_status
        if failed_status is None:
            raise AssertionError("test-stage failure requires a test result status")
        failed_visibility: Literal["PUBLIC", "HIDDEN"] = (
            "PUBLIC" if scenario.stage == "PUBLIC_TEST" else "HIDDEN"
        )
        failed = DockerTestResult(
            test_case_id=(_PUBLIC_TEST_ID if failed_visibility == "PUBLIC" else _HIDDEN_TEST_ID),
            visibility=failed_visibility,
            status=failed_status,
            diagnostic_codes=(scenario.pipeline_code,),
        )
        if failed_visibility == "PUBLIC":
            return (failed,)
        return (
            DockerTestResult(_PUBLIC_TEST_ID, "PUBLIC", "PASSED", ()),
            failed,
        )

    @staticmethod
    def _failure_result(
        builder: DigestPinnedDockerCppBuilder,
        request: CompileAndTestRequest,
        scenario: _FailureScenario,
    ) -> DockerBuildResult:
        validated = validate_source_bundle(request.source_bundle)
        diagnostic = BuildDiagnostic(
            scenario.pipeline_code,
            (
                "A hidden certification test did not pass."
                if scenario.stage == "HIDDEN_TEST"
                else f"deterministic {scenario.pipeline_code.lower()} failure"
            ),
        )
        failure_diagnostics = (diagnostic,)
        result_diagnostics = failure_diagnostics
        if scenario.corrupt_diagnostics:
            result_diagnostics = (
                BuildDiagnostic("FORGED_DIAGNOSTIC", "untrusted diagnostic drift"),
            )
        failure_stage = scenario.stage
        if scenario.corrupt_phase:
            failure_stage = cast(
                Literal["VALIDATE_SOURCE", "COMPILE", "PUBLIC_TEST", "HIDDEN_TEST"],
                "CERTIFY",
            )
        return DockerBuildResult(
            build_id=request.build_id,
            status="FAILED",
            source_sha256=(
                validated.source_sha256
                if scenario.source_sha256_override is None
                else scenario.source_sha256_override
            ),
            compiler_profile=request.compiler_profile,
            compiler_version=PINNED_GCC_VERSION,
            test_suite_version=request.test_suite_version,
            build_identity=builder.build_identity(request),
            workspace=None,
            staged_artifact=None,
            artifact_sha256=None,
            tests=PostgresBuildWorkerFailureMatrixTests._test_results(scenario),
            diagnostics=result_diagnostics,
            failure=DockerBuildFailure(
                code=scenario.pipeline_code,
                stage=failure_stage,
                diagnostics=failure_diagnostics,
            ),
        )

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
        self.assertEqual(
            [path for path in self.artifact_root.rglob("*") if path.is_file()],
            [],
        )

    async def _durable_rows(self, build_id: str) -> dict[str, object]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT b.status,b.terminal,b.source_bundle_sha256,b.build_policy_id,
                       b.resource_json,b.resource_sha256,j.state AS job_state,
                       j.phase AS job_phase,j.attempt,j.fencing_token,j.worker_id,
                       j.lease_id,j.result_json AS job_result,c.status AS command_status,
                       c.record_json AS command_json,
                       COALESCE((
                           SELECT jsonb_agg(jsonb_build_object(
                               'sequence',h.sequence,'status',h.status,
                               'record_sha256',h.record_sha256,
                               'record_json',h.record_json
                           ) ORDER BY h.sequence)
                           FROM yaya_skill_build_history h
                           WHERE h.tenant_id=b.tenant_id AND h.build_id=b.build_id
                       ),'[]'::jsonb) AS history_json,
                       COALESCE((
                           SELECT jsonb_agg(jsonb_build_object(
                               'step',r.step,'attempt',r.attempt,
                               'input_sha256',r.input_sha256,
                               'output_sha256',r.output_sha256,
                               'outcome',r.outcome,'receipt_json',r.receipt_json
                           ) ORDER BY CASE r.step
                               WHEN 'VALIDATE_SOURCE' THEN 1 WHEN 'COMPILE' THEN 2
                               WHEN 'PUBLIC_TEST' THEN 3 WHEN 'HIDDEN_TEST' THEN 4
                               WHEN 'CERTIFY' THEN 5 END)
                           FROM yaya_build_step_receipts r
                           WHERE r.tenant_id=b.tenant_id AND r.build_id=b.build_id
                       ),'[]'::jsonb) AS receipts_json
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
            raise AssertionError("durable Build closure was not found")
        return dict(row)

    async def _assert_durable_failure(
        self,
        build_id: str,
        resource: dict[str, object],
        scenario: _FailureScenario,
    ) -> None:
        row = await self._durable_rows(build_id)
        expected_pipeline_code = scenario.expected_pipeline_code or scenario.pipeline_code
        expected_stage = (
            "COMPILE"
            if scenario.corrupt_phase
            or scenario.corrupt_diagnostics
            or scenario.source_sha256_override is not None
            else scenario.stage
        )
        terminal_index = _PHASES.index(expected_stage)
        expected_steps = list(_PHASES[: terminal_index + 1])

        self.assertEqual(row["status"], scenario.expected_status)
        self.assertIs(row["terminal"], True)
        self.assertEqual(row["resource_json"], resource)
        self.assertEqual(row["resource_sha256"], canonical_json_sha256(resource))
        self.assertEqual(row["job_state"], "SUCCEEDED")
        self.assertEqual(row["job_phase"], "COMPLETE")
        self.assertEqual(row["attempt"], 1)
        self.assertEqual(row["fencing_token"], 1)
        self.assertIsNone(row["worker_id"])
        self.assertIsNone(row["lease_id"])
        self.assertEqual(row["command_status"], "APPLIED")
        expected_result = {
            "result_type": "RESOURCE_CREATED",
            "resource_type": "SKILL_BUILD",
            "resource_id": build_id,
            "resource_url": f"/v1/skill-builds/{build_id}",
        }
        self.assertEqual(row["job_result"], expected_result)
        command = decode_as(row["command_json"], CommandRecord)
        self.assertIs(command.status, CommandStatus.APPLIED)
        self.assertIs(command.terminal, True)
        self.assertEqual(command.result, expected_result)

        history = cast(list[dict[str, object]], row["history_json"])
        self.assertEqual(
            [item["status"] for item in history],
            ["ACCEPTED", "COMPILING", scenario.expected_status],
        )
        self.assertEqual([item["sequence"] for item in history], [1, 2, 3])
        for item in history:
            record = cast(dict[str, object], item["record_json"])
            self.assertEqual(item["record_sha256"], canonical_json_sha256(record))
        self.assertEqual(history[-1]["record_json"], resource)

        receipts = cast(list[dict[str, object]], row["receipts_json"])
        self.assertEqual([item["step"] for item in receipts], expected_steps)
        self.assertEqual([item["attempt"] for item in receipts], [1] * len(receipts))
        self.assertEqual(
            [item["outcome"] for item in receipts],
            ["PASSED"] * (len(receipts) - 1) + ["FAILED"],
        )
        policy_sha256 = canonical_json_sha256(self.authority.policy)
        for index, stored in enumerate(receipts):
            step = cast(str, stored["step"])
            receipt = cast(dict[str, object], stored["receipt_json"])
            expected_outcome = "FAILED" if index == len(receipts) - 1 else "PASSED"
            self.assertEqual(stored["output_sha256"], canonical_json_sha256(receipt))
            self.assertEqual(
                stored["input_sha256"],
                canonical_json_sha256(
                    {
                        "build_id": build_id,
                        "step": step,
                        "source_sha256": row["source_bundle_sha256"],
                        "build_policy_id": BUILD_POLICY_ID,
                        "policy_sha256": policy_sha256,
                    }
                ),
            )
            self.assertEqual(receipt["build_id"], build_id)
            self.assertEqual(receipt["step"], step)
            self.assertEqual(receipt["attempt"], 1)
            self.assertEqual(receipt["source_sha256"], row["source_bundle_sha256"])
            self.assertEqual(receipt["build_policy_id"], BUILD_POLICY_ID)
            self.assertEqual(receipt["policy_sha256"], policy_sha256)
            self.assertEqual(receipt["outcome"], expected_outcome)
            self.assertEqual(receipt["pipeline_status"], "FAILED")
            self.assertIsNone(receipt["artifact_sha256"])
            self.assertIsNone(receipt["terminal_failure_code"])

        phases = cast(list[dict[str, object]], resource["phases"])
        self.assertEqual([item["name"] for item in phases], list(_PHASES))
        self.assertEqual(
            [item["status"] for item in phases],
            ["PASSED"] * terminal_index
            + ["FAILED"]
            + ["SKIPPED"] * (len(_PHASES) - terminal_index - 1),
        )
        failed_phase = phases[terminal_index]
        self.assertIn(
            expected_pipeline_code,
            cast(list[object], failed_phase["diagnostic_codes"]),
        )

    async def _run_failure(self, scenario: _FailureScenario) -> tuple[str, dict[str, object]]:
        build_id, body = await self._accept(scenario.suffix)
        before = await self._state()

        def deterministic_failure(
            builder: DigestPinnedDockerCppBuilder,
            request: CompileAndTestRequest,
        ) -> DockerBuildResult:
            return self._failure_result(builder, request, scenario)

        worker = self._worker(scenario.suffix)
        with patch.object(
            DigestPinnedDockerCppBuilder,
            "build",
            autospec=True,
            side_effect=deterministic_failure,
        ) as build:
            self.assertTrue(await worker.run_once())
        build.assert_called_once()

        resource = dict(
            (
                await self.application.get_build(
                    build_id,
                    self.authority.context.actor,
                )
            ).payload
        )
        expected_pipeline_code = scenario.expected_pipeline_code or scenario.pipeline_code
        self.assertEqual(resource["status"], scenario.expected_status, resource)
        self.assertIs(resource["terminal"], True)
        self.assertIsNone(resource["artifact"])
        self.assertIsNone(resource["skill_version_id"])
        self.assertIsNone(resource["certification"])
        self.assertEqual(resource["evidence_refs"], [])
        failure = cast(dict[str, object], resource["failure"])
        self.assertEqual(failure["code"], scenario.expected_contract_code)
        self.assertEqual(failure["category"], scenario.expected_category)
        details = cast(dict[str, object], failure["details"])
        self.assertEqual(details["pipeline_code"], expected_pipeline_code)
        await self._assert_durable_failure(build_id, resource, scenario)
        trusted_stage = (
            "COMPILE"
            if scenario.corrupt_phase
            or scenario.corrupt_diagnostics
            or scenario.source_sha256_override is not None
            else scenario.stage
        )
        await self._assert_only_failed_build_execution_changed(
            before,
            expected_history_delta=2,
            expected_receipt_delta=_PHASES.index(trusted_stage) + 1,
        )
        source = cast(dict[str, object], body["source_bundle"])
        self.assertEqual(
            canonical_source_bundle_sha256(source),
            (await self._durable_rows(build_id))["source_bundle_sha256"],
        )
        return build_id, resource

    async def test_warning_as_error_is_a_queryable_durable_rejection(self) -> None:
        await self._run_failure(
            _FailureScenario(
                suffix="warning_error_0001",
                pipeline_code="COMPILE_ERROR",
                stage="COMPILE",
                expected_status="REJECTED",
                expected_contract_code="SANDBOX_COMPILE_ERROR",
                expected_category="SANDBOX",
            )
        )

    async def test_compile_timeout_is_a_queryable_resource_limit(self) -> None:
        await self._run_failure(
            _FailureScenario(
                suffix="compile_timeout_0001",
                pipeline_code="COMPILE_TIMEOUT",
                stage="COMPILE",
                expected_status="REJECTED",
                expected_contract_code="SANDBOX_RESOURCE_LIMIT",
                expected_category="SANDBOX",
            )
        )

    async def test_public_and_hidden_test_timeouts_close_exact_receipts(self) -> None:
        for visibility in ("public", "hidden"):
            with self.subTest(visibility=visibility):
                is_public = visibility == "public"
                await self._run_failure(
                    _FailureScenario(
                        suffix=f"{visibility}_timeout_0001",
                        pipeline_code=(
                            "PUBLIC_TEST_TIMEOUT" if is_public else "HIDDEN_TEST_TIMEOUT"
                        ),
                        stage="PUBLIC_TEST" if is_public else "HIDDEN_TEST",
                        expected_status="REJECTED",
                        expected_contract_code="SANDBOX_RESOURCE_LIMIT",
                        expected_category="SANDBOX",
                        failed_test_status="TIMEOUT",
                    )
                )

    async def test_public_and_hidden_output_limits_create_no_authority(self) -> None:
        for visibility in ("public", "hidden"):
            with self.subTest(visibility=visibility):
                is_public = visibility == "public"
                await self._run_failure(
                    _FailureScenario(
                        suffix=f"{visibility}_output_limit_0001",
                        pipeline_code=(
                            "PUBLIC_TEST_OUTPUT_LIMIT" if is_public else "HIDDEN_TEST_OUTPUT_LIMIT"
                        ),
                        stage="PUBLIC_TEST" if is_public else "HIDDEN_TEST",
                        expected_status="REJECTED",
                        expected_contract_code="SANDBOX_RESOURCE_LIMIT",
                        expected_category="SANDBOX",
                        failed_test_status="ERROR",
                    )
                )

    async def test_image_digest_and_inspect_drift_fail_closed(self) -> None:
        scenarios = (
            _FailureScenario(
                suffix="image_digest_drift_0001",
                pipeline_code="COMPILER_IMAGE_DIGEST_DRIFT",
                stage="COMPILE",
                expected_status="FAILED",
                expected_contract_code="INVARIANT_VIOLATION",
                expected_category="INVARIANT",
            ),
            _FailureScenario(
                suffix="image_inspect_drift_0001",
                pipeline_code="COMPILER_IMAGE_INSPECT_FAILED",
                stage="COMPILE",
                expected_status="FAILED",
                expected_contract_code="DEPENDENCY_UNAVAILABLE",
                expected_category="DEPENDENCY",
            ),
        )
        for scenario in scenarios:
            with self.subTest(pipeline_code=scenario.pipeline_code):
                await self._run_failure(scenario)

    async def test_source_hash_drift_becomes_a_trusted_internal_failure(self) -> None:
        await self._run_failure(
            _FailureScenario(
                suffix="source_hash_drift_0001",
                pipeline_code="COMPILE_ERROR",
                stage="COMPILE",
                expected_status="FAILED",
                expected_contract_code="INTERNAL_ERROR",
                expected_category="INTERNAL",
                source_sha256_override="f" * 64,
                expected_pipeline_code="BUILD_RESULT_AUTHORITY_DRIFT",
            )
        )

    async def test_phase_and_diagnostic_corruption_fail_closed_and_stay_immutable(self) -> None:
        scenarios = (
            _FailureScenario(
                suffix="phase_corruption_0001",
                pipeline_code="COMPILE_ERROR",
                stage="COMPILE",
                expected_status="FAILED",
                expected_contract_code="INTERNAL_ERROR",
                expected_category="INTERNAL",
                corrupt_phase=True,
                expected_pipeline_code="BUILD_RESULT_AUTHORITY_DRIFT",
            ),
            _FailureScenario(
                suffix="diagnostic_corruption_0001",
                pipeline_code="COMPILE_ERROR",
                stage="COMPILE",
                expected_status="FAILED",
                expected_contract_code="INTERNAL_ERROR",
                expected_category="INTERNAL",
                corrupt_diagnostics=True,
                expected_pipeline_code="BUILD_RESULT_AUTHORITY_DRIFT",
            ),
        )
        for scenario in scenarios:
            with self.subTest(corruption=scenario.suffix):
                build_id, resource = await self._run_failure(scenario)
                before = await self._state()
                connection = await self.database.connect(autocommit=True)
                try:
                    with self.assertRaises(psycopg.Error) as head_error:
                        await connection.execute(
                            """
                            UPDATE yaya_skill_builds
                               SET resource_json=jsonb_set(
                                   resource_json,'{phases,1,diagnostic_codes}',
                                   '[\"FORGED_PERSISTED_DIAGNOSTIC\"]'::jsonb
                               )
                             WHERE tenant_id=%s AND build_id=%s
                            """,
                            (self.authority.context.actor.tenant_id, build_id),
                        )
                    self.assertEqual(head_error.exception.sqlstate, "55000")
                    with self.assertRaises(psycopg.Error) as history_error:
                        await connection.execute(
                            """
                            UPDATE yaya_skill_build_history
                               SET record_json=jsonb_set(
                                   record_json,'{phases,1,diagnostic_codes}',
                                   '[\"FORGED_PERSISTED_DIAGNOSTIC\"]'::jsonb
                               )
                             WHERE tenant_id=%s AND build_id=%s AND sequence=3
                            """,
                            (self.authority.context.actor.tenant_id, build_id),
                        )
                    self.assertEqual(history_error.exception.sqlstate, "55000")
                finally:
                    await connection.close()
                recovered = (
                    await self.application.get_build(build_id, self.authority.context.actor)
                ).payload
                self.assertEqual(recovered, resource)
                self.assertEqual(await self._state(), before)

    async def test_persisted_build_evidence_is_immutable(self) -> None:
        suffix = "evidence_immutability_0001"
        build_id, _ = await self._accept(suffix)
        artifact_bytes = b"\x7fELF deterministic durable worker matrix artifact"
        artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()

        def deterministic_success(
            builder: DigestPinnedDockerCppBuilder,
            request: CompileAndTestRequest,
        ) -> DockerBuildResult:
            validated = validate_source_bundle(request.source_bundle)
            identity = builder.build_identity(request)
            workspace = self.workspace_root / f"build-{identity}"
            workspace.mkdir(parents=True, exist_ok=False)
            staged = workspace / "skill"
            staged.write_bytes(artifact_bytes)
            return DockerBuildResult(
                build_id=request.build_id,
                status="SUCCEEDED",
                source_sha256=validated.source_sha256,
                compiler_profile=request.compiler_profile,
                compiler_version=PINNED_GCC_VERSION,
                test_suite_version=request.test_suite_version,
                build_identity=identity,
                workspace=workspace,
                staged_artifact=staged,
                artifact_sha256=artifact_sha256,
                tests=(
                    DockerTestResult(_PUBLIC_TEST_ID, "PUBLIC", "PASSED", ()),
                    DockerTestResult(_HIDDEN_TEST_ID, "HIDDEN", "PASSED", ()),
                ),
                diagnostics=(),
                failure=None,
            )

        worker = self._worker(suffix)
        with patch.object(
            DigestPinnedDockerCppBuilder,
            "build",
            autospec=True,
            side_effect=deterministic_success,
        ) as build:
            self.assertTrue(await worker.run_once())
        build.assert_called_once()
        before = (await self.application.get_build(build_id, self.authority.context.actor)).payload
        self.assertEqual(before["status"], "CERTIFIED", before)
        evidence_refs = cast(list[dict[str, object]], before["evidence_refs"])
        self.assertEqual(len(evidence_refs), 1)
        state_before = await self._state()

        connection = await self.database.connect(autocommit=True)
        try:
            with self.assertRaises(psycopg.Error) as evidence_error:
                await connection.execute(
                    """
                    UPDATE yaya_evidence SET payload_sha256=%s
                    WHERE tenant_id=%s AND evidence_id=%s
                    """,
                    (
                        "0" * 64,
                        self.authority.context.actor.tenant_id,
                        evidence_refs[0]["evidence_id"],
                    ),
                )
            self.assertEqual(evidence_error.exception.sqlstate, "55000")
        finally:
            await connection.close()

        after = (await self.application.get_build(build_id, self.authority.context.actor)).payload
        self.assertEqual(after, before)
        self.assertEqual(after["evidence_refs"], evidence_refs)
        self.assertEqual(await self._state(), state_before)


if __name__ == "__main__":
    unittest.main()
