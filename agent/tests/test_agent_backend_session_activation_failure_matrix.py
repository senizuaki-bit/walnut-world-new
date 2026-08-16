from __future__ import annotations

import asyncio
import hashlib
import json
import stat
import sys
import tempfile
import unittest
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import LiteralString, cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
CONTRACTS_ROOT = Path(__file__).resolve().parents[1] / "contracts"
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

import psycopg  # noqa: E402
from a8_state_fingerprint import (  # noqa: E402
    A8StateFingerprint,
    a8_state_fingerprint,
    fingerprint_without,
    missing_a8_business_tables,
)
from agent_runtime_fixtures import TASK_ID, WORLD_ID, make_operation, make_versions  # noqa: E402
from postgres_test_support import postgres_test_server  # noqa: E402
from psycopg import AsyncConnection  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from test_agent_backend_student_skill_chain_surfaces import (  # noqa: E402
    AGENT_PROFILE_ID,
    BUILD_ID,
    CERTIFICATION_ID,
    SKILL_VERSION_ID,
    _CanonicalAuthority,  # pyright: ignore[reportPrivateUsage]
    _CertifiedFixture,  # pyright: ignore[reportPrivateUsage]
    _seed_canonical_launch_authority,  # pyright: ignore[reportPrivateUsage]
    _seed_pre_certified_skill_closure,  # pyright: ignore[reportPrivateUsage]
)
from yaya_agent_backend.application import (  # noqa: E402
    AgentTurnApplication,
    BackendApplicationError,
)
from yaya_agent_backend.auth import JwtAuthenticator  # noqa: E402
from yaya_agent_backend.database import (  # noqa: E402
    PostgresCommitStateUnknown,
    PostgresDatabase,
)
from yaya_agent_backend.http_api import AgentHttpApi, HttpResponse  # noqa: E402
from yaya_agent_backend.student_skill_chain import (  # noqa: E402
    StudentSkillChainApplication,
    StudentSkillChainWorker,
)
from yaya_agent_backend.wire import ContractSchemaValidator  # noqa: E402
from yaya_agent_contracts import (  # noqa: E402
    ActorRef,
    ActorType,
    canonical_json_sha256,
)


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
            raise PostgresCommitStateUnknown("injected lost COMMIT acknowledgement")


def _json_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


_CONTROL_LEDGER_TABLES = frozenset({"yaya_commands", "yaya_control_jobs"})
_ArtifactTreeFingerprint = tuple[tuple[str, str, int, int, int, str], ...]
_BusinessFingerprint = tuple[A8StateFingerprint, _ArtifactTreeFingerprint]


