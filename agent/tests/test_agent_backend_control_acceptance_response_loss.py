from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import hashlib
import json
import sys
import tempfile
import unittest
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from psycopg import AsyncConnection, sql

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPOSITORY_ROOT / "python"
TEST_ROOT = Path(__file__).resolve().parent
CONTRACTS_ROOT = REPOSITORY_ROOT / "contracts"
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from a8_state_fingerprint import (  # noqa: E402
    A8_BUSINESS_TABLES,
    A8StateFingerprint,
    a8_state_fingerprint,
    missing_a8_business_tables,
)
from agent_runtime_fixtures import WORLD_ID  # noqa: E402
from postgres_test_support import postgres_test_server  # noqa: E402
from test_agent_backend_skill_build_executor import (  # noqa: E402
    AGENT_PROFILE_ID,
    LEARNER_ID,
    TEST_SUITE_VERSION,
    _AuthorityFixture,
    _seed_only_build_authority,
)
from yaya_agent_backend.application import AgentTurnApplication  # noqa: E402
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
from yaya_agent_build import CPP20_SAFE_V1_PROFILE  # noqa: E402


class _PostCommitAcknowledgementLossDatabase(PostgresDatabase):
    """Commit normally, then lose exactly one selected COMMIT acknowledgement."""

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


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class ControlAcceptanceResponseLossTests(unittest.IsolatedAsyncioTestCase):
    """Real PostgreSQL response-loss recovery at public control boundaries."""

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
                sql.SQL("TRUNCATE {} CASCADE").format(
                    sql.SQL(", ").join(
                        sql.Identifier(table_name) for table_name in A8_BUSINESS_TABLES
                    )
                )
            )
        finally:
            await connection.close()

        self.authority: _AuthorityFixture = await _seed_only_build_authority(self.database)
        self.validator = ContractSchemaValidator(CONTRACTS_ROOT)
        self.authenticator = JwtAuthenticator(
            hmac_secret="control-response-loss-secret-000000000000000000",
            issuer="yaya-control-response-loss",
            audience="yaya-agent-test",
        )
        self.token = self.authenticator.issue_for_test(
            self.authority.context.actor,
            now=datetime.now(UTC),
        )
        self._artifact_directory = tempfile.TemporaryDirectory(
            prefix="yaya-control-response-loss-artifacts-"
        )
        self.artifact_root = Path(self._artifact_directory.name).resolve()

    async def asyncTearDown(self) -> None:
        self._artifact_directory.cleanup()

    def _surfaces(
        self,
        database: PostgresDatabase,
        *,
        worker_id: str,
    ) -> tuple[AgentHttpApi, StudentSkillChainWorker]:
        chain = StudentSkillChainApplication(
            database,
            self.validator,
            self.authority.versions,
            artifact_root=self.artifact_root,
        )
        http = AgentHttpApi(
            application=AgentTurnApplication(
                database,
                CONTRACTS_ROOT,
                self.authority.versions,
            ),
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
        )
        return http, worker

    def _headers(
        self,
        *,
        suffix: str,
        raw_body: bytes | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Schema-Version": "1.0.0",
            "X-Request-Id": f"req_control_loss_{suffix}",
            "X-Trace-Id": f"trace_control_loss_{suffix}",
            "X-Correlation-Id": f"corr_control_loss_{suffix}",
        }
        if raw_body is not None:
            headers.update(
                {
                    "Content-Type": "application/json",
                    "Content-Length": str(len(raw_body)),
                }
            )
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def _post(
        self,
        http: AgentHttpApi,
        target: str,
        body: Mapping[str, object],
        *,
        suffix: str,
        idempotency_key: str,
    ) -> tuple[HttpResponse, dict[str, object], bytes]:
        raw_body = _json_bytes(body)
        response = await http.handle(
            "POST",
            target,
            self._headers(
                suffix=suffix,
                raw_body=raw_body,
                idempotency_key=idempotency_key,
            ),
            raw_body,
        )
        return response, cast(dict[str, object], json.loads(response.body)), raw_body

    async def _get(
        self,
        http: AgentHttpApi,
        target: str,
        *,
        suffix: str,
    ) -> tuple[HttpResponse, dict[str, object]]:
        response = await http.handle(
            "GET",
            target,
            self._headers(suffix=suffix),
            b"",
        )
        return response, cast(dict[str, object], json.loads(response.body))

    async def _state(self) -> A8StateFingerprint:
        fingerprint = await a8_state_fingerprint(self.database)
        self.assertEqual(missing_a8_business_tables(fingerprint), ())
        return fingerprint

    async def _counts(self) -> dict[str, int]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM yaya_commands)::integer AS commands,
                  (SELECT count(*) FROM yaya_control_jobs)::integer AS control_jobs,
                  (SELECT count(*) FROM yaya_command_jobs)::integer AS turn_jobs,
                  (SELECT count(*) FROM yaya_agent_sessions)::integer AS sessions,
                  (SELECT count(*) FROM yaya_public_agent_sessions)::integer AS public_sessions,
                  (SELECT count(*) FROM yaya_skill_builds)::integer AS builds,
                  (SELECT count(*) FROM yaya_skill_build_history)::integer AS build_history
                """
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            self.fail("control response-loss count query returned no row")
        return {key: cast(int, value) for key, value in row.items()}

    def _assert_no_artifact_side_effects(self) -> None:
        self.assertEqual(
            [path for path in self.artifact_root.rglob("*") if path.is_file()],
            [],
        )

    def _session_body(self) -> dict[str, object]:
        content = self.authority.context.content_ref
        return {
            "world_id": WORLD_ID,
            "learner_id": LEARNER_ID,
            "agent_profile_id": AGENT_PROFILE_ID,
            "channel": "GAME",
            "locale": "zh-CN",
            "content": {
                "unit_id": content.unit_id,
                "version": content.version,
                "content_hash": content.content_hash,
            },
            "expected_world_revision": 5,
        }

    @staticmethod
    def _build_body() -> dict[str, object]:
        source = "int main() { return 0; }\n"
        return {
            "skill_id": "skill_acceptance_response_loss_0001",
            "display_name": "Acceptance response-loss Skill",
            "client_draft_revision": 1,
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

    async def _assert_terminal_session(
        self,
        http: AgentHttpApi,
        location: str,
        *,
        suffix: str,
    ) -> str:
        command_response, command = await self._get(http, location, suffix=f"{suffix}_command")
        self.assertEqual(command_response.status, 200, command)
        self.assertEqual((command["status"], command["terminal"]), ("APPLIED", True))
        result = cast(dict[str, object], command["result"])
        self.assertEqual(result["resource_type"], "AGENT_SESSION")
        resource_url = cast(str, result["resource_url"])
        session_response, session = await self._get(
            http,
            resource_url,
            suffix=f"{suffix}_resource",
        )
        self.assertEqual(session_response.status, 200, session)
        self.assertEqual(session["status"], "ACTIVE")
        self.assertEqual(session["session_id"], result["resource_id"])
        return resource_url

    async def test_session_acceptance_commit_ack_loss_replays_after_application_restart(
        self,
    ) -> None:
        idempotency_key = "session-acceptance-response-loss-0001"
        body = self._session_body()
        lossy_database = _PostCommitAcknowledgementLossDatabase(
            self.server.dsn,
            fail_on_commit=1,
        )
        lossy_http, _ = self._surfaces(
            lossy_database,
            worker_id="session-acceptance-loss-unused-worker",
        )
        first, first_payload, _ = await self._post(
            lossy_http,
            "/v1/agent-sessions",
            body,
            suffix="session_acceptance_first",
            idempotency_key=idempotency_key,
        )
        self.assertEqual(first.status, 202, first_payload)
        self.assertEqual(first.headers["Idempotency-Replayed"], "false")
        self.assertTrue(lossy_database.did_fail)
        self.assertEqual(lossy_database.commit_count, 1)
        location = first.headers["Location"]
        self.assertEqual(location, f"/v1/commands/{first_payload['command_id']}")
        committed_acceptance = await self._state()

        restarted_http, restarted_worker = self._surfaces(
            self.database,
            worker_id="session-acceptance-restarted-worker",
        )
        replay, replay_payload, _ = await self._post(
            restarted_http,
            "/v1/agent-sessions",
            body,
            suffix="session_acceptance_replay",
            idempotency_key=idempotency_key,
        )
        self.assertEqual(replay.status, 202, replay_payload)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(replay.headers["Location"], location)
        self.assertEqual(replay_payload, first_payload)
        self.assertEqual(await self._state(), committed_acceptance)

        self.assertTrue(await restarted_worker.run_once())
        await self._assert_terminal_session(
            restarted_http,
            location,
            suffix="session_acceptance_terminal",
        )
        self.assertEqual(
            await self._counts(),
            {
                "commands": 1,
                "control_jobs": 1,
                "turn_jobs": 0,
                "sessions": 1,
                "public_sessions": 1,
                "builds": 0,
                "build_history": 0,
            },
        )
        stable = await self._state()

        final_http, final_worker = self._surfaces(
            self.database,
            worker_id="session-acceptance-final-worker",
        )
        final_replay, final_payload, _ = await self._post(
            final_http,
            "/v1/agent-sessions",
            body,
            suffix="session_acceptance_final_replay",
            idempotency_key=idempotency_key,
        )
        self.assertEqual(final_replay.status, 202, final_payload)
        self.assertEqual(final_replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(final_replay.headers["Location"], location)
        self.assertEqual(final_payload, first_payload)
        await self._assert_terminal_session(
            final_http,
            location,
            suffix="session_acceptance_final",
        )
        self.assertFalse(await final_worker.run_once())
        self.assertEqual(await self._state(), stable)
        self._assert_no_artifact_side_effects()

    async def test_session_final_resource_commit_ack_loss_recovers_after_worker_restart(
        self,
    ) -> None:
        idempotency_key = "session-final-response-loss-0001"
        body = self._session_body()
        initial_http, _ = self._surfaces(
            self.database,
            worker_id="session-final-initial-unused-worker",
        )
        accepted, accepted_payload, _ = await self._post(
            initial_http,
            "/v1/agent-sessions",
            body,
            suffix="session_final_accept",
            idempotency_key=idempotency_key,
        )
        self.assertEqual(accepted.status, 202, accepted_payload)
        location = accepted.headers["Location"]

        lossy_database = _PostCommitAcknowledgementLossDatabase(
            self.server.dsn,
            fail_on_commit=2,
        )
        _, lossy_worker = self._surfaces(
            lossy_database,
            worker_id="session-final-loss-worker",
        )
        self.assertTrue(await lossy_worker.run_once())
        self.assertTrue(lossy_database.did_fail)
        self.assertEqual(lossy_database.commit_count, 2)
        committed_terminal = await self._state()

        restarted_http, restarted_worker = self._surfaces(
            self.database,
            worker_id="session-final-restarted-worker",
        )
        replay, replay_payload, _ = await self._post(
            restarted_http,
            "/v1/agent-sessions",
            body,
            suffix="session_final_replay",
            idempotency_key=idempotency_key,
        )
        self.assertEqual(replay.status, 202, replay_payload)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(replay.headers["Location"], location)
        self.assertEqual(replay_payload, accepted_payload)
        await self._assert_terminal_session(
            restarted_http,
            location,
            suffix="session_final_terminal",
        )
        self.assertFalse(await restarted_worker.run_once())
        self.assertEqual(
            await self._counts(),
            {
                "commands": 1,
                "control_jobs": 1,
                "turn_jobs": 0,
                "sessions": 1,
                "public_sessions": 1,
                "builds": 0,
                "build_history": 0,
            },
        )
        self.assertEqual(await self._state(), committed_terminal)
        self._assert_no_artifact_side_effects()

    async def test_build_acceptance_commit_ack_loss_preserves_inserted_build_after_restart(
        self,
    ) -> None:
        idempotency_key = "build-acceptance-response-loss-0001"
        body = self._build_body()
        lossy_database = _PostCommitAcknowledgementLossDatabase(
            self.server.dsn,
            fail_on_commit=1,
        )
        lossy_http, _ = self._surfaces(
            lossy_database,
            worker_id="build-acceptance-loss-unused-worker",
        )
        first, first_payload, _ = await self._post(
            lossy_http,
            "/v1/skill-builds",
            body,
            suffix="build_acceptance_first",
            idempotency_key=idempotency_key,
        )
        self.assertEqual(first.status, 202, first_payload)
        self.assertEqual(first.headers["Idempotency-Replayed"], "false")
        self.assertTrue(lossy_database.did_fail)
        self.assertEqual(lossy_database.commit_count, 1)
        location = first.headers["Location"]
        self.assertEqual(location, f"/v1/commands/{first_payload['command_id']}")
        committed = await self._state()

        restarted_http, _ = self._surfaces(
            self.database,
            worker_id="build-acceptance-restarted-unused-worker",
        )
        replay, replay_payload, _ = await self._post(
            restarted_http,
            "/v1/skill-builds",
            body,
            suffix="build_acceptance_replay",
            idempotency_key=idempotency_key,
        )
        self.assertEqual(replay.status, 202, replay_payload)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(replay.headers["Location"], location)
        self.assertEqual(replay_payload, first_payload)

        command_response, command = await self._get(
            restarted_http,
            location,
            suffix="build_acceptance_command",
        )
        self.assertEqual(command_response.status, 200, command)
        self.assertEqual((command["status"], command["terminal"]), ("ACCEPTED", False))
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT resource_id FROM yaya_control_jobs
                WHERE tenant_id=%s AND command_id=%s
                """,
                (
                    self.authority.context.actor.tenant_id,
                    first_payload["command_id"],
                ),
            )
            resource_row = await cursor.fetchone()
        finally:
            await connection.close()
        if resource_row is None:
            self.fail("accepted Build control job disappeared")
        build_id = cast(str, resource_row["resource_id"])
        build_response, build = await self._get(
            restarted_http,
            f"/v1/skill-builds/{build_id}",
            suffix="build_acceptance_resource",
        )
        self.assertEqual(build_response.status, 200, build)
        self.assertEqual(
            (build["build_id"], build["status"], build["terminal"]), (build_id, "ACCEPTED", False)
        )
        self.assertEqual(
            await self._counts(),
            {
                "commands": 1,
                "control_jobs": 1,
                "turn_jobs": 0,
                "sessions": 0,
                "public_sessions": 0,
                "builds": 1,
                "build_history": 1,
            },
        )
        self.assertEqual(await self._state(), committed)
        self._assert_no_artifact_side_effects()


if __name__ == "__main__":
    unittest.main()
