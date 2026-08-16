"""Shared durable workflow worker loop with bounded retry and dead-letter closure."""

from __future__ import annotations

import asyncio
import errno
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Protocol

from asyncpg.exceptions import (
    CannotConnectNowError,
    PostgresConnectionError,
)
from sqlalchemy import func, select
from sqlalchemy.exc import (
    DBAPIError,
    DisconnectionError,
)
from sqlalchemy.exc import (
    TimeoutError as SqlTimeoutError,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    CommandRecord,
    CommandStatus,
    CommandTransition,
    ContractError,
    ErrorCategory,
    Failure,
    OperationContext,
)
from yaya_agent_runtime import AgentRuntimeError

from walnut_backend.adapters.postgres.command_store import PostgresCommandStore
from walnut_backend.adapters.postgres.models import (
    CommandRow,
    JobStepReceiptRow,
    command_record_from_data,
)
from walnut_backend.adapters.postgres.workflow_jobs import (
    ClaimedWorkflowJob,
    PostgresWorkflowJobStore,
    WorkflowBoundaryError,
    WorkflowFenceLost,
    WorkflowInvariantError,
    WorkflowReconciliationPending,
    WorkflowRetryableError,
)


class WorkflowHandler(Protocol):
    operations: frozenset[str]

    async def execute(self, claim: ClaimedWorkflowJob) -> None: ...


_AGENT_ERROR_DETAIL_KEYS = frozenset(
    {
        "actual",
        "event_type",
        "expected",
        "field",
        "maximum",
        "required",
        "role",
    }
)


def _sanitized_failure(error: Exception, *, attempt: int) -> dict[str, Any]:
    """Return one bounded diagnostic without persisting prompts or dependency data."""

    sanitized: dict[str, Any] = {
        "code": "WORKFLOW_EXECUTION_FAILED",
        "exception_type": type(error).__name__,
        "attempt": attempt,
    }
    if isinstance(error, AgentRuntimeError):
        runtime_error: dict[str, Any] = {"code": error.code}
        details = {
            key: value
            for key, value in error.details.items()
            if key in _AGENT_ERROR_DETAIL_KEYS
            and (
                value is None
                or isinstance(value, bool | int)
                or (
                    isinstance(value, str)
                    and len(value) <= 128
                    and "\n" not in value
                    and "\r" not in value
                )
            )
        }
        if details:
            runtime_error["details"] = details
        sanitized["runtime_error"] = runtime_error
    elif isinstance(error, WorkflowBoundaryError):
        sanitized["boundary_stage"] = error.stage
    return sanitized


