from __future__ import annotations

import asyncio
import hashlib
import json
import stat
import sys
import tempfile
import unittest
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import LiteralString, cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
CONTRACTS_ROOT = Path(__file__).resolve().parents[1] / "contracts"
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from agent_runtime_fixtures import make_operation, make_versions  # noqa: E402
from postgres_test_support import postgres_test_server  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from test_agent_backend_student_skill_chain_surfaces import (  # noqa: E402
    AGENT_PROFILE_ID,
    BUILD_ID,
    CERTIFICATION_ID,
    SKILL_VERSION_ID,
    WORLD_ID,
    _CanonicalAuthority,  # pyright: ignore[reportPrivateUsage]
    _seed_canonical_launch_authority,  # pyright: ignore[reportPrivateUsage]
    _seed_pre_certified_skill_closure,  # pyright: ignore[reportPrivateUsage]
)
from yaya_agent_backend.application import AgentTurnApplication  # noqa: E402
from yaya_agent_backend.auth import JwtAuthenticator  # noqa: E402
from yaya_agent_backend.database import PostgresDatabase  # noqa: E402
from yaya_agent_backend.http_api import AgentHttpApi, HttpResponse  # noqa: E402
from yaya_agent_backend.student_skill_chain import (  # noqa: E402
    StudentSkillChainApplication,
    StudentSkillChainWorker,
)
from yaya_agent_backend.wire import ContractSchemaValidator  # noqa: E402
from yaya_agent_contracts import canonical_json_sha256  # noqa: E402


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class CertificationCorruptionMatrixTests(unittest.IsolatedAsyncioTestCase):
    """Persisted corruption must fail closed on both public consumption paths."""

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
                TRUNCATE
                    yaya_learner_projection_terminal_audits,
                    yaya_learner_projection_failures,
                    yaya_learner_projection_receipts,
                    yaya_learner_projection_job_evidence,
                    yaya_learner_projection_jobs,
                    yaya_skill_activations,yaya_registry_entries,yaya_registry_heads,
                    yaya_session_skill_versions,yaya_certification_revocations,
                    yaya_skill_certifications,yaya_artifacts,yaya_build_step_receipts,
                    yaya_skill_build_history,yaya_skill_builds,yaya_build_policies,
                    yaya_product_write_receipts,yaya_skill_draft_heads,
                    yaya_skill_draft_revisions,yaya_control_jobs,
                    yaya_public_agent_sessions,yaya_launch_authorities,
                    yaya_agent_profiles,yaya_learners,yaya_projection_outbox,
                    yaya_agent_interactions,yaya_agent_turns,yaya_command_jobs,
                    yaya_agent_traces,yaya_agent_messages,yaya_skill_invocations,
                    yaya_counterexamples,yaya_runs,yaya_registry_active,
                    yaya_registry_certifications,yaya_skills,yaya_compile_results,
                    yaya_evidence,yaya_events,yaya_outbox,yaya_audit,
                    yaya_learner_models,yaya_commands,yaya_agent_sessions,
                    yaya_worlds,yaya_tasks CASCADE
                """
            )
        finally:
            await connection.close()

        self.context = make_operation()
        self.versions = make_versions()
        self.validator = ContractSchemaValidator(CONTRACTS_ROOT)
        self.authority: _CanonicalAuthority = await _seed_canonical_launch_authority(
            self.database,
            self.context,
            self.versions,
        )
        self._artifact_directory = tempfile.TemporaryDirectory(
            prefix="yaya-certification-corruption-"
        )
        self.artifact_root = Path(self._artifact_directory.name).resolve()
        self.chain = StudentSkillChainApplication(
            self.database,
            self.validator,
            self.versions,
            artifact_root=self.artifact_root,
        )
        self.authenticator = JwtAuthenticator(
            hmac_secret="certification-corruption-secret-0000000000000000",
            issuer="yaya-certification-corruption-matrix",
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
            worker_id="certification-corruption-worker",
            artifact_root=self.artifact_root,
        )
        await _seed_pre_certified_skill_closure(
            self.database,
            self.validator,
            self.authority,
            self.artifact_root,
        )

        # This is only a negative-test control that prevents an already-invalid
        # fixture from making every corruption case a false positive.  It is not
        # used as Build-production success evidence.
        control = await self.http.handle(
            "GET",
            f"/v1/skill-builds/{BUILD_ID}",
            self._get_headers(suffix="uncorrupted_control"),
        )
        self.assertEqual(control.status, 200, control.body)

        accepted, payload = await self._post_activation()
        self.assertEqual(accepted.status, 202, accepted.body)
        self.activation_command_id = cast(str, payload["command_id"])

    async def asyncTearDown(self) -> None:
        for candidate in self.artifact_root.rglob("*"):
            if candidate.is_file() and not candidate.is_symlink():
                candidate.chmod(stat.S_IWRITE | stat.S_IREAD)
        self._artifact_directory.cleanup()

    def _get_headers(self, *, suffix: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "X-Schema-Version": "1.0.0",
            "X-Request-Id": f"req_cert_corruption_{suffix}",
            "X-Trace-Id": f"trace_cert_corruption_{suffix}",
            "X-Correlation-Id": f"corr_cert_corruption_{suffix}",
        }

    def _post_headers(
        self,
        raw: bytes,
        *,
        suffix: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        return {
            **self._get_headers(suffix=suffix),
            "Idempotency-Key": idempotency_key,
            "Content-Type": "application/json",
            "Content-Length": str(len(raw)),
        }

    async def _post_activation(self) -> tuple[HttpResponse, dict[str, object]]:
        body: dict[str, object] = {
            "expected_registry_revision": 0,
            "activation_scope": {
                "world_id": WORLD_ID,
                "agent_profile_id": AGENT_PROFILE_ID,
            },
            "reason": "Exercise fail-closed Certification corruption handling.",
        }
        raw = _json_bytes(body)
        response = await self.http.handle(
            "POST",
            f"/v1/skill-versions/{SKILL_VERSION_ID}/activations",
            self._post_headers(
                raw,
                suffix="activation_accept",
                idempotency_key="certification-corruption-activation-0001",
            ),
            raw,
        )
        return response, cast(dict[str, object], json.loads(response.body))

    async def _read_json(
        self,
        query: LiteralString,
        params: Sequence[object],
    ) -> dict[str, object]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(query, params)
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None or not isinstance(row.get("value"), dict):
            self.fail("corruption target JSON disappeared")
        return cast(dict[str, object], row["value"])

    async def _inject(
        self,
        statements: Sequence[tuple[LiteralString, Sequence[object]]],
    ) -> None:
        """Commit corruption with trigger guards bypassed only in this transaction."""

        async with self.database.transaction_with_commit_boundary() as connection:
            await connection.execute("SET LOCAL session_replication_role = replica")
            for query, params in statements:
                updated = await connection.execute(query, params)
                self.assertEqual(updated.rowcount, 1)

        # SET LOCAL is transaction-scoped.  Prove the next production-facing
        # connection sees all migration guards in their normal origin mode.
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute("SHOW session_replication_role")
            row = await cursor.fetchone()
        finally:
            await connection.close()
        self.assertEqual(row, {"session_replication_role": "origin"})

    async def _mutate_terminal_build(
        self,
        mutate: Callable[[dict[str, object]], None],
    ) -> None:
        resource = await self._read_json(
            """
            SELECT resource_json AS value FROM yaya_skill_builds
            WHERE tenant_id=%s AND build_id=%s
            """,
            (self.context.actor.tenant_id, BUILD_ID),
        )
        mutate(resource)
        self.validator.validate("schemas/game/skill-build.schema.json", resource)
        digest = canonical_json_sha256(resource)
        await self._inject(
            (
                (
                    """
                    UPDATE yaya_skill_builds
                    SET resource_json=%s,resource_sha256=%s
                    WHERE tenant_id=%s AND build_id=%s
                    """,
                    (
                        Jsonb(resource),
                        digest,
                        self.context.actor.tenant_id,
                        BUILD_ID,
                    ),
                ),
                (
                    """
                    UPDATE yaya_skill_build_history
                    SET record_json=%s,record_sha256=%s
                    WHERE tenant_id=%s AND build_id=%s AND sequence=3
                    """,
                    (
                        Jsonb(resource),
                        digest,
                        self.context.actor.tenant_id,
                        BUILD_ID,
                    ),
                ),
            )
        )

    @staticmethod
    def _phase(resource: Mapping[str, object], name: str) -> dict[str, object]:
        phases = resource.get("phases")
        if not isinstance(phases, list):
            raise AssertionError("Build phases disappeared")
        matching: list[dict[str, object]] = []
        for raw_phase in cast(list[object], phases):
            if not isinstance(raw_phase, dict):
                continue
            phase = cast(dict[str, object], raw_phase)
            if phase.get("name") == name:
                matching.append(phase)
        if len(matching) != 1:
            raise AssertionError(f"Build phase {name} did not resolve exactly once")
        return matching[0]

    async def _business_fingerprint(self) -> str:
        """Hash every yaya business table plus the Artifact filesystem.

        Command/control rows are excluded because a fail-closed durable worker
        must terminalize its already-accepted job.  Every other persisted or
        filesystem side effect is included.
        """

        snapshot: dict[str, object] = {}
        connection = await self.database.connect(autocommit=True)
        try:
            table_cursor = await connection.execute(
                """
                SELECT tablename FROM pg_tables
                WHERE schemaname=current_schema()
                  AND tablename LIKE 'yaya_%'
                  AND tablename NOT IN (
                      'yaya_commands','yaya_control_jobs','yaya_schema_migrations'
                  )
                ORDER BY tablename
                """
            )
            table_rows = await table_cursor.fetchall()
            for table_row in table_rows:
                table = cast(str, table_row["tablename"])
                cursor = await connection.execute(
                    sql.SQL(
                        """
                        SELECT COALESCE(
                            jsonb_agg(
                                to_jsonb(candidate)
                                ORDER BY to_jsonb(candidate)::text
                            ),
                            '[]'::jsonb
                        ) AS rows
                        FROM {} AS candidate
                        """
                    ).format(sql.Identifier(table))
                )
                row = await cursor.fetchone()
                if row is None:
                    self.fail(f"fingerprint query for {table} returned no row")
                snapshot[table] = row["rows"]
        finally:
            await connection.close()

        files: list[dict[str, object]] = []
        for path in sorted(
            (candidate for candidate in self.artifact_root.rglob("*") if candidate.is_file()),
            key=lambda candidate: candidate.as_posix(),
        ):
            metadata = path.lstat()
            files.append(
                {
                    "path": path.relative_to(self.artifact_root).as_posix(),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "size": metadata.st_size,
                    "mode": stat.S_IMODE(metadata.st_mode),
                    "symlink": path.is_symlink(),
                }
            )
        snapshot["artifact_files"] = files
        return hashlib.sha256(_json_bytes(snapshot)).hexdigest()

    async def _job_row(self) -> dict[str, object]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT j.state,j.attempt,j.last_error_code,j.result_json,
                       c.status AS command_status
                FROM yaya_control_jobs j
                JOIN yaya_commands c
                  ON c.tenant_id=j.tenant_id AND c.command_id=j.command_id
                WHERE j.tenant_id=%s AND j.command_id=%s
                """,
                (self.context.actor.tenant_id, self.activation_command_id),
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            self.fail("accepted Activation job disappeared")
        return row

    async def _registry_counts(self) -> dict[str, int]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM yaya_registry_heads)::integer AS heads,
                  (SELECT count(*) FROM yaya_registry_entries)::integer AS entries,
                  (SELECT count(*) FROM yaya_skill_activations)::integer AS activations,
                  (SELECT count(*) FROM yaya_registry_active)::integer AS legacy_active
                """
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            self.fail("registry fingerprint query returned no row")
        return {key: cast(int, value) for key, value in row.items()}

    async def _assert_fail_closed(self, *, label: str) -> None:
        corrupted = await self._business_fingerprint()

        response = await self.http.handle(
            "GET",
            f"/v1/skill-builds/{BUILD_ID}",
            self._get_headers(suffix=f"{label}_get"),
        )
        payload = cast(dict[str, object], json.loads(response.body))
        after_get = await self._business_fingerprint()

        handled = await self.worker.run_once()
        terminal = await self._job_row()
        after_activation = await self._business_fingerprint()

        self.assertTrue(handled)
        self.assertEqual(response.status, 500, response.body)
        self.assertEqual(
            cast(dict[str, object], payload["error"])["code"],
            "INVARIANT_VIOLATION",
        )
        self.assertEqual(after_get, corrupted, "GET repaired or mutated persisted authority")
        self.assertEqual(terminal["state"], "FAILED")
        self.assertEqual(terminal["command_status"], "FAILED")
        self.assertIn(
            terminal["last_error_code"],
            {"INVARIANT_VIOLATION", "INTERNAL_ERROR"},
        )
        self.assertIsNone(terminal["result_json"])
        self.assertEqual(
            after_activation,
            corrupted,
            "Activation repaired corruption or emitted a business side effect",
        )
        self.assertEqual(
            await self._registry_counts(),
            {"heads": 0, "entries": 0, "activations": 0, "legacy_active": 0},
        )
        self.assertFalse(await self.worker.run_once())

    async def test_build_snapshot_identity_corruption_fails_closed(self) -> None:
        def corrupt(resource: dict[str, object]) -> None:
            context = cast(dict[str, object], resource["request_context"])
            context["request_id"] = "req_corrupted_build_snapshot_0001"

        await self._mutate_terminal_build(corrupt)
        await self._assert_fail_closed(label="build_snapshot")

    async def test_build_phase_corruption_fails_closed(self) -> None:
        def corrupt(resource: dict[str, object]) -> None:
            self._phase(resource, "COMPILE")["status"] = "SKIPPED"

        await self._mutate_terminal_build(corrupt)
        await self._assert_fail_closed(label="build_phase")

    async def test_build_diagnostic_corruption_fails_closed(self) -> None:
        def corrupt(resource: dict[str, object]) -> None:
            self._phase(resource, "HIDDEN_TEST")["diagnostic_codes"] = ["CORRUPTED_DIAGNOSTIC"]

        await self._mutate_terminal_build(corrupt)
        await self._assert_fail_closed(label="build_diagnostic")

    async def test_step_receipt_identity_corruption_fails_closed(self) -> None:
        receipt = await self._read_json(
            """
            SELECT receipt_json AS value FROM yaya_build_step_receipts
            WHERE tenant_id=%s AND build_id=%s AND step='CERTIFY' AND attempt=1
            """,
            (self.context.actor.tenant_id, BUILD_ID),
        )
        receipt["build_identity"] = "f" * 64
        await self._inject(
            (
                (
                    """
                    UPDATE yaya_build_step_receipts
                    SET receipt_json=%s,output_sha256=%s
                    WHERE tenant_id=%s AND build_id=%s
                      AND step='CERTIFY' AND attempt=1
                    """,
                    (
                        Jsonb(receipt),
                        canonical_json_sha256(receipt),
                        self.context.actor.tenant_id,
                        BUILD_ID,
                    ),
                ),
            )
        )
        await self._assert_fail_closed(label="step_receipt")

    async def test_evidence_ownership_corruption_fails_closed(self) -> None:
        evidence = await self._read_json(
            """
            SELECT evidence_json AS value FROM yaya_evidence
            WHERE tenant_id=%s
              AND evidence_json #>> '{source,source_id}'=%s
            """,
            (self.context.actor.tenant_id, BUILD_ID),
        )
        subject = cast(dict[str, object], evidence["subject"])
        subject["learner_id"] = "learner_corrupted_owner_0001"
        await self._inject(
            (
                (
                    """
                    UPDATE yaya_evidence SET evidence_json=%s
                    WHERE tenant_id=%s
                      AND evidence_json #>> '{source,source_id}'=%s
                    """,
                    (Jsonb(evidence), self.context.actor.tenant_id, BUILD_ID),
                ),
            )
        )
        await self._assert_fail_closed(label="evidence_ownership")

    async def test_build_test_suite_version_corruption_fails_closed(self) -> None:
        def corrupt(resource: dict[str, object]) -> None:
            versions = cast(dict[str, object], resource["versions"])
            versions["test_suite_version"] = "watering-corrupted-9"

        await self._mutate_terminal_build(corrupt)
        await self._assert_fail_closed(label="test_suite_version")

    async def test_legacy_certification_rejection_corruption_fails_closed(self) -> None:
        await self._inject(
            (
                (
                    """
                    UPDATE yaya_registry_certifications SET rejected=TRUE
                    WHERE tenant_id=%s AND certification_id=%s
                    """,
                    (self.context.actor.tenant_id, CERTIFICATION_ID),
                ),
            )
        )
        await self._assert_fail_closed(label="legacy_rejected")

    async def test_compile_result_mirror_corruption_fails_closed(self) -> None:
        snapshot = await self._read_json(
            """
            SELECT snapshot_json AS value FROM yaya_compile_results
            WHERE tenant_id=%s AND build_id=%s
            """,
            (self.context.actor.tenant_id, BUILD_ID),
        )
        snapshot["build_id"] = "build_corrupted_compile_mirror_0001"
        await self._inject(
            (
                (
                    """
                    UPDATE yaya_compile_results SET snapshot_json=%s
                    WHERE tenant_id=%s AND build_id=%s
                    """,
                    (Jsonb(snapshot), self.context.actor.tenant_id, BUILD_ID),
                ),
            )
        )
        await self._assert_fail_closed(label="compile_mirror")


if __name__ == "__main__":
    unittest.main()
