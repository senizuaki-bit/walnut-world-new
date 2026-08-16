"""Dedicated learner projection worker with DB-outage and ACK-loss recovery."""

from __future__ import annotations

import asyncio
import errno
from typing import Any, Protocol

from asyncpg.exceptions import (
    CannotConnectNowError,
    PostgresConnectionError,
)
from sqlalchemy.exc import (
    DBAPIError,
    DisconnectionError,
)
from sqlalchemy.exc import (
    TimeoutError as SqlTimeoutError,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from walnut_backend.adapters.postgres.command_store import PostgresCommandStore
from walnut_backend.adapters.postgres.learner_projection_jobs import (
    ClaimedLearnerProjectionJob,
    LearnerProjectionFenceLost,
    LearnerProjectionInvariantError,
    LearnerProjectionRetryableError,
    PostgresLearnerProjectionJobStore,
)


class LearnerProjector(Protocol):
    async def project(self, claim: ClaimedLearnerProjectionJob) -> None: ...

    async def validate_terminal(self, claim: ClaimedLearnerProjectionJob) -> None: ...


class LearnerProjectionWorker:
    """Claim one learner objective; never replay a reconciled terminal commit."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        jobs: PostgresLearnerProjectionJobStore,
        commands: PostgresCommandStore,
        projector: LearnerProjector,
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
        if not 30 <= lease_seconds <= 3600:
            raise ValueError("learner lease_seconds must be between 30 and 3600")
        if not 1 <= maximum_attempts <= 100:
            raise ValueError("maximum_attempts must be between 1 and 100")
        if not 1 <= retry_base_seconds <= retry_max_seconds <= 3600:
            raise ValueError("retry delay bounds are invalid")
        if not 0.01 <= database_retry_base_seconds <= database_retry_max_seconds <= 60:
            raise ValueError("database retry delay bounds are invalid")
        self._sessions = session_factory
        self._jobs = jobs
        self._commands = commands
        self._projector = projector
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
        try:
            await self._projector.project(claim)
        except LearnerProjectionFenceLost:
            return True
        except Exception as error:
            # This linearizable read is the acknowledgement-loss boundary. A
            # terminal row means the whole learner/Product/Command transaction
            # committed, so replaying it would be incorrect.
            reconciled = False
            if not isinstance(error, LearnerProjectionInvariantError):
                try:
                    reconciled = await self._jobs.reconcile_succeeded(
                        tenant_id=claim.tenant_id,
                        job_id=claim.job_id,
                        request_sha256=claim.request_sha256,
                    )
                except Exception as reconcile_error:
                    if not _temporary_database_error(reconcile_error):
                        raise
                    # The commit outcome is still unknown. Do not rewrite the
                    # claim as RETRY_WAIT; let its lease expire, then reconcile
                    # from a new fenced claim when PostgreSQL returns.
                    raise reconcile_error from error
            if reconciled:
                # Validation failure here is terminal corruption, not a
                # retryable worker error. Let it escape and fail loud.
                await self._projector.validate_terminal(claim)
                return True
            try:
                await self._record_failure(claim, error)
            except LearnerProjectionFenceLost:
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
            if not processed:
                await _wait_or_stop(stop, idle_poll_seconds)

    async def _record_failure(
        self,
        claim: ClaimedLearnerProjectionJob,
        error: Exception,
    ) -> None:
        invariant = isinstance(error, LearnerProjectionInvariantError)
        dead_letter = invariant or claim.attempt >= self._maximum_attempts
        sanitized: dict[str, Any] = {
            "code": (
                "LEARNER_PROJECTION_INVARIANT"
                if invariant
                else "LEARNER_PROJECTION_EXECUTION_FAILED"
            ),
            "exception_type": type(error).__name__,
            "attempt": claim.attempt,
        }
        async with self._sessions() as session, session.begin():
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
                    error=sanitized,
                )
                return
            await self._jobs.dead_letter_in_session(session, claim, error=sanitized)


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


def _retry_delay_seconds(
    error: Exception,
    *,
    attempt: int,
    retry_base_seconds: int,
    retry_max_seconds: int,
) -> int:
    if isinstance(error, LearnerProjectionRetryableError):
        return retry_base_seconds
    return min(retry_max_seconds, retry_base_seconds * (2 ** max(0, attempt - 1)))


__all__ = ["LearnerProjectionWorker", "LearnerProjector"]
