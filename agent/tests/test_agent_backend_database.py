from __future__ import annotations

import asyncio
import hashlib
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

import psycopg  # noqa: E402
from postgres_test_support import postgres_test_server  # noqa: E402
from yaya_agent_backend.database import PostgresDatabase  # noqa: E402

MIGRATIONS_ROOT = PACKAGE_ROOT / "yaya_agent_backend" / "migrations"
EXPECTED_MIGRATIONS = (
    "0001_agent_turn.sql",
    "0002_learner_projection.sql",
    "0003_student_skill_chain.sql",
)
MIGRATIONS = tuple(MIGRATIONS_ROOT / name for name in EXPECTED_MIGRATIONS)


def _docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments],
        check=check,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _wait_ready(container_name: str) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        ready = _docker(
            "exec",
            container_name,
            "pg_isready",
            "--username",
            "yaya_test",
            "--dbname",
            "yaya_test",
            check=False,
        )
        if ready.returncode == 0:
            return
        time.sleep(0.25)
    raise AssertionError("restarted PostgreSQL did not become ready within 45 seconds")


def _published_port(container_name: str) -> int:
    published = _docker("port", container_name, "5432/tcp").stdout.strip()
    match = re.search(r":([0-9]{1,5})$", published)
    if match is None:
        raise AssertionError(f"cannot resolve restarted PostgreSQL port from {published!r}")
    return int(match.group(1))


def _wait_host_psycopg_ready(dsn: str) -> None:
    deadline = time.monotonic() + 45
    last_error: psycopg.Error | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=1) as connection:
                connection.execute("SELECT 1")
            return
        except psycopg.Error as error:
            last_error = error
            time.sleep(0.25)
    raise AssertionError(
        "restarted PostgreSQL never became reachable through its fixed host DSN"
    ) from last_error


class AgentBackendDatabaseTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._server_context = postgres_test_server(fixed_host_port=True)
        try:
            cls.server = cls._server_context.__enter__()
            cls.database = PostgresDatabase(cls.server.dsn)
        except BaseException:
            cls._server_context.__exit__(*sys.exc_info())
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server_context.__exit__(None, None, None)

    async def _count(self, table: str, predicate: str = "TRUE") -> int:
        if not table.startswith("yaya_") or not predicate.isascii():
            raise AssertionError("test SQL identifier or predicate is unsafe")
        async with self.database.transaction() as connection:
            result = await connection.execute(
                f"SELECT count(*) AS count FROM {table} WHERE {predicate}"
            )
            row = await result.fetchone()
        if row is None or not isinstance(row["count"], int):
            raise AssertionError("PostgreSQL count query returned no integer")
        return row["count"]

    async def test_concurrent_migration_is_serialized_and_hash_drift_fails(self) -> None:
        await asyncio.gather(self.database.migrate(), self.database.migrate())
        async with self.database.transaction() as connection:
            result = await connection.execute(
                "SELECT name FROM yaya_schema_migrations ORDER BY name"
            )
            rows = await result.fetchall()
        self.assertEqual(
            tuple(str(row["name"]) for row in rows),
            EXPECTED_MIGRATIONS,
        )

        for migration in MIGRATIONS:
            with self.subTest(migration=migration.name):
                migration_text = migration.read_text(encoding="utf-8")
                actual_digest = hashlib.sha256(migration_text.encode("utf-8")).hexdigest()
                async with self.database.transaction() as connection:
                    await connection.execute(
                        "UPDATE yaya_schema_migrations SET sha256 = %s WHERE name = %s",
                        ("0" * 64, migration.name),
                    )
                with self.assertRaisesRegex(RuntimeError, "immutable hash drift"):
                    await self.database.migrate()
                async with self.database.transaction() as connection:
                    await connection.execute(
                        "UPDATE yaya_schema_migrations SET sha256 = %s WHERE name = %s",
                        (actual_digest, migration.name),
                    )

    async def test_package_migration_installs_terminal_projection_audit(self) -> None:
        await self.database.migrate()
        async with self.database.transaction() as connection:
            table_cursor = await connection.execute(
                """
                SELECT to_regclass(
                    'public.yaya_learner_projection_terminal_audits'
                ) AS relation
                """
            )
            table_row = await table_cursor.fetchone()
            trigger_cursor = await connection.execute(
                """
                SELECT tgname FROM pg_trigger
                WHERE NOT tgisinternal AND tgname IN (
                    'yaya_learner_projection_terminal_audit_enqueue',
                    'yaya_learner_projection_terminal_audit_immutable'
                )
                ORDER BY tgname
                """
            )
            trigger_rows = await trigger_cursor.fetchall()
        self.assertIsNotNone(table_row)
        if table_row is not None:
            self.assertEqual(
                table_row["relation"],
                "yaya_learner_projection_terminal_audits",
            )
        self.assertEqual(
            [row["tgname"] for row in trigger_rows],
            [
                "yaya_learner_projection_terminal_audit_enqueue",
                "yaya_learner_projection_terminal_audit_immutable",
            ],
        )

    async def test_database_restart_aborts_open_transaction_without_partial_record(self) -> None:
        await self.database.migrate()
        digest = "e" * 64
        port_before = _published_port(self.server.container_name)
        connection = await self.database.connect()
        try:
            await connection.execute("BEGIN")
            await connection.execute(
                """
                INSERT INTO yaya_commands(
                    tenant_id, actor_id, operation, idempotency_key, command_id,
                    request_sha256, content_hash, revision, status, updated_at, record_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 'ACCEPTED',
                          clock_timestamp(), '{}'::jsonb)
                """,
                (
                    "tenant_interruption",
                    "student_interruption_0001",
                    "EXECUTE_AGENT_TURN",
                    "idem_interruption_0001",
                    "cmd_interruption_0001",
                    digest,
                    digest,
                ),
            )
            await asyncio.to_thread(
                _docker,
                "restart",
                "--time",
                "0",
                self.server.container_name,
            )
            with self.assertRaises(psycopg.Error):
                await connection.execute("SELECT 1")
        finally:
            try:
                await connection.close()
            except psycopg.Error:
                pass
        await asyncio.to_thread(_wait_ready, self.server.container_name)
        port_after = _published_port(self.server.container_name)
        self.assertEqual(
            port_after,
            port_before,
            "fixed PostgreSQL test port changed across docker restart",
        )
        await asyncio.to_thread(_wait_host_psycopg_ready, self.server.dsn)
        self.database = PostgresDatabase(self.server.dsn)
        self.assertEqual(
            await self._count(
                "yaya_commands",
                "command_id='cmd_interruption_0001'",
            ),
            0,
        )

    async def test_application_exception_rolls_back_command_and_job(self) -> None:
        await self.database.migrate()
        digest = "f" * 64
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO yaya_tasks(
                    tenant_id, task_id, actor_id, content_hash, snapshot_json
                ) VALUES (%s, %s, %s, %s, '{}'::jsonb)
                """,
                (
                    "tenant_rollback",
                    "task_rollback_0001",
                    "student_rollback_0001",
                    digest,
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_worlds(
                    tenant_id, world_id, actor_id, content_hash, stream_id, revision,
                    last_event_sequence, state_hash, world_rules_version, state_json,
                    request_context_json
                ) VALUES (%s, %s, %s, %s, %s, 5, 40, %s, 'farm-rules-1',
                          '{}'::jsonb, '{}'::jsonb)
                """,
                (
                    "tenant_rollback",
                    "world_rollback_0001",
                    "student_rollback_0001",
                    digest,
                    "stream_rollback_0001",
                    digest,
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_agent_sessions(
                    tenant_id, session_id, actor_id, task_id, world_id,
                    content_hash, snapshot_json
                ) VALUES (%s, %s, %s, %s, %s, %s, '{}'::jsonb)
                """,
                (
                    "tenant_rollback",
                    "session_rollback_0001",
                    "student_rollback_0001",
                    "task_rollback_0001",
                    "world_rollback_0001",
                    digest,
                ),
            )

        class InjectedFailure(RuntimeError):
            pass

        with self.assertRaises(InjectedFailure):
            async with self.database.transaction() as connection:
                await connection.execute(
                    """
                    INSERT INTO yaya_commands(
                        tenant_id, actor_id, operation, idempotency_key, command_id,
                        session_id, turn_id, client_turn_sequence, request_sha256,
                        content_hash, revision, status, updated_at, record_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s, 1, 'ACCEPTED',
                              clock_timestamp(), '{}'::jsonb)
                    """,
                    (
                        "tenant_rollback",
                        "student_rollback_0001",
                        "EXECUTE_AGENT_TURN",
                        "idem_rollback_0001",
                        "cmd_rollback_0001",
                        "session_rollback_0001",
                        "turn_rollback_0001",
                        digest,
                        digest,
                    ),
                )
                await connection.execute(
                    """
                    INSERT INTO yaya_command_jobs(
                        tenant_id, command_id, job_id, actor_id, content_hash, session_id,
                        turn_id, client_turn_sequence, event_json, operation_context_json,
                        request_body, accepted_receipt_json, created_at
                    ) VALUES (%s, %s, 'job_rollback_00000001', %s, %s, %s, %s, 1,
                              '{}'::jsonb, '{}'::jsonb,
                              convert_to('{"event_id":"event_rollback_0001"}','UTF8'),
                              '{"job_id":"job_rollback_00000001",'
                              '"job_type":"EXECUTE_AGENT_TURN","status":"ACCEPTED",'
                              '"created_at":"2026-08-09T00:00:00Z",'
                              '"updated_at":"2026-08-09T00:00:00Z",'
                              '"command_id":"cmd_rollback_0001",'
                              '"trace_id":"trace_rollback_00000001","error":null}'::jsonb,
                              clock_timestamp())
                    """,
                    (
                        "tenant_rollback",
                        "cmd_rollback_0001",
                        "student_rollback_0001",
                        digest,
                        "session_rollback_0001",
                        "turn_rollback_0001",
                    ),
                )
                raise InjectedFailure("database fault after job insert")

        self.assertEqual(
            await self._count("yaya_commands", "command_id='cmd_rollback_0001'"),
            0,
        )
        self.assertEqual(
            await self._count("yaya_command_jobs", "command_id='cmd_rollback_0001'"),
            0,
        )


if __name__ == "__main__":
    unittest.main()
