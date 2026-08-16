"""Durable workflow claims, fencing, and immutable step receipts.

The public Gateway accepts commands transactionally, but it never executes a
long-running build, sandbox, or projection inside the HTTP request.  This
adapter is the backend-owned hand-off boundary.  Every claim receives a
monotonic fencing token and every externally visible step is reconciled by an
immutable receipt before a worker may advance the resource or Command.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import CommandRow, JobStepReceiptRow, WorkflowJobRow

WorkflowStatus = Literal[
    "ACCEPTED",
    "READY",
    "CLAIMED",
    "RUNNING",
    "RETRY_WAIT",
    "WAITING_PROJECTION",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "DEAD_LETTER",
]

_CLAIMABLE = ("ACCEPTED", "READY", "RETRY_WAIT")
_RECOVERABLE = ("CLAIMED", "RUNNING")
_OWNED = ("CLAIMED", "RUNNING")
_TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTER")
_WORKFLOW_BOUNDARY_STAGES = frozenset(
    {
        "FINAL_CONTEXT_BUILD",
        "FINAL_DECISION_LOAD_RUN",
        "FINAL_DECISION_SHAPE",
        "OUTCOME_AUTHORITY",
        "PROVIDER_DECISION_WIRE",
        "PROVIDER_RECEIPT_HISTORY",
        "RECORD_RECEIPT",
        "RUNTIME_TRACE_AUTHORITY",
        "FINAL_RUNTIME_PRE_DISPATCH",
        "FINAL_RUNTIME_LLM_GENERATE",
        "FINAL_RUNTIME_PARSE_MODEL_ENVELOPE",
        "FINAL_RUNTIME_VALIDATE_DECISION",
        "FINAL_RUNTIME_MERGE_EVIDENCE",
        "FINAL_RUNTIME_DECISION_TIME",
        "FINAL_RUNTIME_CONSTRUCT_AGENT_DECISION",
    }
)


class WorkflowInvariantError(RuntimeError):
    """Durable job bytes or state violate the workflow protocol."""


class WorkflowBoundaryError(WorkflowInvariantError):
    """Unexpected validation failure at one fixed, non-secret workflow boundary."""

    def __init__(self, stage: str) -> None:
        if stage not in _WORKFLOW_BOUNDARY_STAGES:
            raise ValueError("unsupported workflow boundary")
        super().__init__("workflow boundary rejected a validated value")
        self.stage = stage


class WorkflowRetryableError(RuntimeError):
    """A transient workflow failure with an optional authoritative retry delay."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        if retry_after_seconds is not None and (
            isinstance(retry_after_seconds, bool) or not 1 <= retry_after_seconds <= 86_400
        ):
            raise ValueError("retry_after_seconds must be between 1 and 86400")
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class WorkflowReconciliationPending(WorkflowRetryableError):
    """A durable external operation is still observable and must keep polling.

    Reconciliation waits are not failed executions.  In particular, they must
    never consume the worker's bounded normal-failure/dead-letter budget.
    """


class WorkflowFenceLost(RuntimeError):
    """The caller no longer owns the current fencing token."""


@dataclass(frozen=True, slots=True)
class ClaimedWorkflowJob:
    job_id: str
    tenant_id: str
    command_id: str
    operation: str
    subject_type: str
    subject_id: str
    phase: str
    status: WorkflowStatus
    attempt: int
    fencing_token: int
    lease_owner: str
    lease_expires_at: datetime
    request_sha256: str
    job: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class WorkflowStepReceipt:
    receipt_id: str
    tenant_id: str
    job_id: str
    step_name: str
    fencing_token: int
    input_sha256: str
    output_sha256: str
    receipt: Mapping[str, Any]
    completed_at: datetime
    created: bool


