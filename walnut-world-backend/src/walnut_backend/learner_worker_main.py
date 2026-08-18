"""Production entrypoint for the dedicated Backend learner projection worker."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import signal
from dataclasses import dataclass

from walnut_backend.adapters.postgres.command_store import PostgresCommandStore
from walnut_backend.adapters.postgres.learner_projection_jobs import (
    PostgresLearnerProjectionJobStore,
)
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.workers.learner_projector import PostgresLearnerProjector
from walnut_backend.workers.learner_worker import LearnerProjectionWorker


@dataclass(frozen=True, slots=True)
class LearnerWorkerSettings:
    database_url: str
    tenant_id: str
    worker_id: str = "walnut-learner-worker-1"
    lease_seconds: int = 300
    idle_poll_seconds: float = 0.25
    maximum_attempts: int = 5

    def __post_init__(self) -> None:
        for name in ("database_url", "tenant_id", "worker_id"):
            if not getattr(self, name):
                raise ValueError(f"{name} must be explicitly configured")
        if len(self.worker_id) > 128:
            raise ValueError("worker_id must be at most 128 characters")
        if not 30 <= self.lease_seconds <= 3600:
            raise ValueError("learner lease_seconds must be between 30 and 3600")
        if not 0.01 <= self.idle_poll_seconds <= 60:
            raise ValueError("learner idle_poll_seconds must be between 0.01 and 60")
        if not 1 <= self.maximum_attempts <= 100:
            raise ValueError("learner maximum_attempts must be between 1 and 100")

    @classmethod
    def from_env(cls) -> LearnerWorkerSettings:
        return cls(
            database_url=_required("WALNUT_DATABASE_URL"),
            tenant_id=_required("WALNUT_TENANT_ID"),
            worker_id=os.getenv("WALNUT_LEARNER_WORKER_ID", "walnut-learner-worker-1"),
            lease_seconds=_integer("WALNUT_LEARNER_WORKER_LEASE_SECONDS", 300),
            idle_poll_seconds=float(os.getenv("WALNUT_LEARNER_WORKER_IDLE_POLL_SECONDS", "0.25")),
            maximum_attempts=_integer("WALNUT_LEARNER_WORKER_MAXIMUM_ATTEMPTS", 5),
        )


async def run_learner_worker(settings: LearnerWorkerSettings) -> None:
    sessions = create_session_factory(settings.database_url)
    commands = PostgresCommandStore(sessions)
    jobs = PostgresLearnerProjectionJobStore(sessions)
    projector = PostgresLearnerProjector(
        session_factory=sessions,
        jobs=jobs,
        commands=commands,
        lease_seconds=settings.lease_seconds,
    )
    worker = LearnerProjectionWorker(
        session_factory=sessions,
        jobs=jobs,
        commands=commands,
        projector=projector,
        worker_id=settings.worker_id,
        lease_seconds=settings.lease_seconds,
        maximum_attempts=settings.maximum_attempts,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await worker.run_forever(
            settings.tenant_id,
            stop=stop,
            idle_poll_seconds=settings.idle_poll_seconds,
        )
    finally:
        await sessions.kw["bind"].dispose()


def main() -> None:
    # Windows multiprocessing (spawn) re-imports this module in every child and
    # runs __main__ again.  Only the launcher-spawned parent may own the job
    # polling loop; children exist only to run a dependency sub-task.
    if multiprocessing.parent_process() is not None:
        return
    asyncio.run(run_learner_worker(LearnerWorkerSettings.from_env()))


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _integer(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value is None else int(value)


if __name__ == "__main__":
    main()


__all__ = ["LearnerWorkerSettings", "run_learner_worker"]
