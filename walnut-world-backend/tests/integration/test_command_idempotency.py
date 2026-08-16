"""PostgreSQL contract tests for durable command acceptance and CAS transitions.

These tests deliberately require a real PostgreSQL database.  Set
``WALNUT_TEST_DATABASE_URL`` (for example
``postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/walnut_test``) and run
``py -3.12 -m alembic upgrade head`` before executing them.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    CommandStatus,
    CommandTransition,
    ContentRef,
    NewCommand,
    OperationContext,
    VersionSet,
)

from walnut_backend.adapters.postgres.command_store import PostgresCommandStore
from walnut_backend.adapters.postgres.session import create_session_factory


def test_command_idempotency_and_compare_and_swap_are_postgres_backed() -> None:
    """Same request replays, changed body conflicts, and stale CAS cannot win."""
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError(
            "PostgreSQL integration prerequisite missing: set WALNUT_TEST_DATABASE_URL; "
            "tests must not silently skip durable adapter coverage."
        )
    asyncio.run(_exercise_command_store(database_url))


def test_nonterminal_command_cursor_uses_the_ordering_tuple() -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError("set WALNUT_TEST_DATABASE_URL for required PostgreSQL command coverage")
    asyncio.run(_exercise_nonterminal_cursor(database_url))


async def _exercise_command_store(database_url: str) -> None:
    session_factory = create_session_factory(database_url)
    store = PostgresCommandStore(session_factory)
    try:
        run_id = uuid4().hex
        context = make_context(run_id=run_id, command_suffix="01")
        command = NewCommand(
            command_type="CREATE_AGENT_SESSION",
            idempotency_key=f"idempotency-command-{run_id}",
            request_sha256="1" * 64,
            versions=versions(),
        )

        first = await store.accept_once(command, context)
        assert first.ok
        assert first.value.created is True

        replay_context = make_context(
            run_id=run_id, command_suffix="02", trace_id=f"trace_{run_id}_replay"
        )
        replay = await store.accept_once(command, replay_context)
        assert replay.ok
        assert replay.value.created is False
        assert replay.value.command.command_id == first.value.command.command_id

        reused = await store.accept_once(replace(command, request_sha256="2" * 64), replay_context)
        assert not reused.ok
        assert reused.error.code == "IDEMPOTENCY_KEY_REUSED"

        other_actor = make_context(
            run_id=run_id, command_suffix="03", actor_id=f"actor_{run_id}_other"
        )
        actor_isolated = await store.accept_once(command, other_actor)
        assert actor_isolated.ok
        assert actor_isolated.value.created is True
        assert actor_isolated.value.command.command_id == other_actor.command_id

        previous = first.value.command
        next_record = replace(
            previous,
            status=CommandStatus.VALIDATING,
            stage="VALIDATE",
            updated_at=previous.updated_at + timedelta(microseconds=1),
            revision=previous.revision + 1,
        )
        transition = CommandTransition(previous_record=previous, next_record=next_record)
        transitioned = await store.transition(transition, context)
        assert transitioned.ok
        assert transitioned.value.revision == 2

        stale = await store.transition(transition, context)
        assert not stale.ok
        assert stale.error.code == "WORLD_REVISION_CONFLICT"
    finally:
        await session_factory.kw["bind"].dispose()


async def _exercise_nonterminal_cursor(database_url: str) -> None:
    session_factory = create_session_factory(database_url)
    store = PostgresCommandStore(session_factory)
    run_id = uuid4().hex
    ordering_time = datetime.now(UTC) + timedelta(minutes=5)
    first_context = replace(
        make_context(run_id=run_id, command_suffix="zzzz"),
        requested_at=ordering_time,
    )
    second_context = replace(
        make_context(run_id=run_id, command_suffix="aaaa"),
        requested_at=ordering_time,
    )
    try:
        records = []
        for ordinal, context in enumerate((first_context, second_context), start=1):
            accepted = await store.accept_once(
                NewCommand(
                    command_type="CREATE_AGENT_SESSION",
                    idempotency_key=f"idempotency-cursor-{run_id}-{ordinal}",
                    request_sha256=str(ordinal) * 64,
                    versions=versions(),
                ),
                context,
            )
            assert accepted.ok and accepted.value.created
            records.append(accepted.value.command)
        assert [record.updated_at for record in records] == [ordering_time, ordering_time]
        first_page = await store.find_non_terminal_before(
            datetime.now(UTC) + timedelta(days=1), None, 1, first_context
        )
        assert first_page.ok
        assert first_page.value.items[0].command_id == second_context.command_id
        assert first_page.value.next_cursor is not None
        second_page = await store.find_non_terminal_before(
            datetime.now(UTC) + timedelta(days=1), first_page.value.next_cursor, 1, first_context
        )
        assert second_page.ok
        assert [item.command_id for item in second_page.value.items] == [first_context.command_id]

        malformed = await store.find_non_terminal_before(
            datetime.now(UTC) + timedelta(days=1), "not-a-valid-cursor", 1, first_context
        )
        assert not malformed.ok
        assert malformed.error.code == "INVARIANT_VIOLATION"
    finally:
        await session_factory.kw["bind"].dispose()


def versions() -> VersionSet:
    return VersionSet(
        api_version="1.0.0",
        event_version="1.0.0",
        policy_version="policy-1",
        world_rules_version="world-1",
        teaching_spec_version="teaching-1",
    )


def make_context(
    *,
    run_id: str,
    command_suffix: str,
    actor_id: str | None = None,
    trace_id: str | None = None,
) -> OperationContext:
    return OperationContext(
        request_id=f"req_{run_id}",
        correlation_id=f"corr_{run_id}",
        trace_id=trace_id or f"trace_{run_id}",
        requested_at=datetime.now(UTC),
        actor=ActorRef(
            tenant_id=f"tenant_{run_id}",
            actor_id=actor_id or f"actor_{run_id}",
            actor_type=ActorType.STUDENT,
        ),
        content_ref=ContentRef(unit_id="UNIT_TEST", version="1.0.0", content_hash="0" * 64),
        command_id=f"cmd_{run_id}_{command_suffix}",
        causation_id=None,
    )
