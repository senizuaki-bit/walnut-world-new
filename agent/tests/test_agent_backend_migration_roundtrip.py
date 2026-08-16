from __future__ import annotations

import sys
from pathlib import Path
from typing import LiteralString, cast
from unittest import IsolatedAsyncioTestCase

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from postgres_test_support import postgres_test_server  # noqa: E402
from psycopg import AsyncConnection, sql  # noqa: E402
from psycopg.errors import ObjectNotInPrerequisiteState  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from yaya_agent_backend.database import PostgresDatabase  # noqa: E402

MIGRATIONS_ROOT = PACKAGE_ROOT / "yaya_agent_backend" / "migrations"
MIGRATIONS = tuple(
    MIGRATIONS_ROOT / name
    for name in (
        "0001_agent_turn.sql",
        "0002_learner_projection.sql",
        "0003_student_skill_chain.sql",
    )
)
DOWN_MIGRATION = MIGRATIONS_ROOT / "0003_student_skill_chain.down.sql"
_A8_TABLES = (
    "yaya_public_agent_sessions",
    "yaya_skill_draft_revisions",
    "yaya_skill_draft_heads",
    "yaya_product_write_receipts",
    "yaya_skill_builds",
    "yaya_artifacts",
    "yaya_skill_certifications",
    "yaya_registry_entries",
    "yaya_skill_activations",
)


