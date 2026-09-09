"""Async SQLAlchemy session construction for PostgreSQL only."""

from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session


def normalize_database_url(database_url: str) -> str:
    """Reject accidental non-PostgreSQL stores instead of falling back to memory/SQLite."""
    if database_url.startswith("postgres://"):
        return "postgresql+asyncpg://" + database_url.removeprefix("postgres://")
    if database_url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + database_url.removeprefix("postgresql://")
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url
    raise ValueError("PostgreSQL URL required; durable adapters never use an in-memory fallback")


def create_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(normalize_database_url(database_url), pool_pre_ping=True)


def create_session_factory(database_url: str) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(create_engine(database_url), expire_on_commit=False)


@asynccontextmanager
async def snapshot_read(sessions: async_sessionmaker[AsyncSession]):
    """Reuse repeated SELECT results inside one immutable database snapshot.

    Validation still runs. Only identical SQL and bound values reuse rows, and
    the cache disappears with this read transaction; no write path uses it.
    """
    async with sessions() as session, session.begin():
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        cached = {}

        def reuse_rows(state):
            statement = state.statement
            if not state.is_select or getattr(statement, "_for_update_arg", None) is not None:
                return state.invoke_statement()
            # Skip expressions without a mapped table, such as database clocks.
            if not any(item.get("entity") is not None for item in statement.column_descriptions):
                return state.invoke_statement()
            key = statement._generate_cache_key()
            if key is None:
                return state.invoke_statement()
            identity = (key.key, repr([item.value for item in key.bindparams]), repr(state.parameters))
            if identity not in cached:
                cached[identity] = state.invoke_statement().freeze()
            return cached[identity]()

        sync_session = getattr(session, "sync_session", None)
        if isinstance(sync_session, Session):
            event.listen(sync_session, "do_orm_execute", reuse_rows, retval=True)
        try:
            yield session
        finally:
            if isinstance(sync_session, Session):
                event.remove(sync_session, "do_orm_execute", reuse_rows)
