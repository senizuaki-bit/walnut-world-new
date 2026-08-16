"""Independent durable learner projection queue and Turn hand-off fencing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from .models import CommandRow, JobStepReceiptRow, LearnerProjectionJobRow, WorkflowJobRow
from .workflow_jobs import (
    ClaimedWorkflowJob,
    workflow_json_sha256,
    workflow_receipt_sha256,
    workflow_step_receipt_id,
)

LearnerProjectionStatus = Literal[
    "READY",
    "CLAIMED",
    "RUNNING",
    "RETRY_WAIT",
    "SUCCEEDED",
    "DEAD_LETTER",
]

_CLAIMABLE = ("READY", "RETRY_WAIT")
_OWNED = ("CLAIMED", "RUNNING")
_RECOVERABLE = ("CLAIMED", "RUNNING")


class LearnerProjectionInvariantError(RuntimeError):
    """Durable learner objective or closure bytes are inconsistent."""


class LearnerProjectionFenceLost(RuntimeError):
    """A newer learner claim owns the projection."""


class LearnerProjectionRetryableError(RuntimeError):
    """A valid learner projection cannot be completed yet."""


@dataclass(frozen=True, slots=True)
class ClaimedLearnerProjectionJob:
    job_id: str
    tenant_id: str
    command_id: str
    session_id: str
    turn_id: str
    run_id: str
    learner_id: str
    actor_id: str
    content_hash: str
    source_event_id: str
    expected_revision: int
    through_sequence: int
    status: LearnerProjectionStatus
    attempt: int
    fencing_token: int
    lease_owner: str
    lease_expires_at: datetime
    request_sha256: str
    projection: Mapping[str, Any]
    created_at: datetime


class PostgresLearnerProjectionJobStore:
    """Backend-owned queue whose fence is independent from the Turn worker."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def enqueue_and_handoff_in_session(
        self,
        session: AsyncSession,
        turn_claim: ClaimedWorkflowJob,
        *,
        command_id: str,
        session_id: str,
        turn_id: str,
        run_id: str,
        learner_id: str,
        actor_id: str,
        content_hash: str,
        source_event_id: str,
        expected_revision: int,
        through_sequence: int,
        projection: Mapping[str, Any],
        recorded_at: datetime,
    ) -> LearnerProjectionJobRow:
        """Persist one immutable objective and release the original Turn lease."""

        if turn_claim.command_id != command_id:
            raise LearnerProjectionInvariantError("learner hand-off Command identity drifted")
        if turn_claim.operation != "EXECUTE_AGENT_TURN" or turn_claim.subject_type != "AGENT_TURN":
            raise LearnerProjectionInvariantError("learner hand-off is not an Agent Turn")
        if turn_claim.subject_id != turn_id:
            raise LearnerProjectionInvariantError("learner hand-off Turn identity drifted")
        for name, value, maximum in (
            ("command_id", command_id, 128),
            ("session_id", session_id, 128),
            ("turn_id", turn_id, 128),
            ("run_id", run_id, 128),
            ("learner_id", learner_id, 128),
            ("actor_id", actor_id, 128),
            ("source_event_id", source_event_id, 132),
        ):
            _bounded(value, maximum, name)
        _sha256(content_hash, "content_hash")
        if expected_revision < 0 or through_sequence < 1:
            raise ValueError("learner revision and sequence must be non-negative/positive")
        if recorded_at.tzinfo is None:
            raise LearnerProjectionInvariantError(
                "learner hand-off causal timestamp must be timezone-aware"
            )

        objective = dict(projection)
        request_sha256 = workflow_json_sha256(objective)
        database_now = await _database_now(session)
        original = await session.scalar(
            select(WorkflowJobRow)
            .where(
                WorkflowJobRow.tenant_id == turn_claim.tenant_id,
                WorkflowJobRow.job_id == turn_claim.job_id,
            )
            .with_for_update()
        )
        if not _turn_claim_is_current(original, turn_claim, database_now):
            raise LearnerProjectionFenceLost("Turn fence was lost before learner hand-off")
        assert original is not None
        command = await session.scalar(
            select(CommandRow)
            .where(
                CommandRow.tenant_id == turn_claim.tenant_id,
                CommandRow.command_id == command_id,
            )
            .with_for_update()
        )
        if (
            command is None
            or command.actor_id != actor_id
            or command.command_type != turn_claim.operation
            or recorded_at < original.created_at
            or recorded_at < command.accepted_at
            or recorded_at < command.updated_at
        ):
            raise LearnerProjectionInvariantError(
                "learner hand-off timestamp precedes its Command or parent Job authority"
            )
        # clock_timestamp() can advance after the caller froze its immutable
        # objective. Preserve those bytes while moving every mutable queue
        # timestamp to one causal point at or after both clocks.
        recorded_at = max(recorded_at, database_now)

        await session.flush()
        inserted = await session.scalar(
            insert(LearnerProjectionJobRow)
            .values(
                job_id=turn_claim.job_id,
                tenant_id=turn_claim.tenant_id,
                command_id=command_id,
                session_id=session_id,
                turn_id=turn_id,
                run_id=run_id,
                learner_id=learner_id,
                actor_id=actor_id,
                content_hash=content_hash,
                source_event_id=source_event_id,
                expected_revision=expected_revision,
                through_sequence=through_sequence,
                projection_json=objective,
                status="READY",
                attempt=0,
                fencing_token=0,
                lease_owner=None,
                lease_expires_at=None,
                next_attempt_at=recorded_at,
                request_sha256=request_sha256,
                result_sha256=None,
                result_json=None,
                last_error_json=None,
                completed_at=None,
                created_at=recorded_at,
                updated_at=recorded_at,
            )
            .on_conflict_do_nothing()
            .returning(LearnerProjectionJobRow.job_id)
        )
        rows = (
            await session.scalars(
                select(LearnerProjectionJobRow).where(
                    LearnerProjectionJobRow.tenant_id == turn_claim.tenant_id,
                    or_(
                        LearnerProjectionJobRow.job_id == turn_claim.job_id,
                        LearnerProjectionJobRow.command_id == command_id,
                        and_(
                            LearnerProjectionJobRow.session_id == session_id,
                            LearnerProjectionJobRow.turn_id == turn_id,
                        ),
                        LearnerProjectionJobRow.run_id == run_id,
                        and_(
                            LearnerProjectionJobRow.learner_id == learner_id,
                            LearnerProjectionJobRow.source_event_id == source_event_id,
                        ),
                    ),
                )
            )
        ).all()
        if len(rows) != 1 or (inserted is not None and inserted != rows[0].job_id):
            raise LearnerProjectionInvariantError("learner hand-off identity is not unique")
        row = rows[0]
        if not _immutable_matches(
            row,
            turn_claim=turn_claim,
            command_id=command_id,
            session_id=session_id,
            turn_id=turn_id,
            run_id=run_id,
            learner_id=learner_id,
            actor_id=actor_id,
            content_hash=content_hash,
            source_event_id=source_event_id,
            expected_revision=expected_revision,
            through_sequence=through_sequence,
            request_sha256=request_sha256,
            projection=objective,
        ):
            raise LearnerProjectionInvariantError(
                "learner hand-off identity was reused with different bytes"
            )
        if row.status != "READY" or row.attempt != 0 or row.fencing_token != 0:
            raise LearnerProjectionInvariantError("learner objective existed before Turn hand-off")

        result = await session.execute(
            update(WorkflowJobRow)
            .where(
                WorkflowJobRow.tenant_id == turn_claim.tenant_id,
                WorkflowJobRow.job_id == turn_claim.job_id,
                WorkflowJobRow.lease_owner == turn_claim.lease_owner,
                WorkflowJobRow.fencing_token == turn_claim.fencing_token,
                WorkflowJobRow.status.in_(_OWNED),
                WorkflowJobRow.lease_expires_at > database_now,
            )
            .values(
                status="WAITING_PROJECTION",
                phase="LEARNER_QUEUED",
                lease_owner=None,
                lease_expires_at=None,
                next_attempt_at=None,
                last_error_json=None,
                updated_at=recorded_at,
            )
        )
        if getattr(result, "rowcount", 0) != 1:
            raise LearnerProjectionFenceLost("Turn fence was lost during learner hand-off")
        return row

    async def claim_next(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> ClaimedLearnerProjectionJob | None:
        """Claim one due objective, including an expired process claim."""

        _bounded(worker_id, 128, "worker_id")
        if isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        async with self._sessions() as session, session.begin():
            now = await _database_now(session)
            due = and_(
                LearnerProjectionJobRow.status.in_(_CLAIMABLE),
                LearnerProjectionJobRow.next_attempt_at <= now,
            )
            expired = and_(
                LearnerProjectionJobRow.status.in_(_RECOVERABLE),
                LearnerProjectionJobRow.lease_expires_at <= now,
            )
            prior = aliased(LearnerProjectionJobRow)
            # A failed prior revision is an explicit operational gap. Do not
            # silently project later learner revisions across it.
            no_prior = ~exists(
                select(prior.job_id).where(
                    prior.tenant_id == LearnerProjectionJobRow.tenant_id,
                    prior.learner_id == LearnerProjectionJobRow.learner_id,
                    prior.actor_id == LearnerProjectionJobRow.actor_id,
                    prior.content_hash == LearnerProjectionJobRow.content_hash,
                    prior.status != "SUCCEEDED",
                    prior.expected_revision < LearnerProjectionJobRow.expected_revision,
                )
            )
            parent = aliased(WorkflowJobRow)
            row = await session.scalar(
                select(LearnerProjectionJobRow)
                .join(
                    parent,
                    and_(
                        parent.tenant_id == LearnerProjectionJobRow.tenant_id,
                        parent.job_id == LearnerProjectionJobRow.job_id,
                    ),
                )
                .where(
                    LearnerProjectionJobRow.tenant_id == tenant_id,
                    or_(due, expired),
                    no_prior,
                    parent.command_id == LearnerProjectionJobRow.command_id,
                    parent.operation == "EXECUTE_AGENT_TURN",
                    parent.subject_type == "AGENT_TURN",
                    parent.subject_id == LearnerProjectionJobRow.turn_id,
                    parent.status == "WAITING_PROJECTION",
                    parent.phase == "LEARNER_QUEUED",
                    parent.lease_owner.is_(None),
                    parent.lease_expires_at.is_(None),
                )
                .order_by(
                    LearnerProjectionJobRow.expected_revision,
                    LearnerProjectionJobRow.created_at,
                    LearnerProjectionJobRow.job_id,
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            row.status = "CLAIMED"
            row.attempt += 1
            row.fencing_token += 1
            row.lease_owner = worker_id
            row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            row.next_attempt_at = None
            row.last_error_json = None
            row.updated_at = now
            await session.flush()
            return _claimed(row)

    async def start_in_session(
        self,
        session: AsyncSession,
        claim: ClaimedLearnerProjectionJob,
        *,
        lease_seconds: int,
    ) -> ClaimedLearnerProjectionJob:
        if isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        now = await _database_now(session)
        result = await session.execute(
            update(LearnerProjectionJobRow)
            .where(*_claim_predicate(claim), LearnerProjectionJobRow.lease_expires_at > now)
            .values(
                status="RUNNING",
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                updated_at=now,
            )
        )
        if getattr(result, "rowcount", 0) != 1:
            raise LearnerProjectionFenceLost("learner fence was lost before projection")
        row = await session.scalar(
            select(LearnerProjectionJobRow).where(
                LearnerProjectionJobRow.tenant_id == claim.tenant_id,
                LearnerProjectionJobRow.job_id == claim.job_id,
            )
        )
        if row is None:
            raise LearnerProjectionInvariantError("claimed learner objective disappeared")
        _verify_objective_hash(row)
        return _claimed(row)

    async def retry_in_session(
        self,
        session: AsyncSession,
        claim: ClaimedLearnerProjectionJob,
        *,
        delay_seconds: int,
        error: Mapping[str, Any],
    ) -> None:
        if isinstance(delay_seconds, bool) or not 0 <= delay_seconds <= 86_400:
            raise ValueError("delay_seconds must be between 0 and 86400")
        now = await _database_now(session)
        result = await session.execute(
            update(LearnerProjectionJobRow)
            .where(*_claim_predicate(claim), LearnerProjectionJobRow.lease_expires_at > now)
            .values(
                status="RETRY_WAIT",
                lease_owner=None,
                lease_expires_at=None,
                next_attempt_at=now + timedelta(seconds=delay_seconds),
                last_error_json=dict(error),
                updated_at=now,
            )
        )
        if getattr(result, "rowcount", 0) != 1:
            raise LearnerProjectionFenceLost("learner fence was lost before retry scheduling")

    async def complete_in_session(
        self,
        session: AsyncSession,
        claim: ClaimedLearnerProjectionJob,
        *,
        result: Mapping[str, Any],
    ) -> None:
        """Atomically terminalize learner and original Turn workflow authorities."""

        output = dict(result)
        result_sha256 = workflow_json_sha256(output)
        now = await _database_now(session)
        learner = await _owned_row(session, claim, now, verify_objective=True)
        original = await _waiting_turn(session, claim)
        learner.status = "SUCCEEDED"
        learner.lease_owner = None
        learner.lease_expires_at = None
        learner.next_attempt_at = None
        learner.result_sha256 = result_sha256
        learner.result_json = output
        learner.last_error_json = None
        learner.completed_at = now
        learner.updated_at = now
        original.status = "SUCCEEDED"
        original.phase = "COMPLETE"
        original.next_attempt_at = None
        original.last_error_json = None
        original.updated_at = now
        await session.flush()

    async def record_turn_completed_in_session(
        self,
        session: AsyncSession,
        claim: ClaimedLearnerProjectionJob,
        *,
        input_sha256: str,
        output: Mapping[str, Any],
    ) -> JobStepReceiptRow:
        """Write the parent Turn receipt under the independent learner fence."""

        _sha256(input_sha256, "input_sha256")
        value = dict(output)
        output_sha256 = workflow_receipt_sha256(value)
        now = await _database_now(session)
        await _owned_row(session, claim, now, verify_objective=True)
        original = await _waiting_turn(session, claim)
        receipt_id = workflow_step_receipt_id(claim.tenant_id, claim.job_id, "TURN_COMPLETED")
        inserted = await session.scalar(
            insert(JobStepReceiptRow)
            .values(
                receipt_id=receipt_id,
                tenant_id=claim.tenant_id,
                job_id=claim.job_id,
                step_name="TURN_COMPLETED",
                # Product readers close this receipt over the parent Turn's
                # monotonic fence. The independent learner fence is retained
                # on learner_projection_jobs and checked above.
                fencing_token=original.fencing_token,
                input_sha256=input_sha256,
                output_sha256=output_sha256,
                receipt_json=value,
                completed_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_job_step_once")
            .returning(JobStepReceiptRow.receipt_id)
        )
        row = await session.scalar(
            select(JobStepReceiptRow).where(
                JobStepReceiptRow.tenant_id == claim.tenant_id,
                JobStepReceiptRow.job_id == claim.job_id,
                JobStepReceiptRow.step_name == "TURN_COMPLETED",
            )
        )
        if row is None or (inserted is not None and inserted != row.receipt_id):
            raise LearnerProjectionInvariantError("Turn terminal receipt did not materialize")
        if (
            row.receipt_id != receipt_id
            or row.fencing_token != original.fencing_token
            or row.input_sha256 != input_sha256
            or row.output_sha256 != output_sha256
            or row.receipt_json != value
        ):
            raise LearnerProjectionInvariantError(
                "Turn terminal receipt conflicts with learner projection bytes"
            )
        return row

    async def record_projection_committed_in_session(
        self,
        session: AsyncSession,
        claim: ClaimedLearnerProjectionJob,
        *,
        input_sha256: str,
        output: Mapping[str, Any],
    ) -> JobStepReceiptRow:
        """Freeze the private full projection bytes under the learner fence."""

        _sha256(input_sha256, "input_sha256")
        value = dict(output)
        output_sha256 = workflow_receipt_sha256(value)
        now = await _database_now(session)
        await _owned_row(session, claim, now, verify_objective=True)
        original = await _waiting_turn(session, claim)
        step_name = "LEARNER_PROJECTION_COMMITTED"
        receipt_id = workflow_step_receipt_id(
            claim.tenant_id,
            claim.job_id,
            step_name,
        )
        inserted = await session.scalar(
            insert(JobStepReceiptRow)
            .values(
                receipt_id=receipt_id,
                tenant_id=claim.tenant_id,
                job_id=claim.job_id,
                step_name=step_name,
                fencing_token=original.fencing_token,
                input_sha256=input_sha256,
                output_sha256=output_sha256,
                receipt_json=value,
                completed_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_job_step_once")
            .returning(JobStepReceiptRow.receipt_id)
        )
        row = await session.scalar(
            select(JobStepReceiptRow).where(
                JobStepReceiptRow.tenant_id == claim.tenant_id,
                JobStepReceiptRow.job_id == claim.job_id,
                JobStepReceiptRow.step_name == step_name,
            )
        )
        if row is None or (inserted is not None and inserted != row.receipt_id):
            raise LearnerProjectionInvariantError("learner projection receipt did not materialize")
        if (
            row.receipt_id != receipt_id
            or row.fencing_token != original.fencing_token
            or row.input_sha256 != input_sha256
            or row.output_sha256 != output_sha256
            or row.receipt_json != value
        ):
            raise LearnerProjectionInvariantError(
                "learner projection receipt conflicts with committed bytes"
            )
        return row

    async def dead_letter_in_session(
        self,
        session: AsyncSession,
        claim: ClaimedLearnerProjectionJob,
        *,
        error: Mapping[str, Any],
    ) -> None:
        now = await _database_now(session)
        learner = await _owned_row(session, claim, now, verify_objective=False)
        original = await _waiting_turn(session, claim)
        learner.status = "DEAD_LETTER"
        learner.lease_owner = None
        learner.lease_expires_at = None
        learner.next_attempt_at = None
        learner.last_error_json = dict(error)
        learner.completed_at = now
        learner.updated_at = now
        original.status = "DEAD_LETTER"
        original.phase = "LEARNER_PROJECT"
        original.next_attempt_at = None
        original.last_error_json = dict(error)
        original.updated_at = now
        await session.flush()

    async def reconcile_succeeded(
        self,
        *,
        tenant_id: str,
        job_id: str,
        request_sha256: str,
    ) -> bool:
        """Resolve a lost commit acknowledgement without replaying projection."""

        async with self._sessions() as session:
            row = await session.scalar(
                select(LearnerProjectionJobRow).where(
                    LearnerProjectionJobRow.tenant_id == tenant_id,
                    LearnerProjectionJobRow.job_id == job_id,
                )
            )
            if row is None:
                return False
            _verify_objective_hash(row)
            if row.request_sha256 != request_sha256:
                raise LearnerProjectionInvariantError(
                    "learner acknowledgement lookup used different objective bytes"
                )
            if row.status != "SUCCEEDED":
                return False
            if row.result_json is None or row.result_sha256 != workflow_json_sha256(
                row.result_json
            ):
                raise LearnerProjectionInvariantError("learner terminal result is corrupt")
            original = await session.scalar(
                select(WorkflowJobRow).where(
                    WorkflowJobRow.tenant_id == tenant_id,
                    WorkflowJobRow.job_id == job_id,
                )
            )
            if (
                original is None
                or original.command_id != row.command_id
                or original.operation != "EXECUTE_AGENT_TURN"
                or original.subject_type != "AGENT_TURN"
                or original.subject_id != row.turn_id
                or original.status != "SUCCEEDED"
                or original.phase != "COMPLETE"
                or original.lease_owner is not None
                or original.lease_expires_at is not None
            ):
                raise LearnerProjectionInvariantError(
                    "learner terminal result has no exact parent Turn closure"
                )
            return True


async def _owned_row(
    session: AsyncSession,
    claim: ClaimedLearnerProjectionJob,
    now: datetime,
    *,
    verify_objective: bool,
) -> LearnerProjectionJobRow:
    row = await session.scalar(
        select(LearnerProjectionJobRow)
        .where(*_claim_predicate(claim), LearnerProjectionJobRow.lease_expires_at > now)
        .with_for_update()
    )
    if row is None:
        raise LearnerProjectionFenceLost("learner fence was lost before terminal commit")
    if verify_objective:
        _verify_objective_hash(row)
    return row


async def _waiting_turn(
    session: AsyncSession, claim: ClaimedLearnerProjectionJob
) -> WorkflowJobRow:
    row = await session.scalar(
        select(WorkflowJobRow)
        .where(
            WorkflowJobRow.tenant_id == claim.tenant_id,
            WorkflowJobRow.job_id == claim.job_id,
        )
        .with_for_update()
    )
    if (
        row is None
        or row.command_id != claim.command_id
        or row.operation != "EXECUTE_AGENT_TURN"
        or row.subject_type != "AGENT_TURN"
        or row.subject_id != claim.turn_id
        or row.status != "WAITING_PROJECTION"
        or row.phase != "LEARNER_QUEUED"
        or row.lease_owner is not None
        or row.lease_expires_at is not None
    ):
        raise LearnerProjectionInvariantError("original Turn is not in its exact learner hand-off")
    return row


def _claim_predicate(claim: ClaimedLearnerProjectionJob) -> tuple[Any, ...]:
    return (
        LearnerProjectionJobRow.tenant_id == claim.tenant_id,
        LearnerProjectionJobRow.job_id == claim.job_id,
        LearnerProjectionJobRow.lease_owner == claim.lease_owner,
        LearnerProjectionJobRow.fencing_token == claim.fencing_token,
        LearnerProjectionJobRow.status.in_(_OWNED),
    )


def _turn_claim_is_current(
    row: WorkflowJobRow | None, claim: ClaimedWorkflowJob, now: datetime
) -> bool:
    return bool(
        row is not None
        and row.command_id == claim.command_id
        and row.operation == claim.operation
        and row.subject_type == claim.subject_type
        and row.subject_id == claim.subject_id
        and row.request_sha256 == claim.request_sha256
        and row.status in _OWNED
        and row.lease_owner == claim.lease_owner
        and row.fencing_token == claim.fencing_token
        and row.lease_expires_at is not None
        and row.lease_expires_at > now
    )


def _immutable_matches(
    row: LearnerProjectionJobRow,
    *,
    turn_claim: ClaimedWorkflowJob,
    command_id: str,
    session_id: str,
    turn_id: str,
    run_id: str,
    learner_id: str,
    actor_id: str,
    content_hash: str,
    source_event_id: str,
    expected_revision: int,
    through_sequence: int,
    request_sha256: str,
    projection: Mapping[str, Any],
) -> bool:
    return (
        row.job_id == turn_claim.job_id
        and row.tenant_id == turn_claim.tenant_id
        and row.command_id == command_id
        and row.session_id == session_id
        and row.turn_id == turn_id
        and row.run_id == run_id
        and row.learner_id == learner_id
        and row.actor_id == actor_id
        and row.content_hash == content_hash
        and row.source_event_id == source_event_id
        and row.expected_revision == expected_revision
        and row.through_sequence == through_sequence
        and row.request_sha256 == request_sha256
        and row.projection_json == dict(projection)
    )


def _verify_objective_hash(row: LearnerProjectionJobRow) -> None:
    if row.request_sha256 != workflow_json_sha256(row.projection_json):
        raise LearnerProjectionInvariantError("learner objective hash is corrupt")


def _claimed(row: LearnerProjectionJobRow) -> ClaimedLearnerProjectionJob:
    if row.status not in _OWNED or row.lease_owner is None or row.lease_expires_at is None:
        raise LearnerProjectionInvariantError("claimed learner objective has no complete lease")
    return ClaimedLearnerProjectionJob(
        job_id=row.job_id,
        tenant_id=row.tenant_id,
        command_id=row.command_id,
        session_id=row.session_id,
        turn_id=row.turn_id,
        run_id=row.run_id,
        learner_id=row.learner_id,
        actor_id=row.actor_id,
        content_hash=row.content_hash,
        source_event_id=row.source_event_id,
        expected_revision=row.expected_revision,
        through_sequence=row.through_sequence,
        status=row.status,
        attempt=row.attempt,
        fencing_token=row.fencing_token,
        lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at,
        request_sha256=row.request_sha256,
        projection=dict(row.projection_json),
        created_at=row.created_at,
    )


async def _database_now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise LearnerProjectionInvariantError("PostgreSQL returned an invalid timestamp")
    return value


def _bounded(value: str, maximum: int, name: str) -> None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{name} must be a bounded non-empty string")


def _sha256(value: str, name: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{name} must be lowercase SHA-256")


__all__ = [
    "ClaimedLearnerProjectionJob",
    "LearnerProjectionFenceLost",
    "LearnerProjectionInvariantError",
    "LearnerProjectionRetryableError",
    "PostgresLearnerProjectionJobStore",
]
