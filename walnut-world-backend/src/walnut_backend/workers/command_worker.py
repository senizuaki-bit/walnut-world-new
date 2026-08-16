"""Command worker that executes only newly-created commands and recovers non-terminals."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from yaya_agent_contracts import (
    CommandCreateReceipt,
    CommandRecord,
    CommandStatus,
    CommandStorePort,
    Failure,
    NewCommand,
    OperationContext,
    Result,
)


class CommandExecutor(Protocol):
    async def execute(self, command: CommandRecord, context: OperationContext) -> None: ...


class CommandWorker:
    def __init__(self, store: CommandStorePort, executor: CommandExecutor) -> None:
        self._store = store
        self._executor = executor

    async def accept_and_execute(
        self, command: NewCommand, context: OperationContext
    ) -> Result[CommandCreateReceipt]:
        receipt = await self._store.accept_once(command, context)
        if isinstance(receipt, Failure) or not receipt.value.created:
            return receipt
        await self._executor.execute(receipt.value.command, context)
        return receipt

    async def recover(
        self, context: OperationContext, *, older_than: datetime | None = None
    ) -> tuple[CommandRecord, ...]:
        recovered: list[CommandRecord] = []
        cursor: str | None = None
        recovery_cutoff = older_than or datetime.now(UTC)
        while True:
            page = await self._store.find_non_terminal_before(
                recovery_cutoff, cursor, 100, context
            )
            if isinstance(page, Failure):
                raise RuntimeError(f"cannot recover commands: {page.error.code}")
            for command in page.value.items:
                if command.status not in {
                    CommandStatus.ACCEPTED,
                    CommandStatus.VALIDATING,
                    CommandStatus.RUNNING_SANDBOX,
                    CommandStatus.APPLYING_WORLD,
                }:
                    raise RuntimeError(f"unknown non-terminal command status: {command.status}")
                await self._executor.execute(command, context)
                recovered.append(command)
            cursor = page.value.next_cursor
            if cursor is None:
                break
        return tuple(recovered)
