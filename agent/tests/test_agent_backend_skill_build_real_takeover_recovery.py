from __future__ import annotations

import asyncio
import hashlib
import json
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
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
    A8StateFingerprint,
    a8_state_fingerprint,
    missing_a8_business_tables,
)
from postgres_test_support import postgres_test_server  # noqa: E402
from psycopg import AsyncConnection  # noqa: E402
from test_agent_backend_skill_build_executor import (  # noqa: E402
    PINNED_GCC_IMAGE,
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
    CommandResult,
    SubprocessCommandRunner,
)


class _InjectedFinalizationRollback(RuntimeError):
    pass


class _UnknownAfterRollbackDatabase(PostgresDatabase):
    """Roll back one completed transaction body, then make its outcome unknown."""

    def __init__(self, dsn: str, *, fail_on_transaction: int) -> None:
        super().__init__(dsn)
        self._fail_on_transaction = fail_on_transaction
        self.transaction_count = 0
        self.did_fail = False

    @asynccontextmanager
    async def transaction_with_commit_boundary(
        self,
    ) -> AsyncGenerator[AsyncConnection[dict[str, object]]]:
        self.transaction_count += 1
        current = self.transaction_count
        try:
            async with super().transaction_with_commit_boundary() as connection:
                yield connection
                if current == self._fail_on_transaction and not self.did_fail:
                    raise _InjectedFinalizationRollback("injected rollback after finalization body")
        except _InjectedFinalizationRollback as error:
            if current != self._fail_on_transaction or self.did_fail:
                raise
            self.did_fail = True
            raise PostgresCommitStateUnknown(
                "injected unknown finalization outcome after rollback"
            ) from error


class _RecordingCommandRunner:
    """Delegate every command to the real runner while recording Docker starts."""

    def __init__(self, delegate: SubprocessCommandRunner) -> None:
        self._delegate = delegate
        self._lock = threading.Lock()
        self._container_phases: dict[str, str] = {}
        self._started_phases: list[str] = []

    @property
    def started_phases(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._started_phases)

    @staticmethod
    def _option(arguments: tuple[str, ...], name: str) -> str:
        index = arguments.index(name)
        return arguments[index + 1]

    @classmethod
    def _create_phase(cls, arguments: tuple[str, ...]) -> tuple[str, str]:
        container_name = cls._option(arguments, "--name")
        entrypoint = cls._option(arguments, "--entrypoint")
        if entrypoint == "g++":
            phase = "COMPILER_VERSION"
        elif entrypoint == "/bin/sh":
            phase = "COMPILE"
        elif entrypoint == "/opt/yaya/skill" and "public-argument" in arguments:
            phase = "PUBLIC_TEST"
        elif entrypoint == "/opt/yaya/skill" and "hidden-argument" in arguments:
            phase = "HIDDEN_TEST"
        else:
            phase = f"UNEXPECTED:{entrypoint}"
        return container_name, phase

    def run(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        max_output_bytes: int,
        input_bytes: bytes | None = None,
    ) -> CommandResult:
        arguments = tuple(command)
        if len(arguments) >= 2:
            with self._lock:
                if arguments[1] == "create":
                    container_name, phase = self._create_phase(arguments)
                    self._container_phases[container_name] = phase
                elif arguments[1] == "start":
                    container_name = arguments[-1]
                    self._started_phases.append(
                        self._container_phases.get(container_name, f"UNKNOWN:{container_name}")
                    )
        return self._delegate.run(
            arguments,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            input_bytes=input_bytes,
        )


@dataclass(frozen=True, slots=True)
class _ArtifactFileFingerprint:
    path: str
    inode: int
    size_bytes: int
    mode: int
    modified_ns: int
    sha256: str


