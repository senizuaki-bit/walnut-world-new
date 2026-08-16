"""Worker contract tests; storage behavior remains covered by PostgreSQL integration tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from yaya_agent_contracts import CommandStatus, CursorPage, Success

from walnut_backend.workers.command_worker import CommandWorker
from walnut_backend.workers.outbox_worker import OutboxWorker


def test_command_worker_does_not_execute_replayed_receipt() -> None:
    executor = RecordingExecutor()
    worker = CommandWorker(
        store=SimpleNamespace(
            accept_once=lambda command, context: successful_async(
                SimpleNamespace(created=False, command=SimpleNamespace(command_id="cmd_replayed"))
            )
        ),
        executor=executor,
    )

    result = asyncio.run(worker.accept_and_execute(SimpleNamespace(), SimpleNamespace()))

    assert result.ok
    assert executor.executed == []


def test_command_worker_recovers_all_cursor_pages_and_rejects_unknown_state() -> None:
    commands = [SimpleNamespace(command_id=f"cmd_{index:03}", status=CommandStatus.ACCEPTED) for index in range(101)]
    store = PagedStore(commands)
    executor = RecordingExecutor()
    worker = CommandWorker(store=store, executor=executor)

    recovered = asyncio.run(worker.recover(SimpleNamespace(), older_than=datetime.now(UTC)))

    assert len(recovered) == 101
    assert len(executor.executed) == 101
    assert store.cursors == [None, "cursor-100"]

    unknown_worker = CommandWorker(
        store=PagedStore([SimpleNamespace(command_id="cmd_unknown", status="FUTURE_STATUS")]),
        executor=RecordingExecutor(),
    )
    with pytest.raises(RuntimeError, match="unknown non-terminal command status"):
        asyncio.run(unknown_worker.recover(SimpleNamespace(), older_than=datetime.now(UTC)))


def test_command_worker_uses_one_recovery_cutoff_when_execution_changes_nonterminal_status() -> None:
    command = SimpleNamespace(command_id="cmd_mutating", status=CommandStatus.ACCEPTED)
    store = MutatingPagedStore(command)
    executor = MutatingExecutor()
    worker = CommandWorker(store=store, executor=executor)

    recovered = asyncio.run(worker.recover(SimpleNamespace()))

    assert [item.command_id for item in recovered] == ["cmd_mutating"]
    assert executor.executed == [command]
    assert len(store.cutoffs) == 2
    assert store.cutoffs[0] == store.cutoffs[1]


def test_outbox_worker_rejects_unknown_event_type() -> None:
    worker = OutboxWorker(
        outbox=SimpleNamespace(
            claim_ready=lambda worker_id, limit, lease_seconds, context: successful_async(
                (SimpleNamespace(destination="UNSUPPORTED_EVENT"),)
            )
        ),
        delivery=SimpleNamespace(),
    )

    with pytest.raises(RuntimeError, match="unknown outbox event type"):
        asyncio.run(worker.run_once("worker", SimpleNamespace()))


async def successful_async(value: object) -> Success[object]:
    return Success(value)


class RecordingExecutor:
    def __init__(self) -> None:
        self.executed: list[object] = []

    async def execute(self, command: object, context: object) -> None:
        self.executed.append(command)


class PagedStore:
    def __init__(self, commands: list[object]) -> None:
        self._commands = commands
        self.cursors: list[str | None] = []

    async def find_non_terminal_before(
        self, updated_before: datetime, cursor: str | None, limit: int, context: object
    ) -> Success[CursorPage[object]]:
        self.cursors.append(cursor)
        if cursor is None:
            return Success(CursorPage(items=tuple(self._commands[:100]), next_cursor="cursor-100"))
        return Success(CursorPage(items=tuple(self._commands[100:]), next_cursor=None))


class MutatingExecutor(RecordingExecutor):
    async def execute(self, command: object, context: object) -> None:
        await super().execute(command, context)
        command.status = CommandStatus.VALIDATING


class MutatingPagedStore:
    def __init__(self, command: object) -> None:
        self._command = command
        self.cutoffs: list[datetime] = []

    async def find_non_terminal_before(
        self, updated_before: datetime, cursor: str | None, limit: int, context: object
    ) -> Success[CursorPage[object]]:
        self.cutoffs.append(updated_before)
        if cursor is None:
            return Success(CursorPage(items=(self._command,), next_cursor="after-mutating"))
        if updated_before is not self.cutoffs[0] and self._command.status is CommandStatus.VALIDATING:
            return Success(CursorPage(items=(self._command,), next_cursor=None))
        return Success(CursorPage(items=(), next_cursor=None))
