"""PostgreSQL learner projector; implemented at the Turn integration boundary."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from walnut_backend.adapters.postgres.command_store import PostgresCommandStore
from walnut_backend.adapters.postgres.learner_projection_jobs import (
    ClaimedLearnerProjectionJob,
    PostgresLearnerProjectionJobStore,
)


class PostgresLearnerProjector:
    """Own learner/Product projection after the Turn worker's durable hand-off."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        jobs: PostgresLearnerProjectionJobStore,
        commands: PostgresCommandStore,
        lease_seconds: int,
    ) -> None:
        self._sessions = session_factory
        self._jobs = jobs
        self._commands = commands
        self._lease_seconds = lease_seconds

    async def project(self, claim: ClaimedLearnerProjectionJob) -> None:
        from walnut_backend.workers.turn_projection import project_learner_handoff

        await project_learner_handoff(
            session_factory=self._sessions,
            learner_jobs=self._jobs,
            commands=self._commands,
            claim=claim,
            lease_seconds=self._lease_seconds,
        )

    async def validate_terminal(self, claim: ClaimedLearnerProjectionJob) -> None:
        from walnut_backend.workers.turn_projection import validate_learner_handoff_terminal

        await validate_learner_handoff_terminal(
            session_factory=self._sessions,
            claim=claim,
        )


__all__ = ["PostgresLearnerProjector"]
