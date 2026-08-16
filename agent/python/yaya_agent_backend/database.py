"""Small async psycopg boundary with migration locking."""

from __future__ import annotations

import asyncio
import hashlib
import re
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import LiteralString, cast

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import dict_row

_FORWARD_MIGRATION_NAME = re.compile(r"^[0-9]{4}_[a-z0-9_]+\.sql$")

# psycopg's async libpq integration requires a selector loop on Windows.  Set
# the process policy while this production adapter is imported, before service
# or unittest loops are created; the provider-neutral Runtime remains untouched.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(  # pyright: ignore[reportDeprecated]
        asyncio.WindowsSelectorEventLoopPolicy()
    )


class PostgresCommitStateUnknown(RuntimeError):
    """The transaction body completed, but PostgreSQL did not confirm COMMIT."""


def _commit_outcome_is_unknown(error: psycopg.Error) -> bool:
    """Server-confirmed rollback SQLSTATEs are known, transport loss is not."""

    sqlstate = error.sqlstate
    # psycopg's SerializationFailure and DeadlockDetected inherit from
    # OperationalError, even though PostgreSQL has explicitly confirmed that
    # those transactions were rolled back.  Classify by the server outcome
    # first; only a missing outcome or connection-class SQLSTATE is ambiguous.
    if sqlstate is not None:
        return sqlstate.startswith("08")
    return True


@asynccontextmanager
async def transaction_with_commit_boundary_on(
    connection: AsyncConnection[dict[str, object]],
) -> AsyncGenerator[AsyncConnection[dict[str, object]]]:
    """Apply the same commit classification to an already-owned connection."""

    body_completed = False
    try:
        async with connection.transaction():
            yield connection
            body_completed = True
    except psycopg.Error as error:
        if body_completed and _commit_outcome_is_unknown(error):
            raise PostgresCommitStateUnknown("PostgreSQL did not acknowledge COMMIT") from error
        raise


class PostgresDatabase:
    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN cannot be blank")
        self.dsn = dsn

    async def connect(self, *, autocommit: bool = False) -> AsyncConnection[dict[str, object]]:
        retry_delays = (0.0, 0.25, 0.5, 1.0, 2.0)
        for attempt, delay_seconds in enumerate(retry_delays):
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
            try:
                return await AsyncConnection[dict[str, object]].connect(
                    self.dsn,
                    autocommit=autocommit,
                    row_factory=dict_row,
                )
            except psycopg.OperationalError:
                if attempt == len(retry_delays) - 1:
                    raise
        raise AssertionError("PostgreSQL retry loop did not terminate")

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[AsyncConnection[dict[str, object]]]:
        connection = await self.connect()
        try:
            async with connection.transaction():
                yield connection
        finally:
            await connection.close()

    @asynccontextmanager
    async def transaction_with_commit_boundary(
        self,
    ) -> AsyncGenerator[AsyncConnection[dict[str, object]]]:
        """Expose commit-roundtrip uncertainty separately from known rollback.

        Statement failures raised into the context are rolled back and retain
        their original psycopg type.  Only a database error raised after the
        caller's body completed is classified as an unknown COMMIT outcome.
        """

        connection = await self.connect()
        body_completed = False
        try:
            try:
                async with connection.transaction():
                    yield connection
                    body_completed = True
            except psycopg.Error as error:
                if body_completed and _commit_outcome_is_unknown(error):
                    raise PostgresCommitStateUnknown(
                        "PostgreSQL did not acknowledge COMMIT"
                    ) from error
                raise
        finally:
            await connection.close()

    async def migrate(self) -> None:
        migrations_root = files("yaya_agent_backend.migrations")
        resources = sorted(
            (
                item
                for item in migrations_root.iterdir()
                if _FORWARD_MIGRATION_NAME.fullmatch(item.name) is not None
            ),
            key=lambda item: item.name,
        )
        if not resources:
            raise RuntimeError("no PostgreSQL migrations are packaged")
        connection = await self.connect()
        try:
            async with connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock(%s)", (0x59415941,))
                await connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS yaya_schema_migrations (
                        name TEXT PRIMARY KEY,
                        sha256 CHAR(64) NOT NULL,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
                    )
                    """
                )
                for resource in resources:
                    sql = resource.read_text(encoding="utf-8")
                    digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                    result = await connection.execute(
                        "SELECT sha256 FROM yaya_schema_migrations WHERE name = %s",
                        (resource.name,),
                    )
                    row = await result.fetchone()
                    if row is not None:
                        if row["sha256"] != digest:
                            raise RuntimeError(
                                f"applied migration {resource.name} has immutable hash drift"
                            )
                        continue
                    # Migration bytes are immutable package resources, not
                    # caller input; LiteralString documents that trust boundary.
                    await connection.execute(cast(LiteralString, sql), prepare=False)
                    await connection.execute(
                        "INSERT INTO yaya_schema_migrations(name, sha256) VALUES (%s, %s)",
                        (resource.name, digest),
                    )
        except psycopg.Error:
            raise
        finally:
            await connection.close()


__all__ = [
    "PostgresCommitStateUnknown",
    "PostgresDatabase",
    "transaction_with_commit_boundary_on",
]
