from __future__ import annotations

import asyncio
import hashlib
import json
import stat
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

import psycopg  # noqa: E402
from postgres_test_support import postgres_test_server  # noqa: E402
from psycopg import AsyncConnection  # noqa: E402
from test_agent_backend_skill_build_executor import (  # noqa: E402
    PINNED_GCC_IMAGE,
    PINNED_GCC_VERSION,
    TEST_SUITE_VERSION,
    _AuthorityFixture,  # pyright: ignore[reportPrivateUsage]
    _seed_only_build_authority,  # pyright: ignore[reportPrivateUsage]
)
from yaya_agent_backend.application import HttpAttempt  # noqa: E402
from yaya_agent_backend.database import (  # noqa: E402
    PostgresCommitStateUnknown,
    PostgresDatabase,
)
from yaya_agent_backend.skill_builds import PostgresSkillBuildExecutor  # noqa: E402
from yaya_agent_backend.student_skill_chain import (  # noqa: E402
    StudentSkillChainApplication,
    StudentSkillChainWorker,
)
from yaya_agent_backend.wire import ContractSchemaValidator  # noqa: E402
from yaya_agent_build import (  # noqa: E402
    CPP20_SAFE_V1_PROFILE,
    DigestPinnedDockerCppBuilder,
    DockerBuildResult,
    DockerTestResult,
    validate_source_bundle,
)
from yaya_agent_contracts import CompileAndTestRequest  # noqa: E402


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
            raise PostgresCommitStateUnknown("injected lost final COMMIT acknowledgement")


class _RollbackAfterBodyDatabase(PostgresDatabase):
    """Roll back one transaction after its body published and staged all writes."""

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
                raise psycopg.errors.SerializationFailure(
                    "injected server-confirmed finalization rollback"
                )


class _UnknownAfterRollbackDatabase(PostgresDatabase):
    """Roll back finalization, then report an intentionally unknown outcome."""

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
        try:
            async with super().transaction_with_commit_boundary() as connection:
                yield connection
                if current == self._fail_on_commit and not self.did_fail:
                    raise RuntimeError("injected rollback before unknown outcome")
        except RuntimeError as error:
            if current != self._fail_on_commit or self.did_fail:
                raise
            self.did_fail = True
            raise PostgresCommitStateUnknown(
                "injected unknown finalization outcome after rollback"
            ) from error


