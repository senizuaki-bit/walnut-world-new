"""Alembic entrypoint; URL comes from environment so migrations never hit a hidden database."""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config
database_url = os.getenv("WALNUT_DATABASE_URL") or os.getenv("WALNUT_TEST_DATABASE_URL")
if not database_url:
    raise RuntimeError("Set WALNUT_DATABASE_URL or WALNUT_TEST_DATABASE_URL before running migrations")
if database_url.startswith("postgres://"):
    database_url = "postgresql+asyncpg://" + database_url.removeprefix("postgres://")
elif database_url.startswith("postgresql://"):
    database_url = "postgresql+asyncpg://" + database_url.removeprefix("postgresql://")
config.set_main_option("sqlalchemy.url", database_url)


def do_run_migrations(connection: object) -> None:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}), prefix="sqlalchemy."
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


run_migrations_online()
