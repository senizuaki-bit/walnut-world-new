"""Process-loop gates for temporary PostgreSQL failures."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from asyncpg.exceptions import (
    CannotConnectNowError,
    ConnectionDoesNotExistError,
    InvalidCatalogNameError,
    InvalidPasswordError,
)
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from walnut_backend.workers import workflow_worker as workflow_worker_module
from walnut_backend.workers.workflow_worker import WorkflowWorker


class _Handler:
    operations = frozenset({"TEST_OPERATION"})

    def __init__(self, *, stop: asyncio.Event | None = None) -> None:
        self._stop = stop
        self.execute_calls = 0

    async def execute(self, claim: Any) -> None:
        del claim
        self.execute_calls += 1
        if self._stop is not None:
            self._stop.set()


def _worker(jobs: object, handler: _Handler, **kwargs: Any) -> WorkflowWorker:
    return WorkflowWorker(
        session_factory=cast(Any, object()),
        jobs=cast(Any, jobs),
        commands=cast(Any, object()),
        handlers=(handler,),
        worker_id="workflow-worker-01",
        **kwargs,
    )


def _claim() -> SimpleNamespace:
    return SimpleNamespace(operation="TEST_OPERATION")


def _database_unavailable() -> OperationalError:
    return OperationalError(
        statement="SELECT workflow job",
        params=None,
        orig=ConnectionError("database unavailable"),
    )


def _database_connection_lost() -> DBAPIError:
    return DBAPIError(
        statement="SELECT workflow job",
        params=None,
        orig=ConnectionDoesNotExistError("connection was closed in the middle of operation"),
        connection_invalidated=True,
    )


def _invalid_database_credentials() -> OperationalError:
    return OperationalError(
        statement="SELECT workflow job",
        params=None,
        orig=InvalidPasswordError("password authentication failed"),
        connection_invalidated=False,
    )


def _missing_database() -> InterfaceError:
    return InterfaceError(
        statement="SELECT workflow job",
        params=None,
        orig=InvalidCatalogNameError("database does not exist"),
        connection_invalidated=False,
    )


def test_database_outage_backs_off_then_processes_one_claim_without_failure_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()

    class _Jobs:
        def __init__(self) -> None:
            self.claim_calls = 0
            self.successful_claims = 0

        async def claim_next(self, **kwargs: Any) -> Any:
            del kwargs
            self.claim_calls += 1
            if self.claim_calls <= 2:
                raise _database_unavailable()
            self.successful_claims += 1
            return _claim()

    waits: list[float] = []

    async def capture_wait(event: asyncio.Event, seconds: float) -> None:
        assert event is stop
        waits.append(seconds)

    monkeypatch.setattr(workflow_worker_module, "_wait_or_stop", capture_wait)

    async def exercise() -> None:
        jobs = _Jobs()
        handler = _Handler(stop=stop)
        worker = _worker(
            jobs,
            handler,
            database_retry_base_seconds=0.01,
            database_retry_max_seconds=0.02,
        )
        failure_budget_calls = 0

        async def record_unexpected(*args: Any, **kwargs: Any) -> None:
            nonlocal failure_budget_calls
            del args, kwargs
            failure_budget_calls += 1

        worker._record_unexpected = (  # pyright: ignore[reportPrivateUsage]
            record_unexpected
        )

        await worker.run_forever("tenant_yaya", stop=stop, idle_poll_seconds=0.01)

        assert waits == [0.01, 0.02]
        assert jobs.claim_calls == 3
        assert jobs.successful_claims == 1
        assert handler.execute_calls == 1
        assert failure_budget_calls == 0

    asyncio.run(exercise())


def test_unknown_run_loop_failure_fails_loud_without_retry() -> None:
    class _Jobs:
        def __init__(self) -> None:
            self.claim_calls = 0

        async def claim_next(self, **kwargs: Any) -> Any:
            del kwargs
            self.claim_calls += 1
            raise RuntimeError("unknown business failure")

    async def exercise() -> None:
        jobs = _Jobs()
        worker = _worker(jobs, _Handler())

        with pytest.raises(RuntimeError, match="unknown business failure"):
            await worker.run_forever(
                "tenant_yaya",
                stop=asyncio.Event(),
                idle_poll_seconds=0.01,
            )

        assert jobs.claim_calls == 1

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "error",
    [_invalid_database_credentials(), _missing_database()],
    ids=["invalid-password-28P01", "missing-database-3D000"],
)
def test_permanent_postgres_operational_and_interface_errors_fail_loud(
    error: DBAPIError,
) -> None:
    class _Jobs:
        def __init__(self) -> None:
            self.claim_calls = 0

        async def claim_next(self, **kwargs: Any) -> Any:
            del kwargs
            self.claim_calls += 1
            raise error

    async def exercise() -> None:
        jobs = _Jobs()
        worker = _worker(jobs, _Handler())

        with pytest.raises(type(error)) as captured:
            await worker.run_forever(
                "tenant_yaya",
                stop=asyncio.Event(),
                idle_poll_seconds=0.01,
            )

        assert captured.value is error
        assert jobs.claim_calls == 1

    asyncio.run(exercise())


def test_stop_interrupts_long_database_backoff() -> None:
    attempted = asyncio.Event()
    stop = asyncio.Event()

    class _Jobs:
        def __init__(self) -> None:
            self.claim_calls = 0

        async def claim_next(self, **kwargs: Any) -> Any:
            del kwargs
            self.claim_calls += 1
            attempted.set()
            raise ConnectionError("database unavailable")

    async def exercise() -> None:
        jobs = _Jobs()
        worker = _worker(
            jobs,
            _Handler(),
            database_retry_base_seconds=60,
            database_retry_max_seconds=60,
        )
        task = asyncio.create_task(
            worker.run_forever(
                "tenant_yaya",
                stop=stop,
                idle_poll_seconds=0.01,
            )
        )

        await attempted.wait()
        stop.set()
        await asyncio.wait_for(task, timeout=0.2)

        assert jobs.claim_calls == 1

    asyncio.run(exercise())


def test_postgres_shutdown_sqlstate_is_temporary_and_does_not_exit_process() -> None:
    """Docker stop surfaces asyncpg 57P03 before SQLAlchemy can wrap it."""

    stop = asyncio.Event()

    class _Jobs:
        def __init__(self) -> None:
            self.claim_calls = 0

        async def claim_next(self, **kwargs: Any) -> Any:
            del kwargs
            self.claim_calls += 1
            if self.claim_calls == 1:
                raise CannotConnectNowError("the database system is shutting down")
            stop.set()
            return None

    async def exercise() -> None:
        jobs = _Jobs()
        worker = _worker(
            jobs,
            _Handler(),
            database_retry_base_seconds=0.01,
            database_retry_max_seconds=0.01,
        )
        await worker.run_forever("tenant_yaya", stop=stop, idle_poll_seconds=0.01)
        assert jobs.claim_calls == 2

    asyncio.run(exercise())


def test_dialect_wrapped_connection_loss_is_temporary() -> None:
    stop = asyncio.Event()

    class _Jobs:
        def __init__(self) -> None:
            self.claim_calls = 0

        async def claim_next(self, **kwargs: Any) -> Any:
            del kwargs
            self.claim_calls += 1
            if self.claim_calls == 1:
                raise _database_connection_lost()
            stop.set()
            return None

    async def exercise() -> None:
        jobs = _Jobs()
        worker = _worker(
            jobs,
            _Handler(),
            database_retry_base_seconds=0.01,
            database_retry_max_seconds=0.01,
        )
        await worker.run_forever("tenant_yaya", stop=stop, idle_poll_seconds=0.01)
        assert jobs.claim_calls == 2

    asyncio.run(exercise())