def _json_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class PostgresSkillBuildRecoveryCleanupTests(unittest.IsolatedAsyncioTestCase):
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
            prefix="yaya-build-recovery-artifacts-"
        )
        self._workspace_directory = tempfile.TemporaryDirectory(
            prefix="yaya-build-recovery-workspaces-"
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

    def _executor(self, database: PostgresDatabase) -> PostgresSkillBuildExecutor:
        return PostgresSkillBuildExecutor(
            database=database,
            validator=self.validator,
            artifact_root=self.artifact_root,
            workspace_root=self.workspace_root,
            runtime_image=PINNED_GCC_IMAGE,
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

    async def _accept(self, *, suffix: str) -> str:
        source = "int main() { return 0; }\n"
        body: dict[str, object] = {
            "skill_id": f"skill_recovery_{suffix}",
            "display_name": "Build recovery cleanup",
            "client_draft_revision": 7,
            "source_bundle": {
                "language": "CPP20",
                "entrypoint": "main.cpp",
                "files": [
                    {
                        "path": "main.cpp",
                        "content": source,
                        "content_sha256": hashlib.sha256(source.encode()).hexdigest(),
                    }
                ],
            },
            "compiler_profile": CPP20_SAFE_V1_PROFILE,
            "test_suite_version": TEST_SUITE_VERSION,
            "requested_capabilities": ["WORLD_READ"],
        }
        accepted = await self.application.accept_build(
            actor=self.authority.context.actor,
            attempt=HttpAttempt(
                request_id=f"req_build_recovery_{suffix}",
                trace_id=f"trace_build_recovery_{suffix}",
                correlation_id=f"corr_build_recovery_{suffix}",
                requested_at=datetime.now(UTC),
            ),
            idempotency_key=f"idempotency-{suffix}",
            raw_body=_json_bytes(body),
            body=body,
        )
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                "SELECT resource_id FROM yaya_control_jobs WHERE tenant_id=%s AND command_id=%s",
                (self.authority.context.actor.tenant_id, accepted.command.command_id),
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            raise AssertionError("accepted Build job was not found")
        return cast(str, row["resource_id"])

    def _successful_build(
        self,
        builder: DigestPinnedDockerCppBuilder,
        request: CompileAndTestRequest,
    ) -> DockerBuildResult:
        validated = validate_source_bundle(request.source_bundle)
        workspace = self.workspace_root / request.build_id
        workspace.mkdir(parents=True, exist_ok=True)
        staged = workspace / "skill.bin"
        artifact_bytes = f"artifact:{request.build_id}".encode()
        staged.write_bytes(artifact_bytes)
        return DockerBuildResult(
            build_id=request.build_id,
            status="SUCCEEDED",
            source_sha256=validated.source_sha256,
            compiler_profile=request.compiler_profile,
            compiler_version=PINNED_GCC_VERSION,
            test_suite_version=request.test_suite_version,
            build_identity=builder.build_identity(request),
            workspace=workspace,
            staged_artifact=staged,
            artifact_sha256=hashlib.sha256(artifact_bytes).hexdigest(),
            tests=(
                DockerTestResult("public_exact_io_0001", "PUBLIC", "PASSED", ()),
                DockerTestResult("hidden_exact_io_0001", "HIDDEN", "PASSED", ()),
            ),
            diagnostics=(),
            failure=None,
        )

    async def _durable_state(self, build_id: str) -> dict[str, object]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT b.status,b.terminal,j.state AS job_state,c.status AS command_status,
                       (SELECT count(*)::integer FROM yaya_artifacts a
                         WHERE a.build_id=b.build_id) AS artifacts,
                       (SELECT count(*)::integer FROM yaya_skill_certifications sc
                         WHERE sc.build_id=b.build_id) AS certifications
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
            raise AssertionError("Build durable state was not found")
        return dict(row)

    async def test_lost_commit_ack_reconciles_before_workspace_cleanup(self) -> None:
        build_id = await self._accept(suffix="commit_ack")
        database = _PostCommitUnknownDatabase(self.server.dsn, fail_on_commit=2)
        executor = self._executor(database)
        worker = self._worker("recovery-commit-ack-worker", executor)

        with (
            patch.object(
                DigestPinnedDockerCppBuilder,
                "build",
                autospec=True,
                side_effect=self._successful_build,
            ) as build,
            patch.object(
                DigestPinnedDockerCppBuilder,
                "discard_workspace",
                autospec=True,
            ) as discard,
        ):
            self.assertTrue(await worker.run_once())

        self.assertTrue(database.did_fail)
        self.assertEqual(database.commit_count, 2)
        build.assert_called_once()
        discard.assert_called_once()
        self.assertEqual(
            await self._durable_state(build_id),
            {
                "status": "CERTIFIED",
                "terminal": True,
                "job_state": "SUCCEEDED",
                "command_status": "APPLIED",
                "artifacts": 1,
                "certifications": 1,
            },
        )
        self.assertEqual(len([item for item in self.artifact_root.rglob("*") if item.is_file()]), 1)

    async def test_known_rollback_keeps_takeover_workspace_and_deletes_only_zero_ref_orphan(
        self,
    ) -> None:
        build_id = await self._accept(suffix="known_rollback")
        database = _RollbackAfterBodyDatabase(self.server.dsn, fail_on_commit=2)
        executor = self._executor(database)
        worker = self._worker("recovery-rollback-worker", executor)

        with (
            patch.object(
                DigestPinnedDockerCppBuilder,
                "build",
                autospec=True,
                side_effect=self._successful_build,
            ) as build,
            patch.object(
                DigestPinnedDockerCppBuilder,
                "discard_workspace",
                autospec=True,
            ) as discard,
        ):
            self.assertTrue(await worker.run_once())

        self.assertTrue(database.did_fail)
        self.assertEqual(database.commit_count, 3)
        build.assert_called_once()
        discard.assert_not_called()
        self.assertEqual(
            await self._durable_state(build_id),
            {
                "status": "COMPILING",
                "terminal": False,
                "job_state": "LEASED",
                "command_status": "VALIDATING",
                "artifacts": 0,
                "certifications": 0,
            },
        )
        self.assertEqual([item for item in self.artifact_root.rglob("*") if item.is_file()], [])
        self.assertEqual(
            len([item for item in self.workspace_root.rglob("*") if item.is_file()]), 1
        )

    async def test_unknown_uncommitted_outcome_retains_workspace_and_artifact_for_takeover(
        self,
    ) -> None:
        build_id = await self._accept(suffix="unknown_uncommitted")
        database = _UnknownAfterRollbackDatabase(self.server.dsn, fail_on_commit=2)
        executor = self._executor(database)
        worker = self._worker("recovery-unknown-worker", executor)

        with (
            patch.object(
                DigestPinnedDockerCppBuilder,
                "build",
                autospec=True,
                side_effect=self._successful_build,
            ) as build,
            patch.object(
                DigestPinnedDockerCppBuilder,
                "discard_workspace",
                autospec=True,
            ) as discard,
        ):
            self.assertTrue(await worker.run_once())

        self.assertTrue(database.did_fail)
        self.assertEqual(database.commit_count, 2)
        build.assert_called_once()
        discard.assert_not_called()
        self.assertEqual(
            await self._durable_state(build_id),
            {
                "status": "COMPILING",
                "terminal": False,
                "job_state": "LEASED",
                "command_status": "VALIDATING",
                "artifacts": 0,
                "certifications": 0,
            },
        )
        self.assertEqual(len([item for item in self.artifact_root.rglob("*") if item.is_file()]), 1)
        self.assertEqual(
            len([item for item in self.workspace_root.rglob("*") if item.is_file()]), 1
        )


if __name__ == "__main__":
    unittest.main()
