"""Process-boundary gates for the independently fenced learner worker."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from asyncpg.exceptions import (
    CannotConnectNowError,
    ConnectionDoesNotExistError,
    InvalidCatalogNameError,
    InvalidPasswordError,
)
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from walnut_backend.adapters.postgres.learner_projection_jobs import (
    ClaimedLearnerProjectionJob,
    LearnerProjectionFenceLost,
    LearnerProjectionInvariantError,
)
from walnut_backend.workers.learner_worker import LearnerProjectionWorker


def _claim(*, attempt: int = 1) -> ClaimedLearnerProjectionJob:
    now = datetime.now(UTC)
    return ClaimedLearnerProjectionJob(
        job_id="job_learner_durable_01",
        tenant_id="tenant_yaya",
        command_id="cmd_learner_durable_01",
        session_id="session_learner_durable_01",
        turn_id="turn_learner_durable_01",
        run_id="run_learner_durable_01",
        learner_id="learner_durable_01",
        actor_id="student_durable_01",
        content_hash="a" * 64,
        source_event_id="evt_learner_durable_01",
        expected_revision=0,
        through_sequence=1,
        status="CLAIMED",
        attempt=attempt,
        fencing_token=1,
        lease_owner="learner-worker-01",
        lease_expires_at=now + timedelta(minutes=2),
        request_sha256="b" * 64,
        projection={"schema_version": "1.0.0"},
        created_at=now,
    )


class _Context:
    async def __aenter__(self) -> _Context:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    def begin(self) -> _Context:
        return self


class _Sessions:
    def __call__(self) -> _Context:
        return _Context()


class _Jobs:
    def __init__(self, claim: ClaimedLearnerProjectionJob | None) -> None:
        self.claim = claim
        self.reconciled = False
        self.retry_calls = 0
        self.claim_calls = 0

    async def claim_next(self, **kwargs: Any) -> ClaimedLearnerProjectionJob | None:
        del kwargs
        self.claim_calls += 1
        value, self.claim = self.claim, None
        return value

    async def reconcile_succeeded(self, **kwargs: Any) -> bool:
        del kwargs
        return self.reconciled

    async def retry_in_session(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.retry_calls += 1


class _Projector:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.project_calls = 0
        self.validate_calls = 0
        self.validation_error: Exception | None = None

    async def project(self, claim: ClaimedLearnerProjectionJob) -> None:
        del claim
        self.project_calls += 1
        if self.error is not None:
            raise self.error

    async def validate_terminal(self, claim: ClaimedLearnerProjectionJob) -> None:
        del claim
        self.validate_calls += 1
        if self.validation_error is not None:
            raise self.validation_error


def _worker(jobs: _Jobs, projector: _Projector) -> LearnerProjectionWorker:
    return LearnerProjectionWorker(
        session_factory=_Sessions(),  # type: ignore[arg-type]
        jobs=jobs,  # type: ignore[arg-type]
        commands=object(),  # type: ignore[arg-type]
        projector=projector,
        worker_id="learner-worker-01",
        retry_base_seconds=1,
        retry_max_seconds=2,
    )


def test_commit_ack_loss_validates_terminal_without_replay() -> None:
    async def exercise() -> None:
        jobs = _Jobs(_claim())
        jobs.reconciled = True
        projector = _Projector(ConnectionError("commit acknowledgement lost"))
        processed = await _worker(jobs, projector).run_once("tenant_yaya")
        assert processed is True
        assert projector.project_calls == 1
        assert projector.validate_calls == 1
        assert jobs.retry_calls == 0

    asyncio.run(exercise())


def test_ack_reconciliation_corruption_fails_loud() -> None:
    async def exercise() -> None:
        jobs = _Jobs(_claim())
        jobs.reconciled = True
        projector = _Projector(ConnectionError("commit acknowledgement lost"))
        projector.validation_error = LearnerProjectionInvariantError("tampered closure")
        with pytest.raises(LearnerProjectionInvariantError, match="tampered closure"):
            await _worker(jobs, projector).run_once("tenant_yaya")
        assert projector.project_calls == projector.validate_calls == 1
        assert jobs.retry_calls == 0

    asyncio.run(exercise())


def test_stale_fence_never_retries_or_reconciles_old_owner() -> None:
    async def exercise() -> None:
        jobs = _Jobs(_claim())
        projector = _Projector(LearnerProjectionFenceLost("taken over"))
        assert await _worker(jobs, projector).run_once("tenant_yaya") is True
        assert projector.project_calls == 1
        assert projector.validate_calls == 0
        assert jobs.retry_calls == 0

    asyncio.run(exercise())


def test_retryable_projection_failure_releases_claim_with_backoff() -> None:
    async def exercise() -> None:
        jobs = _Jobs(_claim(attempt=1))
        projector = _Projector(RuntimeError("temporary projection failure"))
        assert await _worker(jobs, projector).run_once("tenant_yaya") is True
        assert projector.project_calls == 1
        assert projector.validate_calls == 0
        assert jobs.retry_calls == 1

    asyncio.run(exercise())


def test_database_outage_loop_recovers_without_process_exit() -> None:
    class _OutageJobs(_Jobs):
        async def claim_next(self, **kwargs: Any) -> ClaimedLearnerProjectionJob | None:
            del kwargs
            self.claim_calls += 1
            if self.claim_calls == 1:
                raise ConnectionError("database unavailable")
            stop.set()
            return None

    async def exercise() -> None:
        jobs = _OutageJobs(None)
        worker = LearnerProjectionWorker(
            session_factory=_Sessions(),  # type: ignore[arg-type]
            jobs=jobs,  # type: ignore[arg-type]
            commands=object(),  # type: ignore[arg-type]
            projector=_Projector(),
            worker_id="learner-worker-01",
            database_retry_base_seconds=0.01,
            database_retry_max_seconds=0.01,
        )
        await worker.run_forever(
            "tenant_yaya",
            stop=stop,
            idle_poll_seconds=0.01,
        )
        assert jobs.claim_calls == 2

    stop = asyncio.Event()
    asyncio.run(exercise())


def test_postgres_shutdown_sqlstate_recovers_without_process_exit() -> None:
    class _OutageJobs(_Jobs):
        async def claim_next(self, **kwargs: Any) -> ClaimedLearnerProjectionJob | None:
            del kwargs
            self.claim_calls += 1
            if self.claim_calls == 1:
                raise CannotConnectNowError("the database system is shutting down")
            stop.set()
            return None

    async def exercise() -> None:
        jobs = _OutageJobs(None)
        worker = LearnerProjectionWorker(
            session_factory=_Sessions(),  # type: ignore[arg-type]
            jobs=jobs,  # type: ignore[arg-type]
            commands=object(),  # type: ignore[arg-type]
            projector=_Projector(),
            worker_id="learner-worker-01",
            database_retry_base_seconds=0.01,
            database_retry_max_seconds=0.01,
        )
        await worker.run_forever(
            "tenant_yaya",
            stop=stop,
            idle_poll_seconds=0.01,
        )
        assert jobs.claim_calls == 2

    stop = asyncio.Event()
    asyncio.run(exercise())


def test_dialect_wrapped_connection_loss_recovers_without_process_exit() -> None:
    class _OutageJobs(_Jobs):
        async def claim_next(self, **kwargs: Any) -> ClaimedLearnerProjectionJob | None:
            del kwargs
            self.claim_calls += 1
            if self.claim_calls == 1:
                raise DBAPIError(
                    statement="SELECT learner job",
                    params=None,
                    orig=ConnectionDoesNotExistError(
                        "connection was closed in the middle of operation"
                    ),
                    connection_invalidated=True,
                )
            stop.set()
            return None

    async def exercise() -> None:
        jobs = _OutageJobs(None)
        worker = LearnerProjectionWorker(
            session_factory=_Sessions(),  # type: ignore[arg-type]
            jobs=jobs,  # type: ignore[arg-type]
            commands=object(),  # type: ignore[arg-type]
            projector=_Projector(),
            worker_id="learner-worker-01",
            database_retry_base_seconds=0.01,
            database_retry_max_seconds=0.01,
        )
        await worker.run_forever(
            "tenant_yaya",
            stop=stop,
            idle_poll_seconds=0.01,
        )
        assert jobs.claim_calls == 2

    stop = asyncio.Event()
    asyncio.run(exercise())


@pytest.mark.parametrize(
    "error",
    [
        OperationalError(
            statement="SELECT learner job",
            params=None,
            orig=InvalidPasswordError("password authentication failed"),
            connection_invalidated=False,
        ),
        InterfaceError(
            statement="SELECT learner job",
            params=None,
            orig=InvalidCatalogNameError("database does not exist"),
            connection_invalidated=False,
        ),
    ],
    ids=["invalid-password-28P01", "missing-database-3D000"],
)
def test_permanent_postgres_operational_and_interface_errors_fail_loud(
    error: DBAPIError,
) -> None:
    class _PermanentFailureJobs(_Jobs):
        async def claim_next(self, **kwargs: Any) -> ClaimedLearnerProjectionJob | None:
            del kwargs
            self.claim_calls += 1
            raise error

    async def exercise() -> None:
        jobs = _PermanentFailureJobs(None)
        worker = LearnerProjectionWorker(
            session_factory=_Sessions(),  # type: ignore[arg-type]
            jobs=jobs,  # type: ignore[arg-type]
            commands=object(),  # type: ignore[arg-type]
            projector=_Projector(),
            worker_id="learner-worker-01",
            database_retry_base_seconds=0.01,
            database_retry_max_seconds=0.01,
        )

        with pytest.raises(type(error)) as captured:
            await worker.run_forever(
                "tenant_yaya",
                stop=asyncio.Event(),
                idle_poll_seconds=0.01,
            )

        assert captured.value is error
        assert jobs.claim_calls == 1

    asyncio.run(exercise())
