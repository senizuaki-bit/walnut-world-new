"""Persistent learner-inference projection worker.

The worker owns scheduling and recovery only.  A production projector must
implement :class:`FencedLearnerProjectionPort` so learner snapshot, source
receipt, derived event/outbox and terminal Job state are committed in one
PostgreSQL transaction after re-checking the live lease and fencing token.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, cast

import psycopg
from psycopg.types.json import Jsonb
from yaya_agent_contracts import (
    ContractError,
    ErrorCategory,
    EvidenceRef,
    Failure,
    FrozenJsonObject,
    LearnerModelSnapshot,
    LearnerUpdate,
    OperationContext,
    Result,
    RuntimeEvent,
    RuntimeEventType,
    Success,
    canonical_json_sha256,
    learner_inference_sha256,
)

from .codec import (
    agent_turn_commit_sha256,
    decode_as,
    encode,
    internal_record_sha256,
    plain,
)
from .database import PostgresCommitStateUnknown, PostgresDatabase
from .learner_model_integrity import validate_persisted_learner_snapshot

_PROJECTION_SOURCE_EVENT = RuntimeEventType.LEARNER_INFERENCE_RECORDED
_RECOVERABLE_ERROR_CODES = frozenset(
    {
        "DEPENDENCY_UNAVAILABLE",
        "EVENT_SEQUENCE_GAP",
        "RATE_LIMITED",
        "UNKNOWN_COMMIT_STATE",
    }
)


class LearnerProjectionWorkerError(RuntimeError):
    """Base error with a stable operational classification."""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class LearnerProjectionFenceLost(LearnerProjectionWorkerError):
    """The Job is expired, completed or owned by a newer claim generation."""

    def __init__(self) -> None:
        super().__init__(
            "LEARNER_PROJECTION_FENCE_LOST",
            "Learner projection lease is stale, expired, or taken over",
            retryable=False,
        )


class LearnerProjectionDurableGraphCorrupt(LearnerProjectionWorkerError):
    """A terminal projection graph is present but fails canonical verification."""

    def __init__(self, cause: str) -> None:
        super().__init__(
            "INVARIANT_VIOLATION",
            f"Learner projection durable graph is corrupt: {cause[:128]}",
            retryable=False,
        )


class _DurableGraphState(Enum):
    """Tri-state reconciliation result for an atomic projection graph."""

    MISSING = "MISSING"
    MATCH = "MATCH"
    CORRUPT = "CORRUPT"


@dataclass(frozen=True, slots=True)
class LearnerProjectionFence:
    tenant_id: str
    job_id: str
    worker_id: str
    lease_id: str
    fencing_token: int


@dataclass(frozen=True, slots=True)
class LearnerProjectionLease:
    tenant_id: str
    job_id: str
    event_id: str
    learner_id: str
    source_stream_sequence: int
    expected_learner_revision: int
    lease_seconds: int
    fence: LearnerProjectionFence


class FencedLearnerProjectionPort(Protocol):
    """Backend extension implemented alongside the public ``LearnerPort``.

    ``project_fenced`` applies the same semantics as ``LearnerPort.project``.
    In addition, its transaction must lock the Job and verify every field in
    ``fence`` plus ``lease_expires_at > clock_timestamp()`` before writing.
    On success that transaction also writes the immutable receipt, the
    learner.model.updated event/outbox and changes the Job to ``SUCCEEDED``.

    ``fail_fenced`` performs the corresponding atomic terminal failure path:
    immutable failure record, learner.projection.failed event/outbox and Job
    ``FAILED``.  Neither method may return ``Success`` before those records are
    durable and mutually consistent.
    """

    async def project_fenced(
        self,
        event: RuntimeEvent,
        expected_learner_revision: int,
        context: OperationContext,
        fence: LearnerProjectionFence,
    ) -> Result[LearnerUpdate]: ...

    async def fail_fenced(
        self,
        event: RuntimeEvent,
        error: ContractError,
        context: OperationContext,
        fence: LearnerProjectionFence,
    ) -> Result[None]: ...


@dataclass(frozen=True, slots=True)
class _ProjectionInput:
    event: RuntimeEvent
    context: OperationContext


@dataclass(frozen=True, slots=True)
class _TerminalAuditInput:
    lease: LearnerProjectionLease
    terminal_state: str
    terminal_kind: str
    error: ContractError | None
    quarantine_error: Mapping[str, object] | None


class LearnerProjectionWorker:
    """Restart-safe ordered projector using PostgreSQL leases and fences."""

    def __init__(
        self,
        *,
        database: PostgresDatabase,
        learner: FencedLearnerProjectionPort,
        worker_id: str,
        lease_seconds: int,
        poll_ms: int,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        if not worker_id.strip() or len(worker_id) > 128:
            raise ValueError("worker_id must contain 1 to 128 characters")
        if lease_seconds < 2:
            raise ValueError("learner projection lease_seconds must be at least 2")
        if poll_ms < 10:
            raise ValueError("learner projection poll_ms must be at least 10")
        if not 0.01 <= retry_delay_seconds <= 3600:
            raise ValueError("retry_delay_seconds is outside the supported range")
        if not callable(getattr(learner, "project_fenced", None)) or not callable(
            getattr(learner, "fail_fenced", None)
        ):
            raise TypeError("production learner projector must implement fenced methods")
        self._database = database
        self._learner = learner
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._poll_ms = poll_ms
        self._retry_delay_seconds = retry_delay_seconds

    async def claim_one(self) -> LearnerProjectionLease | None:
        """Claim only the next contiguous source event for one learner."""

        lease_id = f"learner_lease_{uuid.uuid4().hex}"
        try:
            async with self._database.transaction() as connection:
                cursor = await connection.execute(
                    """
                    SELECT j.tenant_id,j.job_id,j.event_id,j.learner_id,
                           j.source_stream_sequence,j.attempt,
                           COALESCE(m.revision,0) AS learner_revision
                    FROM yaya_learner_projection_jobs j
                    LEFT JOIN yaya_learner_models m
                      ON m.tenant_id=j.tenant_id AND m.learner_id=j.learner_id
                    WHERE (
                        (j.state='READY' AND j.available_at<=clock_timestamp())
                        OR
                        (j.state='LEASED' AND j.lease_expires_at<=clock_timestamp())
                    )
                      AND j.source_stream_sequence=
                          COALESCE(m.projected_through_sequence,0)+1
                      AND NOT EXISTS (
                          SELECT 1
                          FROM yaya_learner_projection_terminal_audits a
                          JOIN yaya_learner_projection_jobs terminal_j
                            ON terminal_j.tenant_id=a.tenant_id
                           AND terminal_j.job_id=a.job_id
                          WHERE a.tenant_id=j.tenant_id
                            AND terminal_j.learner_id=j.learner_id
                            AND a.verified_at IS NULL
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM yaya_learner_projection_failures f
                          WHERE f.tenant_id=j.tenant_id
                            AND f.learner_id=j.learner_id
                            AND f.source_stream_sequence<=j.source_stream_sequence
                            AND f.classification IN ('PERMANENT','QUARANTINED')
                            AND f.resolved_at IS NULL
                      )
                    ORDER BY j.available_at,j.created_at,j.tenant_id,j.job_id
                    FOR UPDATE OF j SKIP LOCKED
                    LIMIT 1
                    """
                )
                row = await cursor.fetchone()
                if row is None:
                    return None
                previous_attempt = cast(int, row["attempt"])
                fencing_token = previous_attempt + 1
                updated = await connection.execute(
                    """
                    UPDATE yaya_learner_projection_jobs
                    SET state='LEASED',attempt=%s,fencing_token=%s,
                        worker_id=%s,lease_id=%s,
                        claimed_at=clock_timestamp(),
                        heartbeat_at=clock_timestamp(),
                        lease_expires_at=
                            clock_timestamp()+%s*interval '1 second',
                        last_error_code=NULL,last_error_json=NULL,
                        updated_at=clock_timestamp()
                    WHERE tenant_id=%s AND job_id=%s
                    RETURNING job_id
                    """,
                    (
                        fencing_token,
                        fencing_token,
                        self._worker_id,
                        lease_id,
                        self._lease_seconds,
                        row["tenant_id"],
                        row["job_id"],
                    ),
                )
                if await updated.fetchone() is None:
                    raise RuntimeError("locked learner projection Job disappeared")
                tenant_id = cast(str, row["tenant_id"])
                job_id = cast(str, row["job_id"])
                fence = LearnerProjectionFence(
                    tenant_id=tenant_id,
                    job_id=job_id,
                    worker_id=self._worker_id,
                    lease_id=lease_id,
                    fencing_token=fencing_token,
                )
                return LearnerProjectionLease(
                    tenant_id=tenant_id,
                    job_id=job_id,
                    event_id=cast(str, row["event_id"]),
                    learner_id=cast(str, row["learner_id"]),
                    source_stream_sequence=cast(int, row["source_stream_sequence"]),
                    expected_learner_revision=cast(int, row["learner_revision"]),
                    lease_seconds=self._lease_seconds,
                    fence=fence,
                )
        except psycopg.Error as error:
            raise LearnerProjectionWorkerError(
                "DEPENDENCY_UNAVAILABLE",
                "PostgreSQL could not claim a learner projection Job",
                retryable=True,
            ) from error

    async def _audit_one_terminal(self) -> bool:
        """Verify one durable terminal graph before claiming new projection work."""

        audit = await self._load_terminal_audit()
        if audit is None:
            return False
        if audit.terminal_kind == "SUCCESS":
            reconciliation = await self._success_graph_state(audit.lease)
        elif audit.terminal_kind == "PERMANENT_FAILURE" and audit.error is not None:
            reconciliation = await self._failure_graph_state(audit.lease, audit.error)
        elif audit.terminal_kind == "QUARANTINE" and audit.quarantine_error is not None:
            reconciliation = await self._quarantine_graph_state(
                audit.lease,
                audit.quarantine_error,
            )
        else:
            raise LearnerProjectionDurableGraphCorrupt(
                "terminal audit has an invalid kind or error payload"
            )
        if reconciliation is not _DurableGraphState.MATCH:
            raise LearnerProjectionDurableGraphCorrupt(
                f"{audit.terminal_kind}_GRAPH_{reconciliation.value}"
            )
        marked = await self._mark_terminal_audited(
            audit.lease,
            audit.terminal_state,
            audit.terminal_kind,
            require_projection_generation=False,
        )
        if not marked:
            raise LearnerProjectionDurableGraphCorrupt(
                "terminal graph matched but its audit obligation was not verifiable"
            )
        return True

    async def _load_terminal_audit(self) -> _TerminalAuditInput | None:
        try:
            connection = await self._database.connect(autocommit=True)
            try:
                cursor = await connection.execute(
                    """
                    SELECT a.terminal_state,a.terminal_kind,a.terminal_at,
                           a.attempt AS audit_attempt,
                           a.fencing_token AS audit_fencing_token,
                           j.tenant_id,j.job_id,j.event_id,j.learner_id,
                           j.source_stream_sequence,j.attempt,j.fencing_token,
                           j.state,j.succeeded_at,j.failed_at,
                           j.last_error_code,j.last_error_json
                    FROM yaya_learner_projection_terminal_audits a
                    JOIN yaya_learner_projection_jobs j
                      ON j.tenant_id=a.tenant_id AND j.job_id=a.job_id
                    WHERE a.verified_at IS NULL
                    ORDER BY a.terminal_at,a.tenant_id,a.job_id
                    LIMIT 1
                    """
                )
                row = await cursor.fetchone()
            finally:
                await connection.close()
        except psycopg.Error as caught:
            raise LearnerProjectionWorkerError(
                "DEPENDENCY_UNAVAILABLE",
                "PostgreSQL could not load a terminal learner projection audit",
                retryable=True,
            ) from caught
        if row is None:
            return None
        try:
            terminal_state = cast(str, row["terminal_state"])
            terminal_kind = cast(str, row["terminal_kind"])
            attempt = cast(int, row["attempt"])
            source_sequence = cast(int, row["source_stream_sequence"])
            if (
                terminal_state not in {"SUCCEEDED", "FAILED"}
                or terminal_kind not in {"SUCCESS", "PERMANENT_FAILURE", "QUARANTINE"}
                or (terminal_state == "SUCCEEDED") != (terminal_kind == "SUCCESS")
                or row["state"] != terminal_state
                or attempt < 1
                or row["fencing_token"] != attempt
                or row["audit_attempt"] != attempt
                or row["audit_fencing_token"] != attempt
                or source_sequence < 1
                or row["terminal_at"]
                != (row["succeeded_at"] if terminal_state == "SUCCEEDED" else row["failed_at"])
            ):
                raise ValueError("terminal audit and Job identity differ")
            error: ContractError | None = None
            quarantine_error: Mapping[str, object] | None = None
            if terminal_kind == "PERMANENT_FAILURE":
                error = self._contract_error_from_wire(row["last_error_json"])
                if row["last_error_code"] != error.code or row[
                    "last_error_json"
                ] != self._error_wire(error):
                    raise ValueError("terminal failure error wire is not canonical")
            elif terminal_kind == "QUARANTINE":
                quarantine_error = self._quarantine_error_from_wire(row["last_error_json"])
                if (
                    row["last_error_code"] != quarantine_error["code"]
                    or row["last_error_json"] != quarantine_error
                ):
                    raise ValueError("terminal quarantine error wire is not canonical")
            elif row["last_error_code"] is not None or row["last_error_json"] is not None:
                raise ValueError("successful terminal audit contains a failure error")
            tenant_id = cast(str, row["tenant_id"])
            job_id = cast(str, row["job_id"])
            lease = LearnerProjectionLease(
                tenant_id=tenant_id,
                job_id=job_id,
                event_id=cast(str, row["event_id"]),
                learner_id=cast(str, row["learner_id"]),
                source_stream_sequence=source_sequence,
                expected_learner_revision=source_sequence - 1,
                lease_seconds=self._lease_seconds,
                fence=LearnerProjectionFence(
                    tenant_id=tenant_id,
                    job_id=job_id,
                    worker_id=self._worker_id,
                    lease_id=f"terminal_audit:{job_id}",
                    fencing_token=attempt,
                ),
            )
        except (KeyError, TypeError, ValueError) as caught:
            raise LearnerProjectionDurableGraphCorrupt(
                "terminal audit contains invalid persisted identity or error data"
            ) from caught
        return _TerminalAuditInput(
            lease=lease,
            terminal_state=terminal_state,
            terminal_kind=terminal_kind,
            error=error,
            quarantine_error=quarantine_error,
        )

    async def _mark_terminal_audited(
        self,
        lease: LearnerProjectionLease,
        terminal_state: str,
        terminal_kind: str,
        *,
        require_projection_generation: bool = True,
    ) -> bool:
        """Complete only the audit obligation; never mutate a terminal graph."""

        try:
            async with self._database.transaction() as connection:
                updated = await connection.execute(
                    """
                    UPDATE yaya_learner_projection_terminal_audits AS a
                    SET verified_at=clock_timestamp(),verified_by=%s
                    FROM yaya_learner_projection_jobs AS j
                    WHERE a.tenant_id=%s AND a.job_id=%s
                      AND a.terminal_state=%s AND a.terminal_kind=%s
                      AND a.verified_at IS NULL
                      AND j.tenant_id=a.tenant_id AND j.job_id=a.job_id
                      AND j.state=a.terminal_state
                      AND j.attempt=a.attempt
                      AND j.fencing_token=a.fencing_token
                      AND (
                          NOT %s
                          OR (a.attempt=%s AND a.fencing_token=%s)
                      )
                    RETURNING a.job_id
                    """,
                    (
                        self._worker_id,
                        lease.tenant_id,
                        lease.job_id,
                        terminal_state,
                        terminal_kind,
                        require_projection_generation,
                        lease.fence.fencing_token,
                        lease.fence.fencing_token,
                    ),
                )
                if await updated.fetchone() is not None:
                    return True
                existing_cursor = await connection.execute(
                    """
                    SELECT a.terminal_state,a.terminal_kind,
                           a.attempt AS audit_attempt,
                           a.fencing_token AS audit_fencing_token,
                           a.verified_at,a.verified_by,
                           j.state,j.attempt,j.fencing_token
                    FROM yaya_learner_projection_terminal_audits a
                    JOIN yaya_learner_projection_jobs j
                      ON j.tenant_id=a.tenant_id AND j.job_id=a.job_id
                    WHERE a.tenant_id=%s AND a.job_id=%s
                    """,
                    (lease.tenant_id, lease.job_id),
                )
                existing = await existing_cursor.fetchone()
                if (
                    existing is not None
                    and existing["terminal_state"] == terminal_state
                    and existing["terminal_kind"] == terminal_kind
                    and existing["state"] == terminal_state
                    and existing["audit_attempt"] == existing["attempt"]
                    and existing["audit_fencing_token"] == existing["fencing_token"]
                    and existing["verified_at"] is not None
                    and existing["verified_by"] is not None
                ):
                    return True
                if (
                    require_projection_generation
                    and existing is not None
                    and existing["terminal_state"] == terminal_state
                    and existing["terminal_kind"] == terminal_kind
                    and existing["state"] == terminal_state
                    and existing["audit_attempt"] == existing["attempt"]
                    and existing["audit_fencing_token"] == existing["fencing_token"]
                    and existing["verified_at"] is None
                    and (
                        existing["audit_attempt"] != lease.fence.fencing_token
                        or existing["audit_fencing_token"] != lease.fence.fencing_token
                    )
                ):
                    return False
        except psycopg.Error as error:
            raise LearnerProjectionWorkerError(
                "DEPENDENCY_UNAVAILABLE",
                "PostgreSQL could not complete a terminal projection audit",
                retryable=True,
            ) from error
        raise LearnerProjectionDurableGraphCorrupt(
            "terminal audit obligation is missing or contradicts its Job"
        )

    async def run_once(self) -> bool:
        if await self._audit_one_terminal():
            return True
        lease = await self.claim_one()
        if lease is None:
            return False
        stop_heartbeat = asyncio.Event()
        lost_lease = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(lease, stop_heartbeat, lost_lease),
            name=f"learner-projection-heartbeat:{lease.job_id}",
        )
        work = asyncio.create_task(
            self._process(lease),
            name=f"learner-projection:{lease.job_id}",
        )
        lost_wait = asyncio.create_task(lost_lease.wait())
        try:
            done, _ = await asyncio.wait(
                {work, lost_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lost_wait in done and lost_lease.is_set() and not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                return True
            await work
            return True
        except asyncio.CancelledError:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            raise
        except LearnerProjectionFenceLost:
            return True
        except LearnerProjectionDurableGraphCorrupt:
            raise
        except LearnerProjectionWorkerError as error:
            if error.retryable:
                try:
                    await self._release_for_retry(
                        lease,
                        error.code,
                        self._error_record(error.code, type(error).__name__),
                    )
                except LearnerProjectionFenceLost:
                    return True
            else:
                try:
                    await self._quarantine(
                        lease,
                        error.code,
                        self._error_record(error.code, type(error).__name__),
                    )
                except LearnerProjectionFenceLost:
                    return True
            return True
        except (psycopg.Error, PostgresCommitStateUnknown, TimeoutError, ConnectionError) as error:
            try:
                await self._release_for_retry(
                    lease,
                    "DEPENDENCY_UNAVAILABLE",
                    self._error_record("DEPENDENCY_UNAVAILABLE", type(error).__name__),
                )
            except LearnerProjectionFenceLost:
                return True
            return True
        except Exception as error:
            try:
                await self._quarantine(
                    lease,
                    "INVARIANT_VIOLATION",
                    self._error_record("INVARIANT_VIOLATION", type(error).__name__),
                )
            except LearnerProjectionFenceLost:
                return True
            return True
        finally:
            lost_wait.cancel()
            stop_heartbeat.set()
            await asyncio.gather(heartbeat, lost_wait, return_exceptions=True)

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Drain Jobs until ``stop`` is set; never abandon an active call early."""

        while not stop.is_set():
            try:
                processed = await self.run_once()
            except LearnerProjectionWorkerError as error:
                if not error.retryable:
                    raise
                processed = False
            except psycopg.Error:
                processed = False
            if not processed:
                try:
                    await asyncio.wait_for(stop.wait(), self._poll_ms / 1000)
                except TimeoutError:
                    continue

    async def _process(self, lease: LearnerProjectionLease) -> None:
        projection = await self._load_projection_input(lease)
        try:
            result = await self._learner.project_fenced(
                projection.event,
                lease.expected_learner_revision,
                projection.context,
                lease.fence,
            )
        except (psycopg.Error, PostgresCommitStateUnknown, TimeoutError, ConnectionError):
            reconciliation = await self._success_graph_state(lease)
            if reconciliation is _DurableGraphState.MATCH:
                await self._mark_terminal_audited(lease, "SUCCEEDED", "SUCCESS")
                return
            if reconciliation is _DurableGraphState.CORRUPT:
                await self._record_graph_corruption(
                    lease,
                    "SUCCESS_GRAPH_CORRUPT_AFTER_UNKNOWN_COMMIT",
                )
                return
            raise
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failure = self._invariant_failure(type(error).__name__)
            await self._persist_permanent_failure(lease, projection, failure)
            return
        if isinstance(result, Success):
            reconciliation = await self._success_graph_state(lease, result.value)
            if reconciliation is _DurableGraphState.CORRUPT:
                await self._record_graph_corruption(
                    lease,
                    "SUCCESS_GRAPH_CORRUPT_AFTER_SUCCESS",
                )
                return
            if reconciliation is _DurableGraphState.MISSING:
                self._validate_update(lease, result.value)
                raise LearnerProjectionWorkerError(
                    "UNKNOWN_COMMIT_STATE",
                    "Projector returned success without a durable projection graph",
                    retryable=True,
                )
            await self._mark_terminal_audited(lease, "SUCCEEDED", "SUCCESS")
            return
        if not isinstance(result, Failure):
            raise LearnerProjectionWorkerError(
                "INVARIANT_VIOLATION",
                "Fenced learner projector returned an invalid Result",
                retryable=False,
            )
        error = result.error
        if error.code == "EVENT_SEQUENCE_GAP":
            reconciliation = await self._success_graph_state(lease)
            if reconciliation is _DurableGraphState.MATCH:
                await self._mark_terminal_audited(lease, "SUCCEEDED", "SUCCESS")
                return
            if reconciliation is _DurableGraphState.CORRUPT:
                await self._record_graph_corruption(
                    lease,
                    "SUCCESS_GRAPH_CORRUPT_AFTER_CAS_CONFLICT",
                )
                return
            checkpoint = await self._read_checkpoint(lease)
            if checkpoint[1] >= lease.source_stream_sequence:
                await self._record_graph_corruption(
                    lease,
                    "MODEL_ADVANCED_WITHOUT_DURABLE_SUCCESS_GRAPH",
                )
                return
            raise LearnerProjectionWorkerError(
                "EVENT_SEQUENCE_GAP",
                "Learner revision CAS changed before projection; retry after reread",
                retryable=True,
            )
        if error.code == "UNKNOWN_COMMIT_STATE":
            reconciliation = await self._success_graph_state(lease)
            if reconciliation is _DurableGraphState.MATCH:
                await self._mark_terminal_audited(lease, "SUCCEEDED", "SUCCESS")
                return
            if reconciliation is _DurableGraphState.CORRUPT:
                await self._record_graph_corruption(
                    lease,
                    "SUCCESS_GRAPH_CORRUPT_AFTER_UNKNOWN_RESULT",
                )
                return
            raise LearnerProjectionWorkerError(
                "UNKNOWN_COMMIT_STATE",
                "Learner projection COMMIT outcome requires receipt reconciliation",
                retryable=True,
            )
        if error.code in _RECOVERABLE_ERROR_CODES and error.retryable:
            raise LearnerProjectionWorkerError(
                error.code,
                "Recoverable learner projection dependency failure",
                retryable=True,
            )
        await self._persist_permanent_failure(lease, projection, error)

    async def _persist_permanent_failure(
        self,
        lease: LearnerProjectionLease,
        projection: _ProjectionInput,
        error: ContractError,
    ) -> None:
        try:
            outcome = await self._learner.fail_fenced(
                projection.event,
                error,
                projection.context,
                lease.fence,
            )
        except (psycopg.Error, PostgresCommitStateUnknown, TimeoutError, ConnectionError):
            reconciliation = await self._failure_graph_state(lease, error)
            if reconciliation is _DurableGraphState.MATCH:
                await self._mark_terminal_audited(
                    lease,
                    "FAILED",
                    "PERMANENT_FAILURE",
                )
                return
            if reconciliation is _DurableGraphState.CORRUPT:
                await self._record_graph_corruption(
                    lease,
                    "FAILURE_GRAPH_CORRUPT_AFTER_UNKNOWN_COMMIT",
                )
                return
            raise
        reconciliation = await self._failure_graph_state(lease, error)
        if reconciliation is _DurableGraphState.MATCH:
            await self._mark_terminal_audited(
                lease,
                "FAILED",
                "PERMANENT_FAILURE",
            )
            return
        if reconciliation is _DurableGraphState.CORRUPT:
            await self._record_graph_corruption(
                lease,
                "FAILURE_GRAPH_CORRUPT_AFTER_PROJECTOR_RESPONSE",
            )
            return
        if isinstance(outcome, Failure):
            if outcome.error.code in _RECOVERABLE_ERROR_CODES or outcome.error.retryable:
                raise LearnerProjectionWorkerError(
                    outcome.error.code,
                    "Permanent projection failure could not be recorded yet",
                    retryable=True,
                )
            raise LearnerProjectionWorkerError(
                outcome.error.code,
                "Fenced projector rejected terminal failure persistence",
                retryable=False,
            )
        if not isinstance(outcome, Success) or outcome.value is not None:
            raise LearnerProjectionWorkerError(
                "INVARIANT_VIOLATION",
                "fail_fenced returned an invalid Result",
                retryable=False,
            )
        raise LearnerProjectionWorkerError(
            "UNKNOWN_COMMIT_STATE",
            "Projector returned terminal failure without a durable failure graph",
            retryable=True,
        )

    async def _heartbeat(
        self,
        lease: LearnerProjectionLease,
        stop: asyncio.Event,
        lost: asyncio.Event,
    ) -> None:
        interval = max(0.25, lease.lease_seconds / 3)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), interval)
                return
            except TimeoutError:
                if stop.is_set():
                    return
            try:
                connection = await self._database.connect(autocommit=True)
                try:
                    cursor = await connection.execute(
                        """
                        UPDATE yaya_learner_projection_jobs
                        SET heartbeat_at=clock_timestamp(),
                            lease_expires_at=
                                clock_timestamp()+%s*interval '1 second',
                            updated_at=clock_timestamp()
                        WHERE tenant_id=%s AND job_id=%s AND state='LEASED'
                          AND worker_id=%s AND lease_id=%s AND fencing_token=%s
                          AND lease_expires_at>clock_timestamp()
                        RETURNING job_id
                        """,
                        (
                            lease.lease_seconds,
                            lease.tenant_id,
                            lease.job_id,
                            lease.fence.worker_id,
                            lease.fence.lease_id,
                            lease.fence.fencing_token,
                        ),
                    )
                    if await cursor.fetchone() is None:
                        lost.set()
                        return
                finally:
                    await connection.close()
            except psycopg.Error:
                lost.set()
                return

    async def _load_projection_input(
        self,
        lease: LearnerProjectionLease,
    ) -> _ProjectionInput:
        try:
            async with self._database.transaction() as connection:
                cursor = await connection.execute(
                    """
                    SELECT j.*,e.event_type AS durable_event_type,
                           e.event_json AS durable_event_json,
                           t.event_sha256 AS durable_source_event_sha256,
                           t.actor_id AS durable_source_actor_id,
                           t.content_hash AS durable_source_content_hash,
                           t.record_json AS turn_record_json,
                           m.actor_id AS model_actor_id,
                           m.content_hash AS model_content_hash,
                           m.revision AS current_learner_revision,
                           m.projected_through_sequence AS current_checkpoint
                    FROM yaya_learner_projection_jobs j
                    JOIN yaya_events e
                      ON e.tenant_id=j.tenant_id AND e.event_id=j.event_id
                     AND e.stream_id=j.source_stream_id
                     AND e.sequence=j.source_stream_sequence
                    JOIN yaya_agent_turns t
                      ON t.tenant_id=j.tenant_id AND t.event_id=j.source_event_id
                    LEFT JOIN yaya_learner_models m
                      ON m.tenant_id=j.tenant_id AND m.learner_id=j.learner_id
                    WHERE j.tenant_id=%s AND j.job_id=%s AND j.state='LEASED'
                      AND j.worker_id=%s AND j.lease_id=%s AND j.fencing_token=%s
                      AND j.lease_expires_at>clock_timestamp()
                    """,
                    (
                        lease.tenant_id,
                        lease.job_id,
                        lease.fence.worker_id,
                        lease.fence.lease_id,
                        lease.fence.fencing_token,
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise LearnerProjectionFenceLost()
                evidence_cursor = await connection.execute(
                    """
                    SELECT evidence_id,evidence_sha256
                    FROM yaya_learner_projection_job_evidence
                    WHERE tenant_id=%s AND job_id=%s
                    ORDER BY ordinal
                    """,
                    (lease.tenant_id, lease.job_id),
                )
                evidence_rows = list(await evidence_cursor.fetchall())
        except LearnerProjectionFenceLost:
            raise
        except psycopg.Error as error:
            raise LearnerProjectionWorkerError(
                "DEPENDENCY_UNAVAILABLE",
                "PostgreSQL could not load the fenced learner projection",
                retryable=True,
            ) from error
        try:
            event = decode_as(row["event_json"], RuntimeEvent)
            durable_event = decode_as(row["durable_event_json"], RuntimeEvent)
            context = decode_as(row["operation_context_json"], OperationContext)
            self._validate_projection_identity(
                lease,
                row,
                event,
                durable_event,
                context,
                evidence_rows,
            )
        except LearnerProjectionWorkerError:
            raise
        except (TypeError, ValueError, KeyError) as error:
            raise LearnerProjectionWorkerError(
                "INVARIANT_VIOLATION",
                "Learner projection Job contains invalid persisted contract data",
                retryable=False,
            ) from error
        return _ProjectionInput(event=event, context=context)

    def _validate_projection_identity(
        self,
        lease: LearnerProjectionLease,
        row: Mapping[str, object],
        event: RuntimeEvent,
        durable_event: RuntimeEvent,
        context: OperationContext,
        evidence_rows: Sequence[Mapping[str, object]],
    ) -> None:
        payload = event.payload
        actor = payload["actor"]
        evidence = payload["evidence_refs"]
        if not isinstance(actor, Mapping):
            raise ValueError("learner inference actor is not an object")
        if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes, bytearray)):
            raise ValueError("learner inference evidence_refs is not an array")
        evidence_items = cast(Sequence[object], evidence)
        stored_event = cast(Mapping[str, object], encode(event))
        turn_record = row["turn_record_json"]
        if not isinstance(turn_record, Mapping):
            raise ValueError("source AgentTurn is not committed")
        evidence_identity: list[tuple[object, object]] = []
        for item_value in evidence_items:
            if not isinstance(item_value, Mapping):
                raise ValueError("learner inference EvidenceRef is not an object")
            item = cast(Mapping[str, object], item_value)
            evidence_identity.append((item.get("evidence_id"), item.get("sha256")))
        durable_evidence = [
            (item["evidence_id"], item["evidence_sha256"]) for item in evidence_rows
        ]
        event_hash = internal_record_sha256(stored_event)
        turn_hash = agent_turn_commit_sha256(cast(Mapping[str, object], turn_record))
        expected_stream = f"learner:{lease.learner_id}"
        if (
            event != durable_event
            or event.event_type is not _PROJECTION_SOURCE_EVENT
            or row["durable_event_type"] != _PROJECTION_SOURCE_EVENT.value
            or event.event_id != lease.event_id
            or event.event_id != row["event_id"]
            or event.stream_id != expected_stream
            or event.stream_id != row["source_stream_id"]
            or event.sequence != lease.source_stream_sequence
            or event.sequence != row["source_stream_sequence"]
            or event.command_id != row["command_id"]
            or event.command_id != context.command_id
            or event.trace_id != context.trace_id
            or event.correlation_id != context.correlation_id
            or event.causation_id != row["source_event_id"]
            or context.causation_id != row["source_event_id"]
            or event.content_ref != context.content_ref
            or event.content_ref.content_hash != row["content_hash"]
            or plain(payload["actor"]) != plain(context.actor)
            or payload["learner_id"] != lease.learner_id
            or payload["learner_id"] != row["learner_id"]
            or payload["learner_id"] != context.actor.actor_id
            or payload["session_id"] != row["session_id"]
            or payload["turn_id"] != row["turn_id"]
            or payload["command_id"] != row["command_id"]
            or payload["run_id"] != row["run_id"]
            or payload["source_event_id"] != row["source_event_id"]
            or payload["source_event_sha256"] != row["source_event_sha256"]
            or payload["source_event_sha256"] != row["durable_source_event_sha256"]
            or row["durable_source_actor_id"] != context.actor.actor_id
            or row["durable_source_content_hash"] != context.content_ref.content_hash
            or payload["turn_commit_sha256"] != row["turn_commit_sha256"]
            or payload["task_id"] != row["task_id"]
            or payload["teaching_spec_version"] != row["teaching_spec_version"]
            or payload["role"] != row["role"]
            or payload["inference_sha256"] != row["inference_sha256"]
            or event_hash != row["event_sha256"]
            or turn_hash != row["turn_commit_sha256"]
            or evidence_identity != durable_evidence
            or row["current_learner_revision"] is not None
            and (
                row["model_actor_id"] != context.actor.actor_id
                or row["model_content_hash"] != context.content_ref.content_hash
            )
            or row["current_learner_revision"] is None
            and (lease.expected_learner_revision != 0 or lease.source_stream_sequence != 1)
        ):
            raise LearnerProjectionWorkerError(
                "INVARIANT_VIOLATION",
                "Learner projection Job identity, hash, authority or sequence drifted",
                retryable=False,
            )

    async def _success_graph_state(
        self,
        lease: LearnerProjectionLease,
        expected_update: LearnerUpdate | None = None,
    ) -> _DurableGraphState:
        """Reconcile the complete immutable success graph, not its receipt header."""

        try:
            async with self._database.transaction() as connection:
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"learner:{lease.tenant_id}:{lease.learner_id}",),
                )
                job_cursor = await connection.execute(
                    """
                    SELECT * FROM yaya_learner_projection_jobs
                    WHERE tenant_id=%s AND job_id=%s
                    """,
                    (lease.tenant_id, lease.job_id),
                )
                job = await job_cursor.fetchone()
                if job is None:
                    return _DurableGraphState.MISSING
                source_cursor = await connection.execute(
                    """
                    SELECT * FROM yaya_events
                    WHERE tenant_id=%s AND event_id=%s
                    """,
                    (lease.tenant_id, job["event_id"]),
                )
                canonical_source_row = await source_cursor.fetchone()
                receipt_cursor = await connection.execute(
                    """
                    SELECT * FROM yaya_learner_projection_receipts
                    WHERE tenant_id=%s AND job_id=%s
                    """,
                    (lease.tenant_id, lease.job_id),
                )
                receipt = await receipt_cursor.fetchone()
                model_cursor = await connection.execute(
                    """
                    SELECT * FROM yaya_learner_models
                    WHERE tenant_id=%s AND learner_id=%s
                    """,
                    (lease.tenant_id, lease.learner_id),
                )
                model = await model_cursor.fetchone()
                if receipt is None:
                    model_applied = bool(
                        model is not None
                        and cast(int, model["projected_through_sequence"])
                        >= lease.source_stream_sequence
                    )
                    if job["state"] == "SUCCEEDED" or model_applied:
                        return _DurableGraphState.CORRUPT
                    return _DurableGraphState.MISSING
                derived_cursor = await connection.execute(
                    """
                    SELECT * FROM yaya_events
                    WHERE tenant_id=%s AND event_id=%s
                    """,
                    (lease.tenant_id, receipt["model_updated_event_id"]),
                )
                derived = await derived_cursor.fetchone()
                outbox_cursor = await connection.execute(
                    """
                    SELECT * FROM yaya_outbox
                    WHERE tenant_id=%s AND message_id=%s
                    """,
                    (lease.tenant_id, receipt["outbox_message_id"]),
                )
                outbox = await outbox_cursor.fetchone()
        except psycopg.Error as error:
            raise LearnerProjectionWorkerError(
                "DEPENDENCY_UNAVAILABLE",
                "PostgreSQL could not reconcile learner projection success",
                retryable=True,
            ) from error

        if (
            job["state"] != "SUCCEEDED"
            or model is None
            or derived is None
            or outbox is None
            or canonical_source_row is None
        ):
            return _DurableGraphState.CORRUPT
        try:
            source_event = decode_as(job["event_json"], RuntimeEvent)
            canonical_source_event = decode_as(canonical_source_row["event_json"], RuntimeEvent)
            context = decode_as(job["operation_context_json"], OperationContext)
            update = decode_as(receipt["update_json"], LearnerUpdate)
            snapshot = decode_as(model["snapshot_json"], LearnerModelSnapshot)
            model_context = decode_as(
                model["request_context_json"],
                OperationContext,
            )
            validate_persisted_learner_snapshot(
                snapshot,
                learner_id=lease.learner_id,
                revision=model["revision"],
                projected_through_sequence=model["projected_through_sequence"],
                model_version=model["projection_policy_version"],
                snapshot_sha256=model["snapshot_sha256"],
                updated_at=model["updated_at"],
            )
            derived_event = decode_as(derived["event_json"], RuntimeEvent)
            identity_seed = {
                "kind": "learner_model_updated_v1",
                "tenant_id": lease.tenant_id,
                "job_id": lease.job_id,
                "event_id": lease.event_id,
                "event_sha256": job["event_sha256"],
            }
            expected_event_id = self._identifier("evt_learner_model", identity_seed)
            expected_outbox_id = self._identifier("learner_model_msg", identity_seed)
            expected_derived = RuntimeEvent(
                event_id=expected_event_id,
                event_type=RuntimeEventType.LEARNER_MODEL_UPDATED,
                event_version=1,
                stream_id=f"learner-model:{lease.learner_id}",
                sequence=cast(int, derived["sequence"]),
                occurred_at=cast(datetime, receipt["projected_at"]),
                producer="learner_projection_worker",
                trace_id=source_event.trace_id,
                command_id=source_event.command_id,
                correlation_id=source_event.correlation_id,
                causation_id=source_event.event_id,
                content_ref=source_event.content_ref,
                payload={
                    "learner_id": lease.learner_id,
                    "previous_revision": update.previous_revision,
                    "learner_revision": update.revision,
                    "projected_through_sequence": lease.source_stream_sequence,
                    "changed_competency_ids": list(update.changed_competency_ids),
                    "updated_at": plain(update.updated_at),
                    "evidence_refs": [self._evidence_wire(item) for item in update.evidence_refs],
                },
            )
            derived_wire = cast(Mapping[str, object], plain(expected_derived))
            receipt_hash = self._receipt_sha256(receipt, update)
            snapshot_hash = internal_record_sha256(snapshot)
        except (KeyError, TypeError, ValueError):
            return _DurableGraphState.CORRUPT

        authority_matches = (
            context.actor.tenant_id == lease.tenant_id
            and context.actor.actor_id == lease.learner_id
            and context.content_ref.content_hash == job["content_hash"]
            and model_context.actor.tenant_id == context.actor.tenant_id
            and model_context.actor.actor_id == context.actor.actor_id
            and model_context.actor.actor_type == context.actor.actor_type
            and model_context.content_ref == context.content_ref
            and source_event.event_id == lease.event_id
            and source_event.stream_id == job["source_stream_id"]
            and source_event.sequence == lease.source_stream_sequence
            and source_event.content_ref == context.content_ref
            and source_event.command_id == context.command_id
            and source_event.trace_id == context.trace_id
            and source_event.correlation_id == context.correlation_id
        )
        source_graph_matches = (
            canonical_source_event == source_event
            and canonical_source_row["event_id"] == source_event.event_id
            and canonical_source_row["stream_id"] == source_event.stream_id
            and canonical_source_row["sequence"] == source_event.sequence
            and canonical_source_row["event_type"] == plain(source_event.event_type)
            and canonical_source_row["occurred_at"] == source_event.occurred_at
            and internal_record_sha256(canonical_source_event) == job["event_sha256"]
            and learner_inference_sha256(canonical_source_event.payload) == job["inference_sha256"]
        )
        receipt_matches = (
            receipt["event_id"] == job["event_id"] == lease.event_id
            and receipt["job_id"] == lease.job_id
            and receipt["source_event_id"] == job["source_event_id"]
            and receipt["learner_id"] == job["learner_id"] == lease.learner_id
            and receipt["actor_id"] == job["actor_id"] == context.actor.actor_id
            and receipt["content_hash"] == job["content_hash"] == context.content_ref.content_hash
            and receipt["source_stream_id"] == job["source_stream_id"] == source_event.stream_id
            and receipt["source_stream_sequence"]
            == job["source_stream_sequence"]
            == lease.source_stream_sequence
            and receipt["event_sha256"] == job["event_sha256"]
            and receipt["inference_sha256"] == job["inference_sha256"]
            and receipt["previous_learner_revision"] == update.previous_revision
            and receipt["learner_revision"] == update.revision
            and receipt["model_version"] == update.model_version
            and receipt["model_updated_event_id"] == expected_event_id
            and receipt["outbox_message_id"] == expected_outbox_id
            and update.learner_id == lease.learner_id
            and update.previous_revision == lease.expected_learner_revision
            and update.revision == lease.expected_learner_revision + 1
            and receipt["receipt_sha256"] == receipt_hash
            and job["succeeded_at"] == receipt["projected_at"]
            and (expected_update is None or expected_update == update)
        )
        model_matches = (
            model["learner_id"] == snapshot.learner_id == lease.learner_id
            and model["actor_id"] == context.actor.actor_id
            and model["content_hash"] == context.content_ref.content_hash
            and model["revision"] == snapshot.revision
            and model["projected_through_sequence"] == snapshot.projected_through_sequence
            and model["projection_policy_version"] == snapshot.model_version
            and model["updated_at"] == snapshot.updated_at
            and model["snapshot_sha256"] == snapshot_hash
            and snapshot.revision >= update.revision
            and snapshot.projected_through_sequence >= lease.source_stream_sequence
            and snapshot.revision - update.revision
            == snapshot.projected_through_sequence - lease.source_stream_sequence
            and (
                snapshot.revision != update.revision
                or snapshot.projected_through_sequence != lease.source_stream_sequence
                or snapshot.model_version != update.model_version
                or (
                    receipt["snapshot_sha256"] == snapshot_hash
                    and snapshot.updated_at == update.updated_at
                )
            )
        )
        derived_matches = (
            derived_event == expected_derived
            and derived["event_id"] == expected_derived.event_id
            and derived["stream_id"] == expected_derived.stream_id
            and derived["sequence"] == expected_derived.sequence
            and derived["event_type"] == RuntimeEventType.LEARNER_MODEL_UPDATED.value
            and derived["occurred_at"] == expected_derived.occurred_at
        )
        outbox_matches = (
            outbox["message_id"] == receipt["outbox_message_id"]
            and outbox["destination"] == "learner_model_events"
            and outbox["idempotency_key"] == f"learner-model:{source_event.event_id}"
            and outbox["payload_sha256"] == internal_record_sha256(derived_wire)
            and outbox["message_json"] == derived_wire
            and outbox["created_at"] == receipt["projected_at"]
        )
        if (
            authority_matches
            and source_graph_matches
            and receipt_matches
            and model_matches
            and derived_matches
            and outbox_matches
        ):
            return _DurableGraphState.MATCH
        return _DurableGraphState.CORRUPT

    async def _failure_graph_state(
        self,
        lease: LearnerProjectionLease,
        error: ContractError,
    ) -> _DurableGraphState:
        """Reconcile the failure record, derived event and Outbox as one graph."""

        try:
            async with self._database.transaction() as connection:
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"learner:{lease.tenant_id}:{lease.learner_id}",),
                )
                job_cursor = await connection.execute(
                    """
                    SELECT * FROM yaya_learner_projection_jobs
                    WHERE tenant_id=%s AND job_id=%s
                    """,
                    (lease.tenant_id, lease.job_id),
                )
                job = await job_cursor.fetchone()
                if job is None:
                    return _DurableGraphState.MISSING
                source_cursor = await connection.execute(
                    """
                    SELECT * FROM yaya_events
                    WHERE tenant_id=%s AND event_id=%s
                    """,
                    (lease.tenant_id, job["event_id"]),
                )
                canonical_source_row = await source_cursor.fetchone()
                failure_cursor = await connection.execute(
                    """
                    SELECT * FROM yaya_learner_projection_failures
                    WHERE tenant_id=%s AND job_id=%s AND attempt=%s
                    """,
                    (lease.tenant_id, lease.job_id, job["attempt"]),
                )
                failure = await failure_cursor.fetchone()
                receipt_cursor = await connection.execute(
                    """
                    SELECT event_id FROM yaya_learner_projection_receipts
                    WHERE tenant_id=%s AND job_id=%s
                    """,
                    (lease.tenant_id, lease.job_id),
                )
                receipt = await receipt_cursor.fetchone()
                if failure is None:
                    if job["state"] == "FAILED" or receipt is not None:
                        return _DurableGraphState.CORRUPT
                    return _DurableGraphState.MISSING
                if failure["failure_event_id"] is None or failure["outbox_message_id"] is None:
                    return _DurableGraphState.CORRUPT
                event_cursor = await connection.execute(
                    """
                    SELECT * FROM yaya_events
                    WHERE tenant_id=%s AND event_id=%s
                    """,
                    (lease.tenant_id, failure["failure_event_id"]),
                )
                failure_event_row = await event_cursor.fetchone()
                outbox_cursor = await connection.execute(
                    """
                    SELECT * FROM yaya_outbox
                    WHERE tenant_id=%s AND message_id=%s
                    """,
                    (lease.tenant_id, failure["outbox_message_id"]),
                )
                outbox = await outbox_cursor.fetchone()
        except psycopg.Error as caught:
            raise LearnerProjectionWorkerError(
                "DEPENDENCY_UNAVAILABLE",
                "PostgreSQL could not reconcile learner projection failure",
                retryable=True,
            ) from caught

        if (
            job["state"] != "FAILED"
            or receipt is not None
            or failure_event_row is None
            or outbox is None
            or canonical_source_row is None
        ):
            return _DurableGraphState.CORRUPT
        try:
            source_event = decode_as(job["event_json"], RuntimeEvent)
            canonical_source_event = decode_as(canonical_source_row["event_json"], RuntimeEvent)
            context = decode_as(job["operation_context_json"], OperationContext)
            failure_event = decode_as(failure_event_row["event_json"], RuntimeEvent)
            error_wire = self._error_wire(error)
            error_sha256 = internal_record_sha256(error_wire)
            identity_seed = {
                "kind": "learner_projection_failed_v1",
                "tenant_id": lease.tenant_id,
                "job_id": lease.job_id,
                "event_id": lease.event_id,
                "attempt": lease.fence.fencing_token,
                "error_sha256": error_sha256,
            }
            expected_failure_id = self._identifier("learner_failure", identity_seed)
            expected_event_id = self._identifier("evt_learner_failed", identity_seed)
            expected_outbox_id = self._identifier("learner_failed_msg", identity_seed)
            expected_event = RuntimeEvent(
                event_id=expected_event_id,
                event_type=RuntimeEventType.LEARNER_PROJECTION_FAILED,
                event_version=1,
                stream_id=f"learner-model:{lease.learner_id}",
                sequence=cast(int, failure_event_row["sequence"]),
                occurred_at=cast(datetime, failure["recorded_at"]),
                producer="learner_projection_worker",
                trace_id=source_event.trace_id,
                command_id=source_event.command_id,
                correlation_id=source_event.correlation_id,
                causation_id=source_event.event_id,
                content_ref=source_event.content_ref,
                payload={
                    "learner_id": lease.learner_id,
                    "source_event_id": source_event.event_id,
                    "failed_at": plain(failure["recorded_at"]),
                    "error": error_wire,
                },
            )
            event_wire = cast(Mapping[str, object], plain(expected_event))
        except (KeyError, TypeError, ValueError):
            return _DurableGraphState.CORRUPT
        failed_at = job["failed_at"]
        recorded_at = failure["recorded_at"]
        if not isinstance(failed_at, datetime) or not isinstance(recorded_at, datetime):
            return _DurableGraphState.CORRUPT

        failure_matches = (
            failure["event_id"] == job["event_id"] == lease.event_id
            and failure["job_id"] == lease.job_id
            and failure["source_event_id"] == job["source_event_id"]
            and failure["learner_id"] == job["learner_id"] == lease.learner_id
            and failure["actor_id"] == job["actor_id"] == context.actor.actor_id
            and failure["content_hash"] == job["content_hash"] == context.content_ref.content_hash
            and failure["source_stream_id"] == job["source_stream_id"] == source_event.stream_id
            and failure["source_stream_sequence"]
            == job["source_stream_sequence"]
            == lease.source_stream_sequence
            and failure["attempt"] == job["attempt"] == lease.fence.fencing_token
            and failure["fencing_token"] == lease.fence.fencing_token
            and failure["classification"] == "PERMANENT"
            and failure["failure_id"] == expected_failure_id
            and failure["failure_event_id"] == expected_event_id
            and failure["outbox_message_id"] == expected_outbox_id
            and failure["error_code"] == job["last_error_code"] == error.code
            and failure["error_json"] == job["last_error_json"] == error_wire
            and failure["error_sha256"] == error_sha256
            and job["failed_at"] == failure["recorded_at"]
        )
        authority_matches = (
            context.actor.tenant_id == lease.tenant_id
            and context.actor.actor_id == lease.learner_id
            and source_event.event_id == lease.event_id
            and source_event.command_id == context.command_id
            and source_event.trace_id == context.trace_id
            and source_event.correlation_id == context.correlation_id
            and source_event.content_ref == context.content_ref
        )
        source_graph_matches = (
            canonical_source_event == source_event
            and canonical_source_row["event_id"] == source_event.event_id
            and canonical_source_row["stream_id"] == source_event.stream_id
            and canonical_source_row["sequence"] == source_event.sequence
            and canonical_source_row["event_type"] == plain(source_event.event_type)
            and canonical_source_row["occurred_at"] == source_event.occurred_at
            and internal_record_sha256(canonical_source_event) == job["event_sha256"]
            and learner_inference_sha256(canonical_source_event.payload) == job["inference_sha256"]
        )
        event_matches = (
            failure_event == expected_event
            and failure_event_row["event_id"] == expected_event.event_id
            and failure_event_row["stream_id"] == expected_event.stream_id
            and failure_event_row["sequence"] == expected_event.sequence
            and failure_event_row["event_type"] == RuntimeEventType.LEARNER_PROJECTION_FAILED.value
            and failure_event_row["occurred_at"] == expected_event.occurred_at
        )
        outbox_matches = (
            outbox["message_id"] == failure["outbox_message_id"]
            and outbox["destination"] == "learner_model_events"
            and outbox["idempotency_key"] == f"learner-projection-failed:{source_event.event_id}"
            and outbox["payload_sha256"] == internal_record_sha256(event_wire)
            and outbox["message_json"] == event_wire
            and outbox["created_at"] == failure["recorded_at"]
        )
        if (
            failure_matches
            and authority_matches
            and source_graph_matches
            and event_matches
            and outbox_matches
        ):
            return _DurableGraphState.MATCH
        return _DurableGraphState.CORRUPT

    async def _quarantine_graph_state(
        self,
        lease: LearnerProjectionLease,
        error_json: Mapping[str, object],
    ) -> _DurableGraphState:
        """Verify a terminal quarantine without requiring derived event delivery."""

        try:
            async with self._database.transaction() as connection:
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"learner:{lease.tenant_id}:{lease.learner_id}",),
                )
                job_cursor = await connection.execute(
                    """
                    SELECT * FROM yaya_learner_projection_jobs
                    WHERE tenant_id=%s AND job_id=%s
                    """,
                    (lease.tenant_id, lease.job_id),
                )
                job = await job_cursor.fetchone()
                if job is None:
                    return _DurableGraphState.MISSING
                source_cursor = await connection.execute(
                    """
                    SELECT * FROM yaya_events
                    WHERE tenant_id=%s AND event_id=%s
                    """,
                    (lease.tenant_id, job["event_id"]),
                )
                canonical_source_row = await source_cursor.fetchone()
                failure_cursor = await connection.execute(
                    """
                    SELECT * FROM yaya_learner_projection_failures
                    WHERE tenant_id=%s AND job_id=%s AND attempt=%s
                    """,
                    (lease.tenant_id, lease.job_id, job["attempt"]),
                )
                failure = await failure_cursor.fetchone()
                receipt_cursor = await connection.execute(
                    """
                    SELECT event_id FROM yaya_learner_projection_receipts
                    WHERE tenant_id=%s AND job_id=%s
                    """,
                    (lease.tenant_id, lease.job_id),
                )
                receipt = await receipt_cursor.fetchone()
        except psycopg.Error as caught:
            raise LearnerProjectionWorkerError(
                "DEPENDENCY_UNAVAILABLE",
                "PostgreSQL could not reconcile learner projection quarantine",
                retryable=True,
            ) from caught

        if (
            job["state"] != "FAILED"
            or failure is None
            or receipt is not None
            or canonical_source_row is None
        ):
            return _DurableGraphState.CORRUPT
        try:
            canonical_error = self._quarantine_error_from_wire(error_json)
            source_event = decode_as(job["event_json"], RuntimeEvent)
            canonical_source_event = decode_as(
                canonical_source_row["event_json"],
                RuntimeEvent,
            )
            context = decode_as(job["operation_context_json"], OperationContext)
            error_sha256 = canonical_json_sha256(canonical_error)
        except (KeyError, TypeError, ValueError):
            return _DurableGraphState.CORRUPT
        failed_at = job["failed_at"]
        recorded_at = failure["recorded_at"]
        if not isinstance(failed_at, datetime) or not isinstance(recorded_at, datetime):
            return _DurableGraphState.CORRUPT

        failure_matches = (
            failure["event_id"] == job["event_id"] == lease.event_id
            and failure["job_id"] == lease.job_id
            and failure["source_event_id"] == job["source_event_id"]
            and failure["learner_id"] == job["learner_id"] == lease.learner_id
            and failure["actor_id"] == job["actor_id"] == context.actor.actor_id
            and failure["content_hash"] == job["content_hash"] == context.content_ref.content_hash
            and failure["source_stream_id"] == job["source_stream_id"] == source_event.stream_id
            and failure["source_stream_sequence"]
            == job["source_stream_sequence"]
            == lease.source_stream_sequence
            and failure["attempt"] == job["attempt"] == lease.fence.fencing_token
            and failure["fencing_token"] == job["fencing_token"] == lease.fence.fencing_token
            and failure["classification"] == "QUARANTINED"
            and failure["failure_event_id"] is None
            and failure["outbox_message_id"] is None
            and failure["error_code"] == job["last_error_code"] == canonical_error["code"]
            and failure["error_json"] == job["last_error_json"] == canonical_error
            and failure["error_sha256"] == error_sha256
            and failed_at >= recorded_at
        )
        authority_matches = (
            context.actor.tenant_id == lease.tenant_id
            and context.actor.actor_id == lease.learner_id
            and source_event.event_id == lease.event_id
            and source_event.stream_id == job["source_stream_id"]
            and source_event.sequence == lease.source_stream_sequence
            and source_event.command_id == context.command_id
            and source_event.trace_id == context.trace_id
            and source_event.correlation_id == context.correlation_id
            and source_event.content_ref == context.content_ref
        )
        source_graph_matches = (
            canonical_source_event == source_event
            and canonical_source_row["event_id"] == source_event.event_id
            and canonical_source_row["stream_id"] == source_event.stream_id
            and canonical_source_row["sequence"] == source_event.sequence
            and canonical_source_row["event_type"] == plain(source_event.event_type)
            and canonical_source_row["occurred_at"] == source_event.occurred_at
            and internal_record_sha256(canonical_source_event) == job["event_sha256"]
            and learner_inference_sha256(canonical_source_event.payload) == job["inference_sha256"]
        )
        if failure_matches and authority_matches and source_graph_matches:
            return _DurableGraphState.MATCH
        return _DurableGraphState.CORRUPT

    @staticmethod
    def _receipt_sha256(
        receipt: Mapping[str, object],
        update: LearnerUpdate,
    ) -> str:
        return internal_record_sha256(
            {
                "tenant_id": receipt["tenant_id"],
                "event_id": receipt["event_id"],
                "job_id": receipt["job_id"],
                "source_event_id": receipt["source_event_id"],
                "learner_id": receipt["learner_id"],
                "source_stream_sequence": receipt["source_stream_sequence"],
                "event_sha256": receipt["event_sha256"],
                "inference_sha256": receipt["inference_sha256"],
                "previous_learner_revision": receipt["previous_learner_revision"],
                "learner_revision": receipt["learner_revision"],
                "model_version": receipt["model_version"],
                "snapshot_sha256": receipt["snapshot_sha256"],
                "model_updated_event_id": receipt["model_updated_event_id"],
                "outbox_message_id": receipt["outbox_message_id"],
                "update": plain(update),
                "projected_at": plain(receipt["projected_at"]),
            }
        )

    @staticmethod
    def _evidence_wire(evidence: EvidenceRef) -> dict[str, object]:
        record: dict[str, object] = {
            "evidence_id": evidence.evidence_id,
            "evidence_type": evidence.evidence_type.value,
            "created_at": plain(evidence.created_at),
        }
        if evidence.sha256 is not None:
            record["sha256"] = evidence.sha256
        if evidence.uri is not None:
            record["uri"] = evidence.uri
        return record

    @staticmethod
    def _contract_error_from_wire(value: object) -> ContractError:
        if not isinstance(value, Mapping):
            raise ValueError("terminal failure error must be an object")
        wire = cast(Mapping[str, object], value)
        required = {"code", "category", "retryable", "user_message_key", "stage"}
        allowed = required | {"message", "details", "evidence_ids"}
        if not required.issubset(wire) or not set(wire).issubset(allowed):
            raise ValueError("terminal failure error fields are not closed")
        raw_details = wire.get("details", {})
        if not isinstance(raw_details, Mapping):
            raise ValueError("terminal failure error details must be an object")
        raw_evidence_ids = wire.get("evidence_ids", ())
        if not isinstance(raw_evidence_ids, Sequence) or isinstance(
            raw_evidence_ids,
            (str, bytes, bytearray),
        ):
            raise ValueError("terminal failure evidence_ids must be an array")
        evidence_ids = cast(Sequence[object], raw_evidence_ids)
        message = wire.get("message")
        if message is not None and not isinstance(message, str):
            raise ValueError("terminal failure message must be a string")
        return ContractError(
            code=cast(str, wire["code"]),
            category=ErrorCategory(cast(str, wire["category"])),
            retryable=cast(bool, wire["retryable"]),
            user_message_key=cast(str, wire["user_message_key"]),
            stage=cast(str, wire["stage"]),
            message=message,
            details=cast(FrozenJsonObject, raw_details),
            evidence_ids=tuple(cast(str, item) for item in evidence_ids),
        )

    @staticmethod
    def _quarantine_error_from_wire(value: object) -> dict[str, object]:
        if not isinstance(value, Mapping):
            raise ValueError("terminal quarantine error must be an object")
        wire = cast(Mapping[str, object], value)
        if set(wire) != {
            "code",
            "cause",
            "redacted",
        }:
            raise ValueError("terminal quarantine error fields are not closed")
        code = wire["code"]
        cause = wire["cause"]
        if (
            not isinstance(code, str)
            or not 1 <= len(code) <= 96
            or not isinstance(cause, str)
            or not 1 <= len(cause) <= 128
            or wire["redacted"] is not True
        ):
            raise ValueError("terminal quarantine error is not canonical")
        return {"code": code, "cause": cause, "redacted": True}

    @staticmethod
    def _error_wire(error: ContractError) -> dict[str, object]:
        record: dict[str, object] = {
            "code": error.code,
            "category": error.category.value,
            "retryable": error.retryable,
            "user_message_key": error.user_message_key,
            "stage": error.stage,
        }
        if error.message is not None:
            record["message"] = error.message
        if error.details:
            record["details"] = dict(error.details)
        if error.evidence_ids:
            record["evidence_ids"] = list(error.evidence_ids)
        return record

    @staticmethod
    def _identifier(prefix: str, seed: Mapping[str, object]) -> str:
        return f"{prefix}_{canonical_json_sha256(seed)[:32]}"

    async def _record_graph_corruption(
        self,
        lease: LearnerProjectionLease,
        cause: str,
    ) -> None:
        """Fence a live Job or surface terminal corruption without mutating it.

        Projector transactions clear their lease fields when terminalizing, so
        no later worker write can truthfully satisfy the live lease predicate.
        A terminal corrupt graph is therefore surfaced as fatal and left
        byte-for-byte intact for incident response.  Only a still-live lease
        may append a QUARANTINED failure and transition its Job.
        """

        connection = await self._database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT state,attempt,fencing_token
                FROM yaya_learner_projection_jobs
                WHERE tenant_id=%s AND job_id=%s
                """,
                (lease.tenant_id, lease.job_id),
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if (
            row is None
            or row["attempt"] != lease.fence.fencing_token
            or row["fencing_token"] != lease.fence.fencing_token
        ):
            raise LearnerProjectionFenceLost()
        if row["state"] == "LEASED":
            await self._quarantine(
                lease,
                "INVARIANT_VIOLATION",
                self._error_record("INVARIANT_VIOLATION", cause),
            )
            return
        if row["state"] in {"SUCCEEDED", "FAILED"}:
            raise LearnerProjectionDurableGraphCorrupt(cause)
        raise LearnerProjectionFenceLost()

    async def _read_checkpoint(self, lease: LearnerProjectionLease) -> tuple[int, int]:
        try:
            connection = await self._database.connect(autocommit=True)
            try:
                cursor = await connection.execute(
                    """
                    SELECT revision,projected_through_sequence
                    FROM yaya_learner_models
                    WHERE tenant_id=%s AND learner_id=%s
                    """,
                    (lease.tenant_id, lease.learner_id),
                )
                row = await cursor.fetchone()
            finally:
                await connection.close()
        except psycopg.Error as error:
            raise LearnerProjectionWorkerError(
                "DEPENDENCY_UNAVAILABLE",
                "PostgreSQL could not reread learner projection CAS",
                retryable=True,
            ) from error
        if row is None:
            return (0, 0)
        return (
            cast(int, row["revision"]),
            cast(int, row["projected_through_sequence"]),
        )

    async def _release_for_retry(
        self,
        lease: LearnerProjectionLease,
        error_code: str,
        error_json: Mapping[str, object],
    ) -> None:
        failure_id = f"learner_failure_{uuid.uuid4().hex}"
        error_sha256 = canonical_json_sha256(error_json)
        try:
            async with self._database.transaction() as connection:
                inserted = await connection.execute(
                    """
                    INSERT INTO yaya_learner_projection_failures(
                        tenant_id,failure_id,job_id,event_id,source_event_id,
                        learner_id,actor_id,content_hash,source_stream_id,
                        source_stream_sequence,attempt,fencing_token,
                        classification,error_code,error_json,error_sha256
                    )
                    SELECT tenant_id,%s,job_id,event_id,source_event_id,
                           learner_id,actor_id,content_hash,source_stream_id,
                           source_stream_sequence,attempt,fencing_token,
                           'RETRYABLE',%s,%s,%s
                    FROM yaya_learner_projection_jobs
                    WHERE tenant_id=%s AND job_id=%s AND state='LEASED'
                      AND worker_id=%s AND lease_id=%s AND fencing_token=%s
                      AND lease_expires_at>clock_timestamp()
                    RETURNING failure_id
                    """,
                    (
                        failure_id,
                        error_code[:96],
                        Jsonb(error_json),
                        error_sha256,
                        lease.tenant_id,
                        lease.job_id,
                        lease.fence.worker_id,
                        lease.fence.lease_id,
                        lease.fence.fencing_token,
                    ),
                )
                if await inserted.fetchone() is None:
                    raise LearnerProjectionFenceLost()
                updated = await connection.execute(
                    """
                    UPDATE yaya_learner_projection_jobs
                    SET state='READY',worker_id=NULL,lease_id=NULL,
                        claimed_at=NULL,heartbeat_at=NULL,lease_expires_at=NULL,
                        available_at=clock_timestamp()+%s*interval '1 second',
                        last_error_code=%s,last_error_json=%s,
                        updated_at=clock_timestamp()
                    WHERE tenant_id=%s AND job_id=%s AND state='LEASED'
                      AND worker_id=%s AND lease_id=%s AND fencing_token=%s
                      AND lease_expires_at>clock_timestamp()
                    """,
                    (
                        self._retry_delay_seconds,
                        error_code[:96],
                        Jsonb(error_json),
                        lease.tenant_id,
                        lease.job_id,
                        lease.fence.worker_id,
                        lease.fence.lease_id,
                        lease.fence.fencing_token,
                    ),
                )
                if updated.rowcount != 1:
                    raise LearnerProjectionFenceLost()
        except LearnerProjectionFenceLost:
            raise
        except psycopg.Error:
            raise

    async def _quarantine(
        self,
        lease: LearnerProjectionLease,
        error_code: str,
        error_json: Mapping[str, object],
    ) -> None:
        canonical_error = self._quarantine_error_from_wire(error_json)
        if canonical_error["code"] != error_code[:96]:
            raise LearnerProjectionWorkerError(
                "INVARIANT_VIOLATION",
                "Quarantine error code and canonical error record differ",
                retryable=False,
            )
        failure_id = f"learner_failure_{uuid.uuid4().hex}"
        error_sha256 = canonical_json_sha256(canonical_error)
        async with self._database.transaction() as connection:
            inserted = await connection.execute(
                """
                INSERT INTO yaya_learner_projection_failures(
                    tenant_id,failure_id,job_id,event_id,source_event_id,
                    learner_id,actor_id,content_hash,source_stream_id,
                    source_stream_sequence,attempt,fencing_token,
                    classification,error_code,error_json,error_sha256
                )
                SELECT tenant_id,%s,job_id,event_id,source_event_id,
                       learner_id,actor_id,content_hash,source_stream_id,
                       source_stream_sequence,attempt,fencing_token,
                       'QUARANTINED',%s,%s,%s
                FROM yaya_learner_projection_jobs
                WHERE tenant_id=%s AND job_id=%s AND state='LEASED'
                  AND worker_id=%s AND lease_id=%s AND fencing_token=%s
                  AND lease_expires_at>clock_timestamp()
                RETURNING failure_id
                """,
                (
                    failure_id,
                    error_code[:96],
                    Jsonb(canonical_error),
                    error_sha256,
                    lease.tenant_id,
                    lease.job_id,
                    lease.fence.worker_id,
                    lease.fence.lease_id,
                    lease.fence.fencing_token,
                ),
            )
            if await inserted.fetchone() is None:
                raise LearnerProjectionFenceLost()
            updated = await connection.execute(
                """
                UPDATE yaya_learner_projection_jobs
                SET state='FAILED',worker_id=NULL,lease_id=NULL,
                    claimed_at=NULL,heartbeat_at=NULL,lease_expires_at=NULL,
                    last_error_code=%s,last_error_json=%s,
                    failed_at=clock_timestamp(),updated_at=clock_timestamp()
                WHERE tenant_id=%s AND job_id=%s AND state='LEASED'
                  AND worker_id=%s AND lease_id=%s AND fencing_token=%s
                  AND lease_expires_at>clock_timestamp()
                """,
                (
                    error_code[:96],
                    Jsonb(canonical_error),
                    lease.tenant_id,
                    lease.job_id,
                    lease.fence.worker_id,
                    lease.fence.lease_id,
                    lease.fence.fencing_token,
                ),
            )
            if updated.rowcount != 1:
                raise LearnerProjectionFenceLost()
        reconciliation = await self._quarantine_graph_state(lease, canonical_error)
        if reconciliation is not _DurableGraphState.MATCH:
            raise LearnerProjectionDurableGraphCorrupt(f"QUARANTINE_GRAPH_{reconciliation.value}")
        await self._mark_terminal_audited(
            lease,
            "FAILED",
            "QUARANTINE",
        )

    @staticmethod
    def _validate_update(lease: LearnerProjectionLease, update: LearnerUpdate) -> None:
        if (
            update.learner_id != lease.learner_id
            or update.previous_revision != lease.expected_learner_revision
            or update.revision != lease.expected_learner_revision + 1
        ):
            raise LearnerProjectionWorkerError(
                "INVARIANT_VIOLATION",
                "Learner projector returned an update for a different CAS identity",
                retryable=False,
            )

    @staticmethod
    def _invariant_failure(cause: str) -> ContractError:
        return ContractError(
            code="INVARIANT_VIOLATION",
            category=ErrorCategory.INVARIANT,
            retryable=False,
            user_message_key="system.invariant_violation",
            stage="COMPLETE",
            message="Learner projection input or implementation violated a durable invariant.",
            details={"cause": cause[:128]},
        )

    @staticmethod
    def _error_record(code: str, cause: str) -> Mapping[str, object]:
        return {
            "code": code[:96],
            "cause": cause[:128],
            "redacted": True,
        }


__all__ = [
    "FencedLearnerProjectionPort",
    "LearnerProjectionDurableGraphCorrupt",
    "LearnerProjectionFence",
    "LearnerProjectionFenceLost",
    "LearnerProjectionLease",
    "LearnerProjectionWorker",
    "LearnerProjectionWorkerError",
]