class WorkflowWorker:
    """Claim one Job at a time and never retry without releasing its lease."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        jobs: PostgresWorkflowJobStore,
        commands: PostgresCommandStore,
        handlers: tuple[WorkflowHandler, ...],
        worker_id: str,
        lease_seconds: int = 120,
        maximum_attempts: int = 5,
        retry_base_seconds: int = 2,
        retry_max_seconds: int = 60,
        database_retry_base_seconds: float = 0.25,
        database_retry_max_seconds: float = 5.0,
    ) -> None:
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must be a bounded non-empty string")
        if not 1 <= maximum_attempts <= 100:
            raise ValueError("maximum_attempts must be between 1 and 100")
        if not 1 <= retry_base_seconds <= retry_max_seconds <= 3600:
            raise ValueError("retry delay bounds are invalid")
        if not 0.01 <= database_retry_base_seconds <= database_retry_max_seconds <= 60:
            raise ValueError("database retry delay bounds are invalid")
        by_operation: dict[str, WorkflowHandler] = {}
        for handler in handlers:
            for operation in handler.operations:
                if operation in by_operation:
                    raise ValueError(f"duplicate workflow handler for {operation}")
                by_operation[operation] = handler
        if not by_operation:
            raise ValueError("at least one workflow handler is required")
        self._sessions = session_factory
        self._jobs = jobs
        self._commands = commands
        self._handlers = by_operation
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._maximum_attempts = maximum_attempts
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._database_retry_base_seconds = database_retry_base_seconds
        self._database_retry_max_seconds = database_retry_max_seconds

    async def run_once(self, tenant_id: str) -> bool:
        claim = await self._jobs.claim_next(
            tenant_id=tenant_id,
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        if claim is None:
            return False
        handler = self._handlers.get(claim.operation)
        if handler is None:
            await self._record_unexpected(
                claim,
                WorkflowInvariantError(f"no workflow handler is registered for {claim.operation}"),
                force_dead_letter=True,
            )
            return True
        try:
            await handler.execute(claim)
        except WorkflowFenceLost:
            # A newer fencing token owns reconciliation.  The stale worker must
            # not mutate the Job, Command, or resource.
            return True
        except Exception as error:
            try:
                await self._record_unexpected(claim, error)
            except WorkflowFenceLost:
                # A terminal hand-off can commit while its acknowledgement is
                # lost. The old Turn fence must not rewrite WAITING_PROJECTION;
                # the independent learner queue now owns recovery.
                return True
        return True

    async def run_forever(
        self,
        tenant_id: str,
        *,
        stop: asyncio.Event,
        idle_poll_seconds: float = 0.5,
    ) -> None:
        if not 0.01 <= idle_poll_seconds <= 60:
            raise ValueError("idle_poll_seconds must be between 0.01 and 60")
        database_failures = 0
        while not stop.is_set():
            try:
                processed = await self.run_once(tenant_id)
                database_failures = 0
            except Exception as error:
                if not _temporary_database_error(error):
                    raise
                database_failures += 1
                await _wait_or_stop(
                    stop,
                    min(
                        self._database_retry_max_seconds,
                        self._database_retry_base_seconds * (2 ** (database_failures - 1)),
                    ),
                )
                continue
            if processed:
                continue
            await _wait_or_stop(stop, idle_poll_seconds)

    async def _record_unexpected(
        self,
        claim: ClaimedWorkflowJob,
        error: Exception,
        *,
        force_dead_letter: bool = False,
    ) -> None:
        sanitized = _sanitized_failure(error, attempt=claim.attempt)
        retry_after_seconds = (
            error.retry_after_seconds if isinstance(error, WorkflowRetryableError) else None
        )
        if retry_after_seconds is not None:
            sanitized["retry_after_seconds"] = retry_after_seconds
        async with self._sessions() as session, session.begin():
            reconciliation_wait = isinstance(error, WorkflowReconciliationPending)
            previous_failures = await session.scalar(
                select(func.count(JobStepReceiptRow.receipt_id)).where(
                    JobStepReceiptRow.tenant_id == claim.tenant_id,
                    JobStepReceiptRow.job_id == claim.job_id,
                    JobStepReceiptRow.step_name.like("WORKER_FAILURE_%"),
                )
            )
            if not isinstance(previous_failures, int):
                raise WorkflowInvariantError("worker failure receipt count is invalid")
            dead_letter = force_dead_letter or _failure_budget_exhausted(
                error,
                previous_failures=previous_failures,
                maximum_attempts=self._maximum_attempts,
            )
            step_name = (
                f"WORKER_RECONCILE_{claim.fencing_token}"
                if reconciliation_wait
                else f"WORKER_FAILURE_{claim.attempt}"
            )
            await self._jobs.record_step_in_session(
                session,
                claim,
                step_name=step_name,
                input_sha256=claim.request_sha256,
                output=sanitized,
            )
            if not dead_letter:
                delay = _retry_delay_seconds(
                    error,
                    attempt=claim.attempt,
                    retry_base_seconds=self._retry_base_seconds,
                    retry_max_seconds=self._retry_max_seconds,
                )
                await self._jobs.retry_in_session(
                    session,
                    claim,
                    delay_seconds=delay,
                    phase=claim.phase,
                    error=sanitized,
                )
                return
            await self._fail_command(session, claim, sanitized)
            await self._jobs.finish_in_session(
                session,
                claim,
                status="DEAD_LETTER",
                phase=claim.phase,
                error=sanitized,
            )

    async def _fail_command(
        self,
        session: AsyncSession,
        claim: ClaimedWorkflowJob,
        details: Mapping[str, Any],
    ) -> None:
        row = await session.scalar(
            select(CommandRow)
            .where(
                CommandRow.tenant_id == claim.tenant_id,
                CommandRow.command_id == claim.command_id,
            )
            .with_for_update()
        )
        if row is None:
            raise WorkflowInvariantError("dead-letter workflow lost its Command")
        command = command_record_from_data(row.record_json)
        if command.terminal:
            if command.status is not CommandStatus.FAILED:
                raise WorkflowInvariantError(
                    "dead-letter workflow has a conflicting terminal Command"
                )
            return
        failure = ContractError(
            code="INTERNAL_ERROR",
            category=ErrorCategory.INTERNAL,
            retryable=False,
            user_message_key="system.internal_error",
            stage=command.stage,
            message="Workflow exhausted bounded recovery attempts.",
            details=dict(details),
        )
        now = await _database_now(session)
        terminal = replace(
            command,
            status=CommandStatus.FAILED,
            terminal=True,
            result=None,
            error=failure,
            revision=command.revision + 1,
            updated_at=now,
        )
        transitioned = await self._commands.transition_in_session(
            session,
            CommandTransition(command, terminal),
            _operation_context(command),
        )
        if isinstance(transitioned, Failure):
            raise WorkflowInvariantError("dead-letter Command CAS was lost")


def _retry_delay_seconds(
    error: Exception,
    *,
    attempt: int,
    retry_base_seconds: int,
    retry_max_seconds: int,
) -> int:
    if isinstance(error, WorkflowRetryableError) and error.retry_after_seconds is not None:
        return error.retry_after_seconds
    exponent = max(0, attempt - 1)
    return min(retry_max_seconds, retry_base_seconds * (2**exponent))


def _failure_budget_exhausted(
    error: Exception,
    *,
    previous_failures: int,
    maximum_attempts: int,
) -> bool:
    if previous_failures < 0 or maximum_attempts < 1:
        raise ValueError("workflow failure budget inputs are invalid")
    if isinstance(error, WorkflowInvariantError) and not isinstance(
        error, WorkflowBoundaryError
    ):
        # Retrying corrupt durable authority cannot repair it and only repeats
        # downstream work. Boundary failures retain their existing bounded
        # retry behavior because they can represent a recoverable hand-off.
        return True
    return not isinstance(error, WorkflowReconciliationPending) and (
        previous_failures + 1 >= maximum_attempts
    )


def _operation_context(command: CommandRecord) -> OperationContext:
    origin = command.request_context
    return OperationContext(
        request_id=origin.request_id,
        correlation_id=origin.correlation_id,
        trace_id=origin.trace_id,
        requested_at=origin.requested_at,
        actor=origin.actor,
        content_ref=origin.content_ref,
        schema_version=origin.schema_version,
        command_id=command.command_id,
        causation_id=None,
        deadline_at=None,
    )


async def _database_now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WorkflowInvariantError("PostgreSQL returned an invalid timestamp")
    return value.astimezone(UTC)


async def _wait_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


def _temporary_database_error(error: Exception) -> bool:
    if isinstance(error, (DisconnectionError, SqlTimeoutError)):
        return True
    if isinstance(error, DBAPIError) and error.connection_invalidated:
        return True
    for candidate in _database_error_candidates(error):
        sqlstate = _sqlstate(candidate)
        if sqlstate is not None and (
            sqlstate.startswith("08") or sqlstate in {"57P01", "57P02", "57P03"}
        ):
            return True
        if isinstance(candidate, (CannotConnectNowError, PostgresConnectionError)):
            return True
        if _connection_boundary_os_error(candidate):
            return True
    return False


def _database_error_candidates(error: Exception) -> tuple[BaseException, ...]:
    if isinstance(error, DBAPIError) and isinstance(error.orig, BaseException):
        return error, error.orig
    return (error,)


def _sqlstate(error: BaseException) -> str | None:
    for attribute in ("sqlstate", "pgcode"):
        value = getattr(error, attribute, None)
        if isinstance(value, str) and len(value) == 5:
            return value.upper()
    return None


_CONNECTION_ERRNOS = frozenset(
    {
        errno.ECONNABORTED,
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.EHOSTDOWN,
        errno.EHOSTUNREACH,
        errno.ENETDOWN,
        errno.ENETRESET,
        errno.ENETUNREACH,
        errno.EPIPE,
        errno.ETIMEDOUT,
    }
)
_CONNECTION_WINERRORS = frozenset({64, 10053, 10054, 10060, 10061, 10064, 10065})


def _connection_boundary_os_error(error: BaseException) -> bool:
    if isinstance(error, ConnectionError):
        return True
    return isinstance(error, OSError) and (
        error.errno in _CONNECTION_ERRNOS
        or getattr(error, "winerror", None) in _CONNECTION_WINERRORS
    )


__all__ = ["WorkflowHandler", "WorkflowWorker"]