class AgentBackendMigrationRoundTripTests(IsolatedAsyncioTestCase):
    async def test_forward_transaction_rollback_restores_pre_a8_schema(self) -> None:
        with postgres_test_server() as postgres:
            database = PostgresDatabase(postgres.dsn)
            connection = await database.connect()
            try:
                async with connection.transaction():
                    for migration in MIGRATIONS[:2]:
                        await connection.execute(
                            sql.SQL(cast(LiteralString, migration.read_text(encoding="utf-8")))
                        )

                self.assertEqual(await self._a8_table_count(connection), 0)
                self.assertFalse(await self._skill_session_is_nullable(connection))

                async with connection.transaction(force_rollback=True):
                    await connection.execute(
                        sql.SQL(cast(LiteralString, MIGRATIONS[2].read_text(encoding="utf-8")))
                    )
                    self.assertEqual(await self._a8_table_count(connection), len(_A8_TABLES))
                    self.assertTrue(await self._skill_session_is_nullable(connection))

                self.assertEqual(await self._a8_table_count(connection), 0)
                self.assertFalse(await self._skill_session_is_nullable(connection))

                async with connection.transaction():
                    await connection.execute(
                        sql.SQL(cast(LiteralString, MIGRATIONS[2].read_text(encoding="utf-8")))
                    )
                self.assertEqual(await self._a8_table_count(connection), len(_A8_TABLES))
                self.assertTrue(await self._skill_session_is_nullable(connection))
            finally:
                await connection.close()

    async def test_packaged_migrator_reentry_is_ledger_idempotent(self) -> None:
        with postgres_test_server() as postgres:
            database = PostgresDatabase(postgres.dsn)
            await database.migrate()
            await database.migrate()

            connection = await database.connect(autocommit=True)
            try:
                cursor = await connection.execute(
                    """
                    SELECT count(*)::int AS migrations,
                           count(DISTINCT name)::int AS distinct_migrations
                    FROM yaya_schema_migrations
                    """
                )
                row = await cursor.fetchone()
                self.assertEqual(
                    row,
                    {"migrations": len(MIGRATIONS), "distinct_migrations": len(MIGRATIONS)},
                )
                self.assertEqual(await self._a8_table_count(connection), len(_A8_TABLES))
            finally:
                await connection.close()

    async def test_committed_forward_down_and_reapply_round_trip(self) -> None:
        with postgres_test_server() as postgres:
            database = PostgresDatabase(postgres.dsn)
            await database.migrate()

            connection = await database.connect()
            try:
                self.assertEqual(await self._a8_table_count(connection), len(_A8_TABLES))
                self.assertTrue(await self._skill_session_is_nullable(connection))

                async with connection.transaction():
                    await connection.execute(
                        sql.SQL(
                            cast(
                                LiteralString,
                                DOWN_MIGRATION.read_text(encoding="utf-8"),
                            )
                        )
                    )

                self.assertEqual(await self._a8_table_count(connection), 0)
                self.assertFalse(await self._skill_session_is_nullable(connection))
                self.assertEqual(
                    await self._migration_ledger(connection),
                    ("0001_agent_turn.sql", "0002_learner_projection.sql"),
                )
            finally:
                await connection.close()

            await database.migrate()
            reloaded = await database.connect(autocommit=True)
            try:
                self.assertEqual(await self._a8_table_count(reloaded), len(_A8_TABLES))
                self.assertTrue(await self._skill_session_is_nullable(reloaded))
                self.assertEqual(
                    await self._migration_ledger(reloaded),
                    tuple(migration.name for migration in MIGRATIONS),
                )
            finally:
                await reloaded.close()

    async def test_down_refuses_nonempty_a8_authority_without_partial_schema_loss(self) -> None:
        with postgres_test_server() as postgres:
            database = PostgresDatabase(postgres.dsn)
            await database.migrate()
            connection = await database.connect()
            try:
                async with connection.transaction():
                    await connection.execute(
                        """
                        INSERT INTO yaya_learners(
                            tenant_id,learner_id,actor_id,content_hash,
                            record_sha256,record_json
                        ) VALUES (%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            "tenant_down_guard_0001",
                            "learner_down_guard_0001",
                            "actor_down_guard_0001",
                            "1" * 64,
                            "2" * 64,
                            Jsonb({"authority": "nonempty"}),
                        ),
                    )

                with self.assertRaisesRegex(
                    ObjectNotInPrerequisiteState,
                    "refusing 0003 downgrade: A8 authority table yaya_learners is not empty",
                ):
                    async with connection.transaction():
                        await connection.execute(
                            sql.SQL(
                                cast(
                                    LiteralString,
                                    DOWN_MIGRATION.read_text(encoding="utf-8"),
                                )
                            )
                        )
            finally:
                await connection.close()

            verifier = await database.connect(autocommit=True)
            try:
                self.assertEqual(await self._a8_table_count(verifier), len(_A8_TABLES))
                self.assertTrue(await self._skill_session_is_nullable(verifier))
                cursor = await verifier.execute("SELECT count(*)::int AS total FROM yaya_learners")
                self.assertEqual(await cursor.fetchone(), {"total": 1})
                self.assertEqual(
                    await self._migration_ledger(verifier),
                    tuple(migration.name for migration in MIGRATIONS),
                )
            finally:
                await verifier.close()

    @staticmethod
    async def _a8_table_count(connection: AsyncConnection[dict[str, object]]) -> int:
        cursor = await connection.execute(
            """
            SELECT count(*)::int AS total
            FROM unnest(%s::text[]) AS expected(name)
            WHERE to_regclass('public.' || expected.name) IS NOT NULL
            """,
            (list(_A8_TABLES),),
        )
        row = await cursor.fetchone()
        if row is None:
            raise AssertionError("A8 table fingerprint query returned no row")
        return cast(int, row["total"])

    @staticmethod
    async def _skill_session_is_nullable(
        connection: AsyncConnection[dict[str, object]],
    ) -> bool:
        cursor = await connection.execute(
            """
            SELECT is_nullable
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='yaya_skills'
              AND column_name='session_id'
            """
        )
        row = await cursor.fetchone()
        if row is None:
            raise AssertionError("yaya_skills.session_id column is missing")
        return row["is_nullable"] == "YES"

    @staticmethod
    async def _migration_ledger(
        connection: AsyncConnection[dict[str, object]],
    ) -> tuple[str, ...]:
        cursor = await connection.execute("SELECT name FROM yaya_schema_migrations ORDER BY name")
        return tuple(str(row["name"]) for row in await cursor.fetchall())


if __name__ == "__main__":
    import unittest

    unittest.main()