def _artifact_file_fingerprint(path: Path) -> _ArtifactFileFingerprint:
    resolved = path.resolve(strict=True)
    payload = resolved.read_bytes()
    metadata = resolved.stat()
    return _ArtifactFileFingerprint(
        path=str(resolved),
        inode=metadata.st_ino,
        size_bytes=metadata.st_size,
        mode=metadata.st_mode,
        modified_ns=metadata.st_mtime_ns,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _json_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class RealPinnedBuildTakeoverRecoveryTests(unittest.IsolatedAsyncioTestCase):
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
            prefix="yaya-real-takeover-artifacts-"
        )
        self._workspace_directory = tempfile.TemporaryDirectory(
            prefix="yaya-real-takeover-workspaces-"
        )
        self.artifact_root = Path(self._artifact_directory.name).resolve()
        self.workspace_root = Path(self._workspace_directory.name).resolve()

    async def asyncTearDown(self) -> None:
        for root in (self.artifact_root, self.workspace_root):
            for candidate in root.rglob("*"):
                if candidate.is_file() and not candidate.is_symlink():
                    candidate.chmod(stat.S_IWRITE | stat.S_IREAD)
        self._workspace_directory.cleanup()
        self._artifact_directory.cleanup()

    def _application(self) -> StudentSkillChainApplication:
        return StudentSkillChainApplication(
            self.database,
            self.validator,
            self.authority.versions,
            artifact_root=self.artifact_root,
        )

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
        application: StudentSkillChainApplication,
        executor: PostgresSkillBuildExecutor,
    ) -> StudentSkillChainWorker:
        return StudentSkillChainWorker(
            database=self.database,
            application=application,
            validator=self.validator,
            worker_id=worker_id,
            artifact_root=self.artifact_root,
            lease_seconds=120,
            build_executor=executor,
        )

    @staticmethod
    def _body() -> dict[str, object]:
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
        return {
            "skill_id": "skill_real_takeover_recovery_0001",
            "display_name": "Real pinned Docker takeover recovery",
            "client_draft_revision": 23,
            "source_bundle": {
                "language": "CPP20",
                "entrypoint": "main.cpp",
                "files": [
                    {
                        "path": "main.cpp",
                        "content": source,
                        "content_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                    }
                ],
            },
            "compiler_profile": CPP20_SAFE_V1_PROFILE,
            "test_suite_version": TEST_SUITE_VERSION,
            "requested_capabilities": ["WORLD_READ"],
        }

    @staticmethod
    def _attempt(suffix: str) -> HttpAttempt:
        return HttpAttempt(
            request_id=f"req_real_takeover_{suffix}",
            trace_id=f"trace_real_takeover_{suffix}",
            correlation_id=f"corr_real_takeover_{suffix}",
            requested_at=datetime.now(UTC),
        )

    async def _accept(
        self,
        application: StudentSkillChainApplication,
        body: dict[str, object],
        raw_body: bytes,
        idempotency_key: str,
    ) -> tuple[str, str]:
        accepted = await application.accept_build(
            actor=self.authority.context.actor,
            attempt=self._attempt("accept_0001"),
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
        if row is None:
            raise AssertionError("accepted Build job was not found")
        return cast(str, row["resource_id"]), accepted.command.command_id

    async def _durable_closure(self, build_id: str) -> dict[str, object]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT b.status,b.terminal,j.state AS job_state,j.phase AS job_phase,
                       j.attempt,j.fencing_token,c.status AS command_status,
                       (SELECT array_agg(h.status ORDER BY h.sequence)
                          FROM yaya_skill_build_history h
                         WHERE h.tenant_id=b.tenant_id AND h.build_id=b.build_id)
                         AS history_statuses,
                       (SELECT count(*)::integer FROM yaya_build_step_receipts r
                         WHERE r.tenant_id=b.tenant_id AND r.build_id=b.build_id)
                         AS build_receipts,
                       (SELECT count(*)::integer FROM yaya_artifacts) AS artifacts,
                       (SELECT count(*)::integer FROM yaya_skills) AS skill_versions,
                       (SELECT count(*)::integer FROM yaya_skill_certifications)
                         AS certifications,
                       (SELECT count(*)::integer FROM yaya_registry_certifications)
                         AS legacy_certifications,
                       (SELECT count(*)::integer FROM yaya_compile_results) AS compile_results,
                       (SELECT count(*)::integer FROM yaya_evidence) AS evidence
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
            raise AssertionError("Build durable closure was not found")
        return dict(row)

    async def _expire_lease(self, build_id: str) -> None:
        connection = await self.database.connect(autocommit=True)
        try:
            updated = await connection.execute(
                """
                UPDATE yaya_control_jobs
                   SET heartbeat_at=clock_timestamp() - interval '2 seconds',
                       lease_expires_at=clock_timestamp() - interval '1 second'
                 WHERE tenant_id=%s AND resource_id=%s AND state='LEASED'
                """,
                (self.authority.context.actor.tenant_id, build_id),
            )
        finally:
            await connection.close()
        self.assertEqual(updated.rowcount, 1)

    async def _fingerprint(self) -> A8StateFingerprint:
        fingerprint = await a8_state_fingerprint(self.database)
        self.assertEqual(missing_a8_business_tables(fingerprint), ())
        return fingerprint

    def _only_artifact_file(self) -> Path:
        files = [candidate for candidate in self.artifact_root.rglob("*") if candidate.is_file()]
        self.assertEqual(len(files), 1, files)
        return files[0]

    async def test_real_receipts_and_cas_are_reconciled_by_lease_takeover(self) -> None:
        body = self._body()
        raw_body = _json_bytes(body)
        idempotency_key = "idem-real-takeover-recovery-0001"
        first_application = self._application()
        build_id, command_id = await self._accept(
            first_application,
            body,
            raw_body,
            idempotency_key,
        )
        fault_database = _UnknownAfterRollbackDatabase(
            self.server.dsn,
            fail_on_transaction=2,
        )
        first_executor = self._executor(fault_database)
        first_worker = self._worker(
            "real-takeover-first-worker-0001",
            first_application,
            first_executor,
        )
        recording_runner = _RecordingCommandRunner(SubprocessCommandRunner())

        # This constructor substitution changes no Docker result or behavior:
        # every call is delegated to the production subprocess runner.
        with patch(
            "yaya_agent_build.pipeline.SubprocessCommandRunner",
            return_value=recording_runner,
        ) as runner_constructor:
            self.assertTrue(await first_worker.run_once())
            self.assertTrue(fault_database.did_fail)
            self.assertEqual(fault_database.transaction_count, 2)
            first_runner_constructions = runner_constructor.call_count
            self.assertGreaterEqual(first_runner_constructions, 1)
            self.assertEqual(
                recording_runner.started_phases,
                ("COMPILER_VERSION", "COMPILE", "PUBLIC_TEST", "HIDDEN_TEST"),
            )
            self.assertEqual(
                await self._durable_closure(build_id),
                {
                    "status": "COMPILING",
                    "terminal": False,
                    "job_state": "LEASED",
                    "job_phase": "COMPILE",
                    "attempt": 1,
                    "fencing_token": 1,
                    "command_status": "VALIDATING",
                    "history_statuses": ["ACCEPTED", "COMPILING"],
                    "build_receipts": 0,
                    "artifacts": 0,
                    "skill_versions": 0,
                    "certifications": 0,
                    "legacy_certifications": 0,
                    "compile_results": 0,
                    "evidence": 0,
                },
            )

            workspaces = [
                candidate for candidate in self.workspace_root.iterdir() if candidate.is_dir()
            ]
            self.assertEqual(len(workspaces), 1, workspaces)
            filesystem_receipts = sorted((workspaces[0] / "receipts").glob("*.json"))
            self.assertEqual(len(filesystem_receipts), 4)
            for receipt in filesystem_receipts:
                self.assertTrue(receipt.is_file())
                self.assertFalse(receipt.is_symlink())
                self.assertEqual(receipt.stat().st_mode & 0o222, 0)

            artifact_path = self._only_artifact_file()
            self.assertFalse(artifact_path.is_symlink())
            self.assertEqual(artifact_path.stat().st_mode & 0o222, 0)
            self.assertEqual(artifact_path.parent.name, artifact_path.name[:2])
            artifact_before_takeover = _artifact_file_fingerprint(artifact_path)
            self.assertEqual(artifact_before_takeover.sha256, artifact_path.name)

            await self._expire_lease(build_id)
            takeover_application = self._application()
            takeover_executor = self._executor(self.database)
            takeover_worker = self._worker(
                "real-takeover-second-worker-0001",
                takeover_application,
                takeover_executor,
            )
            self.assertIsNot(takeover_executor, first_executor)
            self.assertIsNot(takeover_worker, first_worker)
            starts_before_takeover = recording_runner.started_phases
            self.assertTrue(await takeover_worker.run_once())
            self.assertGreater(runner_constructor.call_count, first_runner_constructions)
            takeover_runner_constructions = runner_constructor.call_count
            self.assertEqual(recording_runner.started_phases, starts_before_takeover)

            resource = (
                await takeover_application.get_build(
                    build_id,
                    self.authority.context.actor,
                )
            ).payload
            self.assertEqual(resource["status"], "CERTIFIED", resource)
            self.assertIs(resource["terminal"], True)
            self.assertIsNone(resource["failure"])
            self.assertEqual(
                await self._durable_closure(build_id),
                {
                    "status": "CERTIFIED",
                    "terminal": True,
                    "job_state": "SUCCEEDED",
                    "job_phase": "COMPLETE",
                    "attempt": 2,
                    "fencing_token": 2,
                    "command_status": "APPLIED",
                    "history_statuses": ["ACCEPTED", "COMPILING", "CERTIFIED"],
                    "build_receipts": 5,
                    "artifacts": 1,
                    "skill_versions": 1,
                    "certifications": 1,
                    "legacy_certifications": 1,
                    "compile_results": 1,
                    "evidence": 1,
                },
            )
            self.assertEqual(list(self.workspace_root.iterdir()), [])
            self.assertEqual(self._only_artifact_file(), artifact_path)
            self.assertEqual(
                _artifact_file_fingerprint(artifact_path),
                artifact_before_takeover,
            )

            stable_database = await self._fingerprint()
            stable_artifact = _artifact_file_fingerprint(artifact_path)
            stable_starts = recording_runner.started_phases
            restarted_application = self._application()
            replay = await restarted_application.accept_build(
                actor=self.authority.context.actor,
                attempt=self._attempt("replay_0002"),
                idempotency_key=idempotency_key,
                raw_body=raw_body,
                body=body,
            )
            self.assertTrue(replay.replayed)
            self.assertEqual(replay.command.command_id, command_id)
            restarted_executor = self._executor(self.database)
            restarted_worker = self._worker(
                "real-takeover-restarted-worker-0001",
                restarted_application,
                restarted_executor,
            )
            self.assertFalse(await restarted_worker.run_once())
            self.assertEqual(runner_constructor.call_count, takeover_runner_constructions)
            self.assertEqual(recording_runner.started_phases, stable_starts)
            self.assertEqual(await self._fingerprint(), stable_database)
            self.assertEqual(_artifact_file_fingerprint(artifact_path), stable_artifact)


if __name__ == "__main__":
    unittest.main()