class SessionActivationFailureMatrixTests(unittest.IsolatedAsyncioTestCase):
    """Provider- and Docker-independent public Session/Activation failure matrix."""

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
            prefix="yaya-session-activation-matrix-"
        )
        self.artifact_root = Path(self._artifact_directory.name).resolve()
        self.authenticator = JwtAuthenticator(
            hmac_secret="session-activation-matrix-secret-0000000000000000",
            issuer="yaya-session-activation-matrix",
            audience="yaya-agent-test",
        )
        self.token = self.authenticator.issue_for_test(
            self.context.actor,
            now=datetime.now(UTC),
        )
        self.chain, self.http, self.worker = self._surfaces(
            self.database,
            worker_id="session-activation-matrix-worker",
        )

    async def asyncTearDown(self) -> None:
        for candidate in self.artifact_root.rglob("*"):
            if candidate.is_file() and not candidate.is_symlink():
                candidate.chmod(stat.S_IWRITE | stat.S_IREAD)
        self._artifact_directory.cleanup()

    def _surfaces(
        self,
        database: PostgresDatabase,
        *,
        worker_id: str,
        lease_seconds: int = 120,
    ) -> tuple[StudentSkillChainApplication, AgentHttpApi, StudentSkillChainWorker]:
        chain = StudentSkillChainApplication(
            database,
            self.validator,
            self.versions,
            artifact_root=self.artifact_root,
        )
        http = AgentHttpApi(
            application=AgentTurnApplication(database, CONTRACTS_ROOT, self.versions),
            authenticator=self.authenticator,
            validator=self.validator,
            student_chain=chain,
        )
        worker = StudentSkillChainWorker(
            database=database,
            application=chain,
            validator=self.validator,
            worker_id=worker_id,
            artifact_root=self.artifact_root,
            lease_seconds=lease_seconds,
        )
        return chain, http, worker

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
            "X-Request-Id": f"req_matrix_{suffix}",
            "X-Trace-Id": f"trace_matrix_{suffix}",
            "X-Correlation-Id": f"corr_matrix_{suffix}",
            "Idempotency-Key": idempotency_key,
            "Content-Type": "application/json",
            "Content-Length": str(len(raw_body)),
        }

    def _get_headers(self, *, suffix: str, token: str | None = None) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token or self.token}",
            "X-Schema-Version": "1.0.0",
            "X-Request-Id": f"req_matrix_{suffix}",
            "X-Trace-Id": f"trace_matrix_{suffix}",
            "X-Correlation-Id": f"corr_matrix_{suffix}",
        }

    async def _post(
        self,
        target: str,
        body: dict[str, object],
        *,
        suffix: str,
        idempotency_key: str,
        token: str | None = None,
        http: AgentHttpApi | None = None,
    ) -> tuple[HttpResponse, dict[str, object]]:
        raw = _json_bytes(body)
        response = await (http or self.http).handle(
            "POST",
            target,
            self._headers(
                raw,
                suffix=suffix,
                idempotency_key=idempotency_key,
                token=token,
            ),
            raw,
        )
        return response, cast(dict[str, object], json.loads(response.body))

    @staticmethod
    def _error_code(payload: dict[str, object]) -> str:
        return cast(str, cast(dict[str, object], payload["error"])["code"])

    @staticmethod
    def _activation_body(*, revision: int = 0) -> dict[str, object]:
        return {
            "expected_registry_revision": revision,
            "activation_scope": {
                "world_id": WORLD_ID,
                "agent_profile_id": AGENT_PROFILE_ID,
            },
        }

    async def _seed_certified(self) -> _CertifiedFixture:
        return await _seed_pre_certified_skill_closure(
            self.database,
            self.validator,
            self.authority,
            self.artifact_root,
        )

    def _artifact_tree_fingerprint(self) -> _ArtifactTreeFingerprint:
        entries: list[tuple[str, str, int, int, int, str]] = []
        for candidate in sorted(
            self.artifact_root.rglob("*"),
            key=lambda path: path.relative_to(self.artifact_root).as_posix(),
        ):
            relative = candidate.relative_to(self.artifact_root).as_posix()
            metadata = candidate.lstat()
            mode = stat.S_IMODE(metadata.st_mode)
            if candidate.is_symlink():
                entries.append(
                    (
                        relative,
                        "symlink",
                        mode,
                        metadata.st_size,
                        metadata.st_mtime_ns,
                        str(candidate.readlink()),
                    )
                )
            elif candidate.is_dir():
                entries.append((relative, "directory", mode, 0, metadata.st_mtime_ns, ""))
            else:
                entries.append(
                    (
                        relative,
                        "file",
                        mode,
                        metadata.st_size,
                        metadata.st_mtime_ns,
                        hashlib.sha256(candidate.read_bytes()).hexdigest(),
                    )
                )
        return tuple(entries)

    async def _business_fingerprint(self) -> _BusinessFingerprint:
        fingerprint = await a8_state_fingerprint(self.database)
        self.assertEqual(missing_a8_business_tables(fingerprint), ())
        return (
            fingerprint_without(fingerprint, _CONTROL_LEDGER_TABLES),
            self._artifact_tree_fingerprint(),
        )

    async def _job_row(self, command_id: str) -> dict[str, object]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT j.*,c.status AS command_status,c.revision AS command_revision
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

    async def _command_job_counts(self) -> tuple[int, int]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT (SELECT count(*) FROM yaya_commands) AS commands,
                       (SELECT count(*) FROM yaya_control_jobs) AS jobs
                """
            )
            row = await cursor.fetchone()
            if row is None:
                self.fail("command count query returned no row")
            return cast(int, row["commands"]), cast(int, row["jobs"])
        finally:
            await connection.close()

    async def _expire_crashed_lease(self, command_id: str) -> None:
        """Advance only a confirmed crashed lease past expiry without a wall-clock race."""

        connection = await self.database.connect(autocommit=True)
        try:
            updated = await connection.execute(
                """
                UPDATE yaya_control_jobs
                SET claimed_at=clock_timestamp()-interval '4 seconds',
                    heartbeat_at=clock_timestamp()-interval '3 seconds',
                    lease_expires_at=clock_timestamp()-interval '1 second',
                    updated_at=clock_timestamp()
                WHERE tenant_id=%s AND command_id=%s AND state='LEASED'
                  AND attempt=1 AND worker_id IS NOT NULL AND lease_id IS NOT NULL
                """,
                (self.context.actor.tenant_id, command_id),
            )
            self.assertEqual(updated.rowcount, 1)
        finally:
            await connection.close()

    async def _registry_state(self) -> dict[str, int]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT (SELECT count(*) FROM yaya_registry_heads) AS heads,
                       (SELECT count(*) FROM yaya_registry_entries) AS entries,
                       (SELECT count(*) FROM yaya_skill_activations) AS activations,
                       COALESCE((SELECT max(revision) FROM yaya_registry_heads),0) AS revision
                """
            )
            row = await cursor.fetchone()
            if row is None:
                self.fail("registry count query returned no row")
            return {key: cast(int, row[key]) for key in row}
        finally:
            await connection.close()

    async def _assert_registry_empty(self) -> None:
        self.assertEqual(
            await self._registry_state(),
            {"heads": 0, "entries": 0, "activations": 0, "revision": 0},
        )

    async def _replica_execute(
        self,
        query: LiteralString,
        params: Sequence[object] = (),
    ) -> None:
        """Inject at-rest corruption while bypassing immutable/FK triggers."""

        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute("SET session_replication_role = replica")
            try:
                await connection.execute(query, params)
            finally:
                await connection.execute("SET session_replication_role = origin")
        finally:
            await connection.close()

    def _token_for(self, *, tenant_id: str, actor_id: str) -> str:
        actor = ActorRef(
            tenant_id=tenant_id,
            actor_id=actor_id,
            actor_type=ActorType.STUDENT,
            roles=("game:player",),
        )
        return self.authenticator.issue_for_test(actor, now=datetime.now(UTC))

    async def test_session_unknown_and_cross_scope_authorities_write_nothing(self) -> None:
        cases: list[tuple[str, dict[str, object], str | None]] = []
        unknown_world = dict(self.authority.session_request)
        unknown_world["world_id"] = "world_unknown_0001"
        cases.append(("unknown_world", unknown_world, None))
        unknown_learner = dict(self.authority.session_request)
        unknown_learner["learner_id"] = "learner_unknown_0001"
        cases.append(("unknown_learner", unknown_learner, None))
        unknown_profile = dict(self.authority.session_request)
        unknown_profile["agent_profile_id"] = "agent_profile_unknown_0001"
        cases.append(("unknown_profile", unknown_profile, None))
        wrong_content = dict(self.authority.session_request)
        content = dict(cast(dict[str, object], wrong_content["content"]))
        content["content_hash"] = "e" * 64
        wrong_content["content"] = content
        cases.append(("wrong_content", wrong_content, None))
        cases.append(
            (
                "wrong_actor",
                self.authority.session_request,
                self._token_for(
                    tenant_id=self.context.actor.tenant_id,
                    actor_id="student_other_0001",
                ),
            )
        )
        cases.append(
            (
                "wrong_tenant",
                self.authority.session_request,
                self._token_for(
                    tenant_id="tenant_other_0001",
                    actor_id=self.context.actor.actor_id,
                ),
            )
        )

        before = await self._business_fingerprint()
        for suffix, body, token in cases:
            with self.subTest(case=suffix):
                response, payload = await self._post(
                    "/v1/agent-sessions",
                    body,
                    suffix=f"session_{suffix}",
                    idempotency_key=f"session-matrix-{suffix}-0001",
                    token=token,
                )
                self.assertEqual(response.status, 404)
                self.assertEqual(self._error_code(payload), "NOT_FOUND")
                self.assertEqual(await self._business_fingerprint(), before)
        self.assertEqual(await self._command_job_counts(), (0, 0))
        await self._assert_registry_empty()

    async def test_session_ambiguous_task_mapping_is_structurally_rejected(self) -> None:
        """The active-scope unique index makes an ambiguous Task row unrepresentable."""

        before = await self._business_fingerprint()
        with self.assertRaises(psycopg.errors.UniqueViolation):
            async with self.database.transaction_with_commit_boundary() as connection:
                await connection.execute(
                    """
                    INSERT INTO yaya_tasks(tenant_id,task_id,actor_id,content_hash,snapshot_json)
                    SELECT tenant_id,'task_ambiguous_0001',actor_id,content_hash,snapshot_json
                    FROM yaya_tasks WHERE tenant_id=%s AND task_id=%s
                    """,
                    (self.context.actor.tenant_id, TASK_ID),
                )
                await connection.execute(
                    """
                    INSERT INTO yaya_launch_authorities(
                        tenant_id,authority_id,actor_id,learner_id,content_unit_id,
                        content_version,content_hash,world_id,agent_profile_id,task_id,
                        active,versions_json,snapshot_sha256
                    )
                    SELECT tenant_id,'authority_ambiguous_0001',actor_id,learner_id,
                           content_unit_id,content_version,content_hash,world_id,
                           agent_profile_id,'task_ambiguous_0001',TRUE,versions_json,%s
                    FROM yaya_launch_authorities
                    WHERE tenant_id=%s AND authority_id=%s
                    """,
                    (
                        "f" * 64,
                        self.context.actor.tenant_id,
                        self.authority.authority_id,
                    ),
                )
        self.assertEqual(await self._business_fingerprint(), before)
        self.assertEqual(await self._command_job_counts(), (0, 0))

    async def test_session_stale_world_revision_is_terminal_and_side_effect_free(self) -> None:
        body = dict(self.authority.session_request)
        body["expected_world_revision"] = self.authority.world_revision - 1
        accepted, payload = await self._post(
            "/v1/agent-sessions",
            body,
            suffix="session_stale_accept",
            idempotency_key="session-matrix-stale-0001",
        )
        self.assertEqual(accepted.status, 202)
        before = await self._business_fingerprint()
        self.assertTrue(await self.worker.run_once())
        row = await self._job_row(cast(str, payload["command_id"]))
        self.assertEqual(row["state"], "FAILED")
        self.assertEqual(row["command_status"], "REJECTED")
        self.assertEqual(row["last_error_code"], "WORLD_REVISION_CONFLICT")
        self.assertEqual(await self._business_fingerprint(), before)
        await self._assert_registry_empty()

    async def test_session_same_key_different_body_has_no_second_receipt_or_session(self) -> None:
        before = await self._business_fingerprint()
        first, _ = await self._post(
            "/v1/agent-sessions",
            self.authority.session_request,
            suffix="session_key_first",
            idempotency_key="session-matrix-reused-0001",
        )
        self.assertEqual(first.status, 202)
        changed = dict(self.authority.session_request)
        changed["locale"] = "en-US"
        second, payload = await self._post(
            "/v1/agent-sessions",
            changed,
            suffix="session_key_changed",
            idempotency_key="session-matrix-reused-0001",
        )
        self.assertEqual(second.status, 409)
        self.assertEqual(self._error_code(payload), "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual(await self._command_job_counts(), (1, 1))
        self.assertEqual(await self._business_fingerprint(), before)

    async def test_session_corrupt_receipt_and_job_digest_fail_closed(self) -> None:
        accepted, payload = await self._post(
            "/v1/agent-sessions",
            self.authority.session_request,
            suffix="session_corrupt_accept",
            idempotency_key="session-matrix-corrupt-0001",
        )
        self.assertEqual(accepted.status, 202)
        command_id = cast(str, payload["command_id"])
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                UPDATE yaya_control_jobs
                SET accepted_receipt_json=jsonb_set(
                    accepted_receipt_json,'{trace_id}','"trace_corrupt"'::jsonb
                )
                WHERE tenant_id=%s AND command_id=%s
                """,
                (self.context.actor.tenant_id, command_id),
            )
        finally:
            await connection.close()
        before = await self._business_fingerprint()
        replay, replay_payload = await self._post(
            "/v1/agent-sessions",
            self.authority.session_request,
            suffix="session_corrupt_replay",
            idempotency_key="session-matrix-corrupt-0001",
        )
        self.assertEqual(replay.status, 500)
        self.assertEqual(self._error_code(replay_payload), "INVARIANT_VIOLATION")
        self.assertEqual(await self._business_fingerprint(), before)

        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                UPDATE yaya_control_jobs SET request_sha256=%s
                WHERE tenant_id=%s AND command_id=%s
                """,
                ("0" * 64, self.context.actor.tenant_id, command_id),
            )
        finally:
            await connection.close()
        with self.assertRaises(BackendApplicationError):
            await self.worker.run_once()
        row = await self._job_row(command_id)
        self.assertEqual((row["state"], row["attempt"]), ("READY", 0))
        self.assertEqual(await self._business_fingerprint(), before)
        await self._assert_registry_empty()

    async def test_session_accepted_restart_and_terminal_replay_create_once(self) -> None:
        body = self.authority.session_request
        first, first_payload = await self._post(
            "/v1/agent-sessions",
            body,
            suffix="session_restart_accept",
            idempotency_key="session-matrix-restart-0001",
        )
        self.assertEqual(first.status, 202)
        _, restarted_http, restarted_worker = self._surfaces(
            self.database,
            worker_id="session-restart-worker-1",
        )
        replay, replay_payload = await self._post(
            "/v1/agent-sessions",
            body,
            suffix="session_restart_replay",
            idempotency_key="session-matrix-restart-0001",
            http=restarted_http,
        )
        self.assertEqual(replay.status, 202)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(replay_payload, first_payload)
        self.assertTrue(await restarted_worker.run_once())

        _, final_http, final_worker = self._surfaces(
            self.database,
            worker_id="session-restart-worker-2",
        )
        terminal_replay, terminal_payload = await self._post(
            "/v1/agent-sessions",
            body,
            suffix="session_terminal_replay",
            idempotency_key="session-matrix-restart-0001",
            http=final_http,
        )
        self.assertEqual(terminal_replay.status, 202)
        self.assertEqual(terminal_payload, first_payload)
        self.assertFalse(await final_worker.run_once())
        row = await self._job_row(cast(str, first_payload["command_id"]))
        session_id = cast(str, row["resource_id"])
        response = await final_http.handle(
            "GET",
            f"/v1/agent-sessions/{session_id}",
            self._get_headers(suffix="session_restart_get"),
        )
        self.assertEqual(response.status, 200)
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT (SELECT count(*) FROM yaya_agent_sessions) AS legacy,
                       (SELECT count(*) FROM yaya_public_agent_sessions) AS public
                """
            )
            counts = await cursor.fetchone()
            self.assertEqual(counts, {"legacy": 1, "public": 1})
        finally:
            await connection.close()
        await self._assert_registry_empty()

    async def test_session_inflight_commit_loss_is_taken_over_once(self) -> None:
        body = self.authority.session_request
        accepted, payload = await self._post(
            "/v1/agent-sessions",
            body,
            suffix="session_takeover_accept",
            idempotency_key="session-matrix-takeover-0001",
        )
        self.assertEqual(accepted.status, 202)
        command_id = cast(str, payload["command_id"])
        lossy = _PostCommitUnknownDatabase(self.server.dsn, fail_on_commit=1)
        _, _, crashed_worker = self._surfaces(
            lossy,
            worker_id="session-crashed-worker",
            lease_seconds=2,
        )
        with self.assertRaises(PostgresCommitStateUnknown):
            await crashed_worker.run_once()
        leased = await self._job_row(command_id)
        self.assertEqual(
            (leased["state"], leased["attempt"], leased["command_status"]),
            ("LEASED", 1, "VALIDATING"),
        )
        replay, replay_payload = await self._post(
            "/v1/agent-sessions",
            body,
            suffix="session_takeover_inflight_replay",
            idempotency_key="session-matrix-takeover-0001",
        )
        self.assertEqual(replay.status, 202)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(replay_payload, payload)

        await self._expire_crashed_lease(command_id)
        _, _, takeover_worker = self._surfaces(
            self.database,
            worker_id="session-takeover-worker",
            lease_seconds=2,
        )
        self.assertTrue(await takeover_worker.run_once())
        terminal = await self._job_row(command_id)
        self.assertEqual(
            (terminal["state"], terminal["attempt"], terminal["command_status"]),
            ("SUCCEEDED", 2, "APPLIED"),
        )
        self.assertFalse(await takeover_worker.run_once())
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                "SELECT count(*) AS count FROM yaya_public_agent_sessions"
            )
            row = await cursor.fetchone()
            self.assertEqual(row, {"count": 1})
        finally:
            await connection.close()

    async def test_activation_uncertified_and_cross_scope_requests_write_nothing(self) -> None:
        target = "/v1/skill-versions/skillver_uncertified_0001/activations"
        before = await self._business_fingerprint()
        response, payload = await self._post(
            target,
            self._activation_body(),
            suffix="activation_uncertified",
            idempotency_key="activation-matrix-uncertified-0001",
        )
        self.assertEqual(response.status, 422)
        self.assertEqual(self._error_code(payload), "SKILL_NOT_CERTIFIED")
        self.assertEqual(await self._business_fingerprint(), before)

        await self._seed_certified()
        target = f"/v1/skill-versions/{SKILL_VERSION_ID}/activations"
        cases: list[tuple[str, dict[str, object], str | None, int, str]] = []
        wrong_world = self._activation_body()
        cast(dict[str, object], wrong_world["activation_scope"])["world_id"] = "world_other_0001"
        cases.append(("wrong_world", wrong_world, None, 404, "NOT_FOUND"))
        wrong_profile = self._activation_body()
        cast(dict[str, object], wrong_profile["activation_scope"])["agent_profile_id"] = (
            "agent_profile_other_0001"
        )
        cases.append(("wrong_profile", wrong_profile, None, 404, "NOT_FOUND"))
        cases.append(
            (
                "wrong_actor",
                self._activation_body(),
                self._token_for(
                    tenant_id=self.context.actor.tenant_id,
                    actor_id="student_other_0001",
                ),
                422,
                "SKILL_NOT_CERTIFIED",
            )
        )
        cases.append(
            (
                "wrong_tenant",
                self._activation_body(),
                self._token_for(
                    tenant_id="tenant_other_0001",
                    actor_id=self.context.actor.actor_id,
                ),
                422,
                "SKILL_NOT_CERTIFIED",
            )
        )
        fixture_fingerprint = await self._business_fingerprint()
        for suffix, body, token, status, code in cases:
            with self.subTest(case=suffix):
                failed, failed_payload = await self._post(
                    target,
                    body,
                    suffix=f"activation_{suffix}",
                    idempotency_key=f"activation-matrix-{suffix}-0001",
                    token=token,
                )
                self.assertEqual(failed.status, status)
                self.assertEqual(self._error_code(failed_payload), code)
                self.assertEqual(await self._business_fingerprint(), fixture_fingerprint)
        self.assertEqual(await self._command_job_counts(), (0, 0))
        await self._assert_registry_empty()

    async def test_activation_cross_content_certification_drift_fails_at_http_boundary(
        self,
    ) -> None:
        await self._seed_certified()
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT record_json FROM yaya_skill_certifications
                WHERE tenant_id=%s AND certification_id=%s
                """,
                (self.context.actor.tenant_id, CERTIFICATION_ID),
            )
            row = await cursor.fetchone()
            if row is None:
                self.fail("certification fixture disappeared")
            record = cast(dict[str, object], row["record_json"])
        finally:
            await connection.close()
        request_context = cast(dict[str, object], record["request_context"])
        content_ref = cast(dict[str, object], request_context["content_ref"])
        content_ref["content_hash"] = "f" * 64
        await self._replica_execute(
            """
            UPDATE yaya_skill_certifications SET record_json=%s,certification_sha256=%s
            WHERE tenant_id=%s AND certification_id=%s
            """,
            (
                Jsonb(record),
                canonical_json_sha256(record),
                self.context.actor.tenant_id,
                CERTIFICATION_ID,
            ),
        )
        before = await self._business_fingerprint()
        response, payload = await self._post(
            f"/v1/skill-versions/{SKILL_VERSION_ID}/activations",
            self._activation_body(),
            suffix="activation_cross_content",
            idempotency_key="activation-matrix-cross-content-0001",
        )
        self.assertEqual(response.status, 500)
        self.assertEqual(self._error_code(payload), "INVARIANT_VIOLATION")
        self.assertEqual(await self._business_fingerprint(), before)
        self.assertEqual(await self._command_job_counts(), (0, 0))
        await self._assert_registry_empty()

    async def test_activation_revoked_certification_is_terminal_and_side_effect_free(self) -> None:
        await self._seed_certified()
        revoked_at = datetime.now(UTC)
        revocation: dict[str, object] = {
            "revocation_id": "revocation_matrix_0001",
            "certification_id": CERTIFICATION_ID,
            "reason": "Matrix revocation fault injection",
            "revoked_at": revoked_at.isoformat().replace("+00:00", "Z"),
        }
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                INSERT INTO yaya_certification_revocations(
                    tenant_id,revocation_id,certification_id,actor_id,content_hash,
                    reason,revocation_sha256,record_json,revoked_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    self.context.actor.tenant_id,
                    revocation["revocation_id"],
                    CERTIFICATION_ID,
                    self.context.actor.actor_id,
                    self.context.content_ref.content_hash,
                    revocation["reason"],
                    canonical_json_sha256(revocation),
                    Jsonb(revocation),
                    revoked_at,
                ),
            )
        finally:
            await connection.close()
        accepted, payload = await self._post(
            f"/v1/skill-versions/{SKILL_VERSION_ID}/activations",
            self._activation_body(),
            suffix="activation_revoked_accept",
            idempotency_key="activation-matrix-revoked-0001",
        )
        self.assertEqual(accepted.status, 202)
        before = await self._business_fingerprint()
        self.assertTrue(await self.worker.run_once())
        terminal = await self._job_row(cast(str, payload["command_id"]))
        self.assertEqual(terminal["state"], "FAILED")
        self.assertEqual(terminal["last_error_code"], "SKILL_NOT_CERTIFIED")
        self.assertEqual(await self._business_fingerprint(), before)
        await self._assert_registry_empty()

    async def test_activation_failed_build_is_terminal_and_side_effect_free(self) -> None:
        await self._seed_certified()
        accepted, payload = await self._post(
            f"/v1/skill-versions/{SKILL_VERSION_ID}/activations",
            self._activation_body(),
            suffix="activation_failed_build_accept",
            idempotency_key="activation-matrix-failed-build-0001",
        )
        self.assertEqual(accepted.status, 202)
        await self._replica_execute(
            """
            UPDATE yaya_skill_builds SET status='FAILED',terminal=TRUE
            WHERE tenant_id=%s AND build_id=%s
            """,
            (self.context.actor.tenant_id, BUILD_ID),
        )
        before = await self._business_fingerprint()
        self.assertTrue(await self.worker.run_once())
        terminal = await self._job_row(cast(str, payload["command_id"]))
        self.assertEqual(terminal["state"], "FAILED")
        self.assertEqual(terminal["last_error_code"], "INVARIANT_VIOLATION")
        self.assertEqual(await self._business_fingerprint(), before)
        await self._assert_registry_empty()

    async def test_activation_wrong_certification_id_is_terminal_and_side_effect_free(self) -> None:
        await self._seed_certified()
        accepted, payload = await self._post(
            f"/v1/skill-versions/{SKILL_VERSION_ID}/activations",
            self._activation_body(),
            suffix="activation_wrong_cert_accept",
            idempotency_key="activation-matrix-wrong-cert-0001",
        )
        self.assertEqual(accepted.status, 202)
        await self._replica_execute(
            """
            UPDATE yaya_skills SET certification_id='cert_wrong_matrix_0001'
            WHERE tenant_id=%s AND skill_version_id=%s
            """,
            (self.context.actor.tenant_id, SKILL_VERSION_ID),
        )
        before = await self._business_fingerprint()
        self.assertTrue(await self.worker.run_once())
        terminal = await self._job_row(cast(str, payload["command_id"]))
        self.assertEqual(terminal["state"], "FAILED")
        self.assertEqual(terminal["last_error_code"], "SKILL_NOT_CERTIFIED")
        self.assertEqual(await self._business_fingerprint(), before)
        await self._assert_registry_empty()

    async def test_activation_unknown_artifact_is_terminal_and_side_effect_free(self) -> None:
        fixture = await self._seed_certified()
        accepted, payload = await self._post(
            f"/v1/skill-versions/{SKILL_VERSION_ID}/activations",
            self._activation_body(),
            suffix="activation_missing_artifact_accept",
            idempotency_key="activation-matrix-missing-artifact-0001",
        )
        self.assertEqual(accepted.status, 202)
        fixture.artifact_path.rename(fixture.artifact_path.with_suffix(".missing"))
        before = await self._business_fingerprint()
        self.assertTrue(await self.worker.run_once())
        terminal = await self._job_row(cast(str, payload["command_id"]))
        self.assertEqual(terminal["state"], "FAILED")
        self.assertEqual(terminal["last_error_code"], "INVARIANT_VIOLATION")
        self.assertEqual(await self._business_fingerprint(), before)
        await self._assert_registry_empty()

    async def test_activation_artifact_hash_mismatch_is_terminal_and_side_effect_free(self) -> None:
        await self._seed_certified()
        accepted, payload = await self._post(
            f"/v1/skill-versions/{SKILL_VERSION_ID}/activations",
            self._activation_body(),
            suffix="activation_artifact_hash_accept",
            idempotency_key="activation-matrix-artifact-hash-0001",
        )
        self.assertEqual(accepted.status, 202)
        await self._replica_execute(
            """
            UPDATE yaya_artifacts
            SET metadata_json=jsonb_set(metadata_json,'{artifact_sha256}',%s::jsonb)
            WHERE tenant_id=%s AND build_id=%s
            """,
            (json.dumps("a" * 64), self.context.actor.tenant_id, BUILD_ID),
        )
        before = await self._business_fingerprint()
        self.assertTrue(await self.worker.run_once())
        terminal = await self._job_row(cast(str, payload["command_id"]))
        self.assertEqual(terminal["state"], "FAILED")
        self.assertEqual(terminal["last_error_code"], "INVARIANT_VIOLATION")
        self.assertEqual(await self._business_fingerprint(), before)
        await self._assert_registry_empty()

    async def test_activation_same_key_different_body_has_no_registry_write(self) -> None:
        await self._seed_certified()
        target = f"/v1/skill-versions/{SKILL_VERSION_ID}/activations"
        first, _ = await self._post(
            target,
            self._activation_body(),
            suffix="activation_key_first",
            idempotency_key="activation-matrix-reused-0001",
        )
        self.assertEqual(first.status, 202)
        before = await self._business_fingerprint()
        changed = self._activation_body()
        changed["reason"] = "A byte-distinct replay must be rejected."
        second, payload = await self._post(
            target,
            changed,
            suffix="activation_key_changed",
            idempotency_key="activation-matrix-reused-0001",
        )
        self.assertEqual(second.status, 409)
        self.assertEqual(self._error_code(payload), "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual(await self._business_fingerprint(), before)
        self.assertEqual(await self._command_job_counts(), (1, 1))
        await self._assert_registry_empty()

    async def test_activation_concurrent_and_stale_cas_advance_exactly_once(self) -> None:
        await self._seed_certified()
        target = f"/v1/skill-versions/{SKILL_VERSION_ID}/activations"
        (first, first_payload), (second, second_payload) = await asyncio.gather(
            self._post(
                target,
                self._activation_body(),
                suffix="activation_concurrent_a",
                idempotency_key="activation-matrix-concurrent-a-0001",
            ),
            self._post(
                target,
                self._activation_body(),
                suffix="activation_concurrent_b",
                idempotency_key="activation-matrix-concurrent-b-0001",
            ),
        )
        self.assertEqual((first.status, second.status), (202, 202))
        _, _, worker_a = self._surfaces(
            self.database,
            worker_id="activation-concurrent-worker-a",
        )
        _, _, worker_b = self._surfaces(
            self.database,
            worker_id="activation-concurrent-worker-b",
        )
        self.assertEqual(
            await asyncio.gather(worker_a.run_once(), worker_b.run_once()), [True, True]
        )
        rows = [
            await self._job_row(cast(str, first_payload["command_id"])),
            await self._job_row(cast(str, second_payload["command_id"])),
        ]
        self.assertEqual(sorted(cast(str, row["state"]) for row in rows), ["FAILED", "SUCCEEDED"])
        failed = next(row for row in rows if row["state"] == "FAILED")
        self.assertEqual(failed["last_error_code"], "CONTENT_VERSION_MISMATCH")
        self.assertEqual(
            await self._registry_state(),
            {"heads": 1, "entries": 1, "activations": 1, "revision": 1},
        )

        stable = await self._business_fingerprint()
        stale, stale_payload = await self._post(
            target,
            self._activation_body(),
            suffix="activation_stale",
            idempotency_key="activation-matrix-stale-0001",
        )
        self.assertEqual(stale.status, 409)
        self.assertEqual(self._error_code(stale_payload), "CONTENT_VERSION_MISMATCH")
        for suffix, key in (
            ("a", "activation-matrix-concurrent-a-0001"),
            ("b", "activation-matrix-concurrent-b-0001"),
        ):
            replay, _ = await self._post(
                target,
                self._activation_body(),
                suffix=f"activation_concurrent_replay_{suffix}",
                idempotency_key=key,
            )
            self.assertEqual(replay.status, 202)
            self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(await self._business_fingerprint(), stable)

    async def test_activation_accept_and_final_commit_response_loss_reconcile_once(self) -> None:
        await self._seed_certified()
        target = f"/v1/skill-versions/{SKILL_VERSION_ID}/activations"
        lossy_accept_database = _PostCommitUnknownDatabase(self.server.dsn, fail_on_commit=1)
        _, lossy_http, _ = self._surfaces(
            lossy_accept_database,
            worker_id="activation-lossy-accept-unused",
        )
        accepted, accepted_payload = await self._post(
            target,
            self._activation_body(),
            suffix="activation_lossy_accept",
            idempotency_key="activation-matrix-lossy-0001",
            http=lossy_http,
        )
        self.assertEqual(accepted.status, 202)
        self.assertTrue(lossy_accept_database.did_fail)

        lossy_final_database = _PostCommitUnknownDatabase(self.server.dsn, fail_on_commit=2)
        _, _, lossy_worker = self._surfaces(
            lossy_final_database,
            worker_id="activation-lossy-final-worker",
        )
        self.assertTrue(await lossy_worker.run_once())
        self.assertTrue(lossy_final_database.did_fail)
        terminal = await self._job_row(cast(str, accepted_payload["command_id"]))
        self.assertEqual((terminal["state"], terminal["command_status"]), ("SUCCEEDED", "APPLIED"))
        self.assertEqual(
            await self._registry_state(),
            {"heads": 1, "entries": 1, "activations": 1, "revision": 1},
        )

        stable = await self._business_fingerprint()
        replay, replay_payload = await self._post(
            target,
            self._activation_body(),
            suffix="activation_lossy_replay",
            idempotency_key="activation-matrix-lossy-0001",
        )
        self.assertEqual(replay.status, 202)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(replay_payload, accepted_payload)
        _, _, restarted_worker = self._surfaces(
            self.database,
            worker_id="activation-lossy-restart-worker",
        )
        self.assertFalse(await restarted_worker.run_once())
        self.assertEqual(await self._business_fingerprint(), stable)

    async def test_activation_inflight_commit_loss_is_taken_over_once(self) -> None:
        await self._seed_certified()
        target = f"/v1/skill-versions/{SKILL_VERSION_ID}/activations"
        accepted, payload = await self._post(
            target,
            self._activation_body(),
            suffix="activation_takeover_accept",
            idempotency_key="activation-matrix-takeover-0001",
        )
        self.assertEqual(accepted.status, 202)
        command_id = cast(str, payload["command_id"])
        lossy = _PostCommitUnknownDatabase(self.server.dsn, fail_on_commit=1)
        _, _, crashed_worker = self._surfaces(
            lossy,
            worker_id="activation-crashed-worker",
            lease_seconds=2,
        )
        with self.assertRaises(PostgresCommitStateUnknown):
            await crashed_worker.run_once()
        leased = await self._job_row(command_id)
        self.assertEqual(
            (leased["state"], leased["attempt"], leased["command_status"]),
            ("LEASED", 1, "VALIDATING"),
        )
        replay, replay_payload = await self._post(
            target,
            self._activation_body(),
            suffix="activation_takeover_inflight_replay",
            idempotency_key="activation-matrix-takeover-0001",
        )
        self.assertEqual(replay.status, 202)
        self.assertEqual(replay_payload, payload)
        await self._expire_crashed_lease(command_id)

        _, restarted_http, takeover_worker = self._surfaces(
            self.database,
            worker_id="activation-takeover-worker",
            lease_seconds=2,
        )
        self.assertTrue(await takeover_worker.run_once())
        terminal = await self._job_row(command_id)
        self.assertEqual(
            (terminal["state"], terminal["attempt"], terminal["command_status"]),
            ("SUCCEEDED", 2, "APPLIED"),
        )
        stable = await self._business_fingerprint()
        terminal_replay, terminal_payload = await self._post(
            target,
            self._activation_body(),
            suffix="activation_takeover_terminal_replay",
            idempotency_key="activation-matrix-takeover-0001",
            http=restarted_http,
        )
        self.assertEqual(terminal_replay.status, 202)
        self.assertEqual(terminal_payload, payload)
        self.assertFalse(await takeover_worker.run_once())
        self.assertEqual(await self._business_fingerprint(), stable)
        self.assertEqual(
            await self._registry_state(),
            {"heads": 1, "entries": 1, "activations": 1, "revision": 1},
        )


if __name__ == "__main__":
    unittest.main()