class PostgresWorkflowJobStore:
    """Backend-owned workflow queue with database-clock leases.

    The methods ending in ``_in_session`` deliberately do not commit.  Workers
    use them in the same transaction as a resource projection, Command CAS,
    registry CAS, or World commit, so no second table authority is introduced.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def enqueue_in_session(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        command_id: str,
        operation: str,
        subject_type: str,
        subject_id: str,
        request_sha256: str,
        job: Mapping[str, Any],
        phase: str = "ACCEPT",
    ) -> WorkflowJobRow:
        """Insert the one durable job for a Command or reconcile an exact replay."""

        _require_sha256(request_sha256, "request_sha256")
        value = dict(job)
        database_now = await _database_now(session)
        command = await session.scalar(
            select(CommandRow)
            .where(
                CommandRow.tenant_id == tenant_id,
                CommandRow.command_id == command_id,
            )
            .with_for_update()
        )
        if command is None:
            raise WorkflowInvariantError("workflow enqueue has no Command authority")
        now = max(database_now, command.accepted_at, command.updated_at)
        job_id = workflow_job_id(tenant_id, command_id)
        inserted = await session.scalar(
            insert(WorkflowJobRow)
            .values(
                job_id=job_id,
                tenant_id=tenant_id,
                command_id=command_id,
                operation=operation,
                subject_type=subject_type,
                subject_id=subject_id,
                phase=phase,
                status="READY",
                attempt=0,
                fencing_token=0,
                lease_owner=None,
                lease_expires_at=None,
                next_attempt_at=now,
                request_sha256=request_sha256,
                job_json=value,
                last_error_json=None,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_workflow_job_command")
            .returning(WorkflowJobRow.job_id)
        )
        row = await session.scalar(
            select(WorkflowJobRow).where(
                WorkflowJobRow.tenant_id == tenant_id,
                WorkflowJobRow.command_id == command_id,
            )
        )
        if row is None or (inserted is not None and inserted != row.job_id):
            raise WorkflowInvariantError("workflow enqueue did not materialize its job")
        immutable = (
            row.job_id == job_id
            and row.operation == operation
            and row.subject_type == subject_type
            and row.subject_id == subject_id
            and row.request_sha256 == request_sha256
            and row.job_json == value
        )
        if not immutable:
            raise WorkflowInvariantError(
                "command workflow identity was reused with different bytes"
            )
        return row

    async def claim_next(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        lease_seconds: int,
        operation: str | None = None,
    ) -> ClaimedWorkflowJob | None:
        """Claim one due job, including an expired claim, with a new fence."""

        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must be a bounded non-empty string")
        if isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        async with self._sessions() as session, session.begin():
            now = await _database_now(session)
            due = and_(
                WorkflowJobRow.status.in_(_CLAIMABLE),
                or_(
                    WorkflowJobRow.next_attempt_at.is_(None),
                    WorkflowJobRow.next_attempt_at <= now,
                ),
            )
            expired = and_(
                WorkflowJobRow.status.in_(_RECOVERABLE),
                WorkflowJobRow.lease_expires_at.is_not(None),
                WorkflowJobRow.lease_expires_at <= now,
            )
            statement = (
                select(WorkflowJobRow)
                .where(WorkflowJobRow.tenant_id == tenant_id, or_(due, expired))
                .order_by(WorkflowJobRow.created_at, WorkflowJobRow.job_id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if operation is not None:
                statement = statement.where(WorkflowJobRow.operation == operation)
            row = await session.scalar(statement)
            if row is None:
                return None
            now = await _causal_job_now(session, row, database_now=now)
            row.status = "CLAIMED"
            row.attempt += 1
            row.fencing_token += 1
            row.lease_owner = worker_id
            row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            row.next_attempt_at = None
            row.updated_at = now
            await session.flush()
            return _claimed(row)

    async def get_by_command(self, *, tenant_id: str, command_id: str) -> WorkflowJobRow | None:
        async with self._sessions() as session:
            return await session.scalar(
                select(WorkflowJobRow).where(
                    WorkflowJobRow.tenant_id == tenant_id,
                    WorkflowJobRow.command_id == command_id,
                )
            )

    async def start_step_in_session(
        self,
        session: AsyncSession,
        claim: ClaimedWorkflowJob,
        *,
        phase: str,
        lease_seconds: int,
    ) -> ClaimedWorkflowJob:
        """Advance the owned job to RUNNING and renew its database-clock lease."""

        if isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        database_now = await _database_now(session)
        row = await _owned_job(
            session,
            claim,
            database_now=database_now,
            fence_message=f"workflow fence lost before {phase}",
        )
        now = await _causal_job_now(session, row, database_now=database_now)
        assert row.lease_expires_at is not None
        row.status = "RUNNING"
        row.phase = phase
        row.lease_expires_at = max(
            row.lease_expires_at,
            now + timedelta(seconds=lease_seconds),
        )
        row.updated_at = now
        await session.flush()
        return _claimed(row)

    async def renew(self, claim: ClaimedWorkflowJob, *, lease_seconds: int) -> ClaimedWorkflowJob:
        async with self._sessions() as session, session.begin():
            return await self.start_step_in_session(
                session, claim, phase=claim.phase, lease_seconds=lease_seconds
            )

    async def record_step_in_session(
        self,
        session: AsyncSession,
        claim: ClaimedWorkflowJob,
        *,
        step_name: str,
        input_sha256: str,
        output: Mapping[str, Any],
    ) -> WorkflowStepReceipt:
        """Record or reconcile one immutable result while the claim is current."""

        _require_sha256(input_sha256, "input_sha256")
        if not step_name or len(step_name) > 64:
            raise ValueError("step_name must be a bounded non-empty string")
        output_value = dict(output)
        output_sha256 = workflow_receipt_sha256(output_value)
        database_now = await _database_now(session)
        current = await _owned_job(
            session,
            claim,
            database_now=database_now,
            fence_message=f"workflow fence lost before receipt {step_name}",
        )
        now = await _causal_job_now(session, current, database_now=database_now)
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
                fencing_token=claim.fencing_token,
                input_sha256=input_sha256,
                output_sha256=output_sha256,
                receipt_json=output_value,
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
        if row is None:
            raise WorkflowInvariantError("step receipt did not materialize")
        if (
            row.receipt_id != receipt_id
            or row.input_sha256 != input_sha256
            or row.output_sha256 != output_sha256
            or row.receipt_json != output_value
        ):
            raise WorkflowInvariantError(f"step receipt {step_name} conflicts with durable bytes")
        return WorkflowStepReceipt(
            receipt_id=row.receipt_id,
            tenant_id=row.tenant_id,
            job_id=row.job_id,
            step_name=row.step_name,
            fencing_token=row.fencing_token,
            input_sha256=row.input_sha256,
            output_sha256=row.output_sha256,
            receipt=dict(row.receipt_json),
            completed_at=row.completed_at,
            created=inserted is not None,
        )

    async def finish_in_session(
        self,
        session: AsyncSession,
        claim: ClaimedWorkflowJob,
        *,
        status: Literal["SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTER"],
        phase: str = "COMPLETE",
        error: Mapping[str, Any] | None = None,
    ) -> None:
        """Release a current fence only after caller projections are staged."""

        if status not in _TERMINAL:
            raise ValueError("finish status must be terminal")
        database_now = await _database_now(session)
        row = await _owned_job(
            session,
            claim,
            database_now=database_now,
            fence_message="workflow fence lost before terminal commit",
        )
        now = await _causal_job_now(session, row, database_now=database_now)
        row.status = status
        row.phase = phase
        row.lease_owner = None
        row.lease_expires_at = None
        row.next_attempt_at = None
        row.last_error_json = dict(error) if error is not None else None
        row.updated_at = now
        await session.flush()

    async def retry_in_session(
        self,
        session: AsyncSession,
        claim: ClaimedWorkflowJob,
        *,
        delay_seconds: int,
        phase: str,
        error: Mapping[str, Any],
    ) -> None:
        """Release a current fence into a durable, bounded retry wait."""

        if isinstance(delay_seconds, bool) or not 0 <= delay_seconds <= 86_400:
            raise ValueError("delay_seconds must be between 0 and 86400")
        database_now = await _database_now(session)
        row = await _owned_job(
            session,
            claim,
            database_now=database_now,
            fence_message="workflow fence lost before retry scheduling",
        )
        now = await _causal_job_now(session, row, database_now=database_now)
        row.status = "RETRY_WAIT"
        row.phase = phase
        row.lease_owner = None
        row.lease_expires_at = None
        row.next_attempt_at = now + timedelta(seconds=delay_seconds)
        row.last_error_json = dict(error)
        row.updated_at = now
        await session.flush()


async def _owned_job(
    session: AsyncSession,
    claim: ClaimedWorkflowJob,
    *,
    database_now: datetime,
    fence_message: str,
) -> WorkflowJobRow:
    row = await session.scalar(
        select(WorkflowJobRow)
        .where(
            *_claim_predicate(claim),
            WorkflowJobRow.lease_expires_at > database_now,
        )
        .with_for_update()
    )
    if row is None:
        raise WorkflowFenceLost(fence_message)
    return row


async def _causal_job_now(
    session: AsyncSession,
    row: WorkflowJobRow,
    *,
    database_now: datetime,
) -> datetime:
    """Return one monotonic timestamp for a locked Job and its receipts."""

    last_receipt_at = await session.scalar(
        select(func.max(JobStepReceiptRow.completed_at)).where(
            JobStepReceiptRow.tenant_id == row.tenant_id,
            JobStepReceiptRow.job_id == row.job_id,
        )
    )
    return max(
        value
        for value in (database_now, row.created_at, row.updated_at, last_receipt_at)
        if value is not None
    )


def _claim_predicate(claim: ClaimedWorkflowJob) -> tuple[Any, ...]:
    return (
        WorkflowJobRow.tenant_id == claim.tenant_id,
        WorkflowJobRow.job_id == claim.job_id,
        WorkflowJobRow.lease_owner == claim.lease_owner,
        WorkflowJobRow.fencing_token == claim.fencing_token,
        WorkflowJobRow.status.in_(_OWNED),
    )


def _claimed(row: WorkflowJobRow) -> ClaimedWorkflowJob:
    if row.lease_owner is None or row.lease_expires_at is None:
        raise WorkflowInvariantError("claimed workflow has no complete lease")
    if row.status not in _OWNED:
        raise WorkflowInvariantError("claimed workflow has a non-owned status")
    return ClaimedWorkflowJob(
        job_id=row.job_id,
        tenant_id=row.tenant_id,
        command_id=row.command_id,
        operation=row.operation,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        phase=row.phase,
        status=row.status,
        attempt=row.attempt,
        fencing_token=row.fencing_token,
        lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at,
        request_sha256=row.request_sha256,
        job=dict(row.job_json),
    )


async def _database_now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WorkflowInvariantError("PostgreSQL returned an invalid clock timestamp")
    return value


def workflow_json_sha256(value: Mapping[str, Any]) -> str:
    """Hash one internal workflow JSON object, including finite fractions."""

    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise WorkflowInvariantError("workflow value is not finite canonical JSON") from error
    return hashlib.sha256(payload).hexdigest()


def workflow_receipt_sha256(value: Mapping[str, Any]) -> str:
    """Hash receipt JSON with the exact codec used by the workflow writer."""

    return workflow_json_sha256(value)


def _scoped_identifier(prefix: str, *parts: str) -> str:
    framed = "\x00".join((prefix, *parts)).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(framed).hexdigest()[:24]}"


def workflow_job_id(tenant_id: str, command_id: str) -> str:
    """Return the one public Job identity for a tenant-scoped Command."""

    return _scoped_identifier("job", tenant_id, command_id)


def workflow_step_receipt_id(tenant_id: str, job_id: str, step_name: str) -> str:
    """Return the canonical immutable receipt identity for one workflow step."""

    if not step_name or len(step_name) > 64:
        raise ValueError("step_name must be a bounded non-empty string")
    return _scoped_identifier("receipt", tenant_id, job_id, step_name)


def _require_sha256(value: str, name: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{name} must be lowercase SHA-256")


__all__ = [
    "ClaimedWorkflowJob",
    "PostgresWorkflowJobStore",
    "WorkflowFenceLost",
    "WorkflowInvariantError",
    "WorkflowReconciliationPending",
    "WorkflowRetryableError",
    "WorkflowStepReceipt",
    "workflow_job_id",
    "workflow_json_sha256",
    "workflow_receipt_sha256",
    "workflow_step_receipt_id",
]
