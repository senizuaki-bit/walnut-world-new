"""Atomic PostgreSQL CommandStorePort implementation."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    CommandCreateReceipt,
    CommandRecord,
    CommandTransition,
    CursorPage,
    Failure,
    NewCommand,
    OperationContext,
    Result,
    SkillRef,
    Success,
)
from yaya_agent_runtime import side_effect_execution_id, skill_invocation_request_sha256

from .audit import append_in_session, system_audit_record
from .models import (
    AgentSessionRow,
    AgentTurnRow,
    CommandRow,
    IdempotencyReceiptRow,
    JobStepReceiptRow,
    LearnerProjectionJobRow,
    RunRow,
    SkillActivationRow,
    SkillBuildRow,
    WorkflowJobRow,
    command_record_data,
    command_record_from_data,
    request_context_data,
)
from .workflow_jobs import (
    workflow_job_id,
    workflow_json_sha256,
    workflow_receipt_sha256,
    workflow_step_receipt_id,
)


class PostgresCommandStore:
    """Persists idempotency and revision/status CAS in PostgreSQL transactions."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def get(self, command_id: str, context: OperationContext) -> Result[CommandRecord]:
        async with self._sessions() as session:
            row = await session.scalar(
                select(CommandRow).where(
                    CommandRow.command_id == command_id,
                    CommandRow.tenant_id == context.actor.tenant_id,
                    CommandRow.actor_id == context.actor.actor_id,
                )
            )
            if row is None:
                return Failure(_not_found())
            record = await validated_command_record(session, row)
        return (
            Success(record)
            if record is not None
            else Failure(_invariant("READ", "command durable authority drifted"))
        )

    async def get_by_idempotency_key(
        self, operation: str, idempotency_key: str, context: OperationContext
    ) -> Result[CommandRecord]:
        async with self._sessions() as session:
            receipt = await session.scalar(
                select(IdempotencyReceiptRow).where(
                    IdempotencyReceiptRow.tenant_id == context.actor.tenant_id,
                    IdempotencyReceiptRow.actor_id == context.actor.actor_id,
                    IdempotencyReceiptRow.operation == operation,
                    IdempotencyReceiptRow.idempotency_key == idempotency_key,
                )
            )
            row = (
                await session.scalar(
                    select(CommandRow).where(
                        CommandRow.command_id == receipt.command_id,
                        CommandRow.tenant_id == context.actor.tenant_id,
                        CommandRow.actor_id == context.actor.actor_id,
                        CommandRow.command_type == operation,
                    )
                )
                if receipt
                else None
            )
            if row is None:
                return Failure(_not_found())
            record = await validated_command_record(session, row)
        return (
            Success(record)
            if record is not None
            else Failure(_invariant("READ", "command durable authority drifted"))
        )

    async def accept_once(
        self, command: NewCommand, context: OperationContext
    ) -> Result[CommandCreateReceipt]:
        async with self._sessions() as session, session.begin():
            return await self.accept_once_in_session(session, command, context)

    async def accept_once_in_session(
        self, session: AsyncSession, command: NewCommand, context: OperationContext
    ) -> Result[CommandCreateReceipt]:
        """Participate in a larger write transaction without committing it."""
        tenant_id, actor_id, operation, idempotency_key = command.idempotency_scope(context)
        database_now = await session.scalar(select(func.clock_timestamp()))
        if not isinstance(database_now, datetime) or database_now.tzinfo is None:
            return Failure(_invariant("ACCEPT", "PostgreSQL returned an invalid timestamp"))
        # OperationContext.requested_at is assigned by the Gateway host.  Keep
        # one logical floor when that host clock is slightly ahead of PostgreSQL
        # so Command, Session, Job, and their downstream projections cannot be
        # created before the request that caused them.
        accepted_at = max(
            database_now.astimezone(UTC),
            context.requested_at.astimezone(UTC),
        )
        record = command.initial_record(context, accepted_at)
        inserted = await session.scalar(
            insert(IdempotencyReceiptRow)
            .values(
                tenant_id=tenant_id,
                actor_id=actor_id,
                operation=operation,
                idempotency_key=idempotency_key,
                request_sha256=command.request_sha256,
                command_id=record.command_id,
                accepted_at=accepted_at,
            )
            .on_conflict_do_nothing(constraint="uq_command_idempotency_scope")
            .returning(IdempotencyReceiptRow.receipt_id)
        )
        if inserted is not None:
            session.add(
                CommandRow(
                    command_id=record.command_id,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    command_type=record.command_type,
                    status=record.status.value,
                    revision=record.revision,
                    terminal=record.terminal,
                    accepted_at=record.accepted_at,
                    updated_at=record.updated_at,
                    record_json=command_record_data(record),
                )
            )
            await append_in_session(
                session,
                system_audit_record(
                    context, "COMMAND_ACCEPTED", record.command_id, {"operation": operation}
                ),
                context,
            )
            return Success(CommandCreateReceipt(command=record, created=True))

        receipt = await session.scalar(
            select(IdempotencyReceiptRow).where(
                IdempotencyReceiptRow.tenant_id == tenant_id,
                IdempotencyReceiptRow.actor_id == actor_id,
                IdempotencyReceiptRow.operation == operation,
                IdempotencyReceiptRow.idempotency_key == idempotency_key,
            )
        )
        if receipt is None:  # defensive: a broken isolation configuration must fail loudly
            return Failure(_invariant("ACCEPT", "idempotency receipt disappeared"))
        if receipt.request_sha256 != command.request_sha256:
            return Failure(_idempotency_reused())
        existing = await session.scalar(
            select(CommandRow).where(
                CommandRow.command_id == receipt.command_id,
                CommandRow.tenant_id == tenant_id,
                CommandRow.actor_id == actor_id,
                CommandRow.command_type == operation,
            )
        )
        if existing is None:
            return Failure(_invariant("ACCEPT", "idempotency receipt has no command"))
        existing_record = await validated_command_record(session, existing)
        if existing_record is None:
            return Failure(_invariant("ACCEPT", "replayed command durable authority drifted"))
        return Success(CommandCreateReceipt(command=existing_record, created=False))

    async def transition(
        self, transition: CommandTransition, context: OperationContext
    ) -> Result[CommandRecord]:
        previous = transition.previous_record
        if (
            previous.request_context.actor.tenant_id != context.actor.tenant_id
            or previous.request_context.actor.actor_id != context.actor.actor_id
        ):
            return Failure(_not_found())
        async with self._sessions() as session, session.begin():
            return await self.transition_in_session(session, transition, context)

    async def transition_in_session(
        self, session: AsyncSession, transition: CommandTransition, context: OperationContext
    ) -> Result[CommandRecord]:
        """Apply one validated CAS transition as part of a larger atomic operation."""
        previous = transition.previous_record
        next_record = transition.next_record
        if (
            previous.request_context.actor.tenant_id != context.actor.tenant_id
            or previous.request_context.actor.actor_id != context.actor.actor_id
        ):
            return Failure(_not_found())
        result = await session.execute(
            update(CommandRow)
            .where(
                CommandRow.command_id == transition.command_id,
                CommandRow.tenant_id == context.actor.tenant_id,
                CommandRow.actor_id == context.actor.actor_id,
                CommandRow.revision == transition.expected_revision,
                CommandRow.status == transition.expected_status.value,
            )
            .values(
                status=next_record.status.value,
                revision=next_record.revision,
                terminal=next_record.terminal,
                updated_at=next_record.updated_at,
                record_json=command_record_data(next_record),
            )
        )
        if getattr(result, "rowcount", 0) != 1:
            return Failure(_revision_conflict())
        await append_in_session(
            session,
            system_audit_record(
                context,
                "COMMAND_TRANSITION",
                next_record.command_id,
                {"from_status": previous.status.value, "to_status": next_record.status.value},
            ),
            context,
        )
        return Success(next_record)

    async def find_non_terminal_before(
        self, updated_before: datetime, cursor: str | None, limit: int, context: OperationContext
    ) -> Result[CursorPage[CommandRecord]]:
        statement = (
            select(CommandRow)
            .where(
                CommandRow.tenant_id == context.actor.tenant_id,
                CommandRow.actor_id == context.actor.actor_id,
                CommandRow.terminal.is_(False),
                CommandRow.updated_at < updated_before,
            )
            .order_by(CommandRow.updated_at, CommandRow.command_id)
            .limit(limit + 1)
        )
        if cursor:
            try:
                cursor_at, cursor_id = _decode_cursor(cursor)
            except ValueError:
                return Failure(_invariant("READ", "invalid command cursor"))
            statement = statement.where(
                or_(
                    CommandRow.updated_at > cursor_at,
                    and_(CommandRow.updated_at == cursor_at, CommandRow.command_id > cursor_id),
                )
            )
        async with self._sessions() as session:
            rows = list((await session.scalars(statement)).all())
            records = tuple([await validated_command_record(session, row) for row in rows[:limit]])
        has_more = len(rows) > limit
        if any(record is None for record in records):
            return Failure(_invariant("READ", "command durable authority drifted"))
        valid_records = tuple(record for record in records if record is not None)
        return Success(
            CursorPage(
                items=valid_records,
                next_cursor=_encode_cursor(rows[limit - 1]) if has_more and valid_records else None,
            )
        )


async def validated_command_record(session: AsyncSession, row: CommandRow) -> CommandRecord | None:
    """Rebuild a Command and close any created resource to its workflow authority."""

    try:
        record = command_record_from_data(row.record_json)
    except (KeyError, TypeError, ValueError):
        return None
    origin = record.request_context
    if (
        record.command_id != row.command_id
        or origin.actor.tenant_id != row.tenant_id
        or origin.actor.actor_id != row.actor_id
        or record.command_type != row.command_type
        or record.status.value != row.status
        or record.revision != row.revision
        or record.terminal is not row.terminal
        or record.accepted_at != row.accepted_at
        or record.updated_at != row.updated_at
    ):
        return None
    if not await _resource_created_authority_matches(session, row, record):
        return None
    if not await _in_progress_turn_authority_matches(session, row, record):
        return None
    return record


async def _in_progress_turn_authority_matches(
    session: AsyncSession, row: CommandRow, record: CommandRecord
) -> bool:
    if record.command_type != "EXECUTE_AGENT_TURN" or record.status.value not in {
        "RUNNING_SANDBOX",
        "APPLYING_WORLD",
    }:
        return True
    jobs = list(
        (
            await session.scalars(
                select(WorkflowJobRow).where(
                    WorkflowJobRow.tenant_id == row.tenant_id,
                    WorkflowJobRow.command_id == row.command_id,
                )
            )
        ).all()
    )
    if len(jobs) != 1:
        return False
    job = jobs[0]
    receipts = list(
        (
            await session.scalars(
                select(JobStepReceiptRow).where(
                    JobStepReceiptRow.tenant_id == row.tenant_id,
                    JobStepReceiptRow.job_id == job.job_id,
                    JobStepReceiptRow.step_name == "SANDBOX_DISPATCHED",
                )
            )
        ).all()
    )
    if len(receipts) != 1:
        return False
    receipt = receipts[0]
    output = receipt.receipt_json
    job_wire = job.job_json
    command_receipt = await session.scalar(
        select(IdempotencyReceiptRow).where(
            IdempotencyReceiptRow.tenant_id == row.tenant_id,
            IdempotencyReceiptRow.actor_id == row.actor_id,
            IdempotencyReceiptRow.operation == row.command_type,
            IdempotencyReceiptRow.command_id == row.command_id,
        )
    )
    turn = await session.scalar(
        select(AgentTurnRow).where(
            AgentTurnRow.tenant_id == row.tenant_id,
            AgentTurnRow.actor_id == row.actor_id,
            AgentTurnRow.command_id == row.command_id,
            AgentTurnRow.turn_id == job.subject_id,
        )
    )
    if turn is None:
        return False
    owner = await session.scalar(
        select(AgentSessionRow).where(
            AgentSessionRow.tenant_id == row.tenant_id,
            AgentSessionRow.actor_id == row.actor_id,
            AgentSessionRow.session_id == turn.session_id,
            AgentSessionRow.status == "ACTIVE",
        )
    )
    turn_request = turn.request_json
    bindings = turn_request.get("skill_bindings")
    if (
        command_receipt is None
        or owner is None
        or not isinstance(bindings, list)
        or len(bindings) != 1
        or not isinstance(bindings[0], Mapping)
    ):
        return False
    binding = _skill_ref_from_wire(bindings[0])
    arguments = output.get("arguments")
    expected_world_revision = turn_request.get("expected_world_revision")
    if (
        binding is None
        or not isinstance(arguments, Mapping)
        or isinstance(expected_world_revision, bool)
        or not isinstance(expected_world_revision, int)
    ):
        return False
    expected_invocation_id = side_effect_execution_id(row.command_id, turn.turn_id)
    invocation_id = output.get("invocation_id")
    run_id = output.get("run_id")
    expected_run_id = (
        f"run_{hashlib.sha256(invocation_id.encode('utf-8')).hexdigest()[:24]}"
        if isinstance(invocation_id, str)
        else None
    )
    expected_request_sha256 = skill_invocation_request_sha256(
        tenant_id=row.tenant_id,
        invocation_id=expected_invocation_id,
        session_id=turn.session_id,
        turn_id=turn.turn_id,
        command_id=row.command_id,
        world_id=owner.world_id,
        expected_world_revision=expected_world_revision,
        skill_ref=binding,
        arguments=arguments,
    )
    job_state_matches = _recoverable_job_state_matches(job)
    if job.status == "WAITING_PROJECTION":
        job_state_matches = await _waiting_projection_authority_matches(
            session,
            row=row,
            record=record,
            job=job,
            turn=turn,
            run_id=run_id,
        )
    dispatch_matches = (
        job.job_id == workflow_job_id(row.tenant_id, row.command_id)
        and job.tenant_id == row.tenant_id
        and job.command_id == row.command_id
        and job.operation == record.command_type
        and job.subject_type == "AGENT_TURN"
        and job.subject_id == job_wire.get("turn_id") == turn.turn_id
        and set(job_wire)
        == {
            "schema_version",
            "request_context",
            "session_id",
            "turn_id",
            "turn_sequence",
            "request",
        }
        and job_wire.get("schema_version") == "1.0.0"
        and job_wire.get("session_id") == turn.session_id
        and job_wire.get("turn_sequence") == turn.turn_sequence
        and job_wire.get("request") == turn_request
        and job_wire.get("request_context") == request_context_data(record.request_context)
        and turn.created_at == record.accepted_at
        and turn_request.get("turn_id") == turn.turn_id
        and command_receipt.tenant_id == row.tenant_id
        and command_receipt.actor_id == row.actor_id
        and command_receipt.operation == row.command_type
        and command_receipt.command_id == row.command_id
        and command_receipt.accepted_at == record.accepted_at
        and job.request_sha256 == command_receipt.request_sha256
        and job_state_matches
        and receipt.receipt_id
        == workflow_step_receipt_id(row.tenant_id, job.job_id, "SANDBOX_DISPATCHED")
        and receipt.tenant_id == row.tenant_id
        and receipt.job_id == job.job_id
        and receipt.step_name == "SANDBOX_DISPATCHED"
        and 0 < receipt.fencing_token <= job.fencing_token
        and receipt.input_sha256 == expected_request_sha256
        and output.get("request_sha256") == expected_request_sha256
        and receipt.output_sha256 == workflow_receipt_sha256(output)
        and set(output)
        == {
            "schema_version",
            "invocation_id",
            "run_id",
            "request_sha256",
            "arguments",
            "skill",
            "world_id",
            "expected_world_revision",
        }
        and output.get("schema_version") == "1.0.0"
        and invocation_id == expected_invocation_id
        and _valid_identifier(run_id)
        and run_id == expected_run_id
        and output.get("skill") == dict(bindings[0])
        and output.get("world_id") == owner.world_id
        and output.get("expected_world_revision") == expected_world_revision
        and record.links.get("run") == f"/v1/runs/{run_id}"
    )
    if not dispatch_matches:
        return False
    if job.status == "WAITING_PROJECTION":
        return True
    if record.status.value == "RUNNING_SANDBOX":
        return True
    return await _applying_turn_authority_matches(
        session,
        row=row,
        record=record,
        job=job,
        dispatch=receipt,
        turn=turn,
        owner=owner,
        binding=binding,
        arguments=arguments,
        request_sha256=expected_request_sha256,
        run_id=run_id,
        expected_world_revision=expected_world_revision,
    )


async def _waiting_projection_authority_matches(
    session: AsyncSession,
    *,
    row: CommandRow,
    record: CommandRecord,
    job: WorkflowJobRow,
    turn: AgentTurnRow,
    run_id: object,
) -> bool:
    """Close a readable in-progress Command over its exact learner hand-off."""

    if (
        not isinstance(run_id, str)
        or job.status != "WAITING_PROJECTION"
        or job.phase != "LEARNER_QUEUED"
        or job.attempt < 1
        or job.fencing_token != job.attempt
        or job.lease_owner is not None
        or job.lease_expires_at is not None
        or job.next_attempt_at is not None
        or job.last_error_json is not None
    ):
        return False
    learner_rows = list(
        (
            await session.scalars(
                select(LearnerProjectionJobRow).where(
                    LearnerProjectionJobRow.tenant_id == row.tenant_id,
                    or_(
                        LearnerProjectionJobRow.job_id == job.job_id,
                        LearnerProjectionJobRow.command_id == row.command_id,
                        and_(
                            LearnerProjectionJobRow.session_id == turn.session_id,
                            LearnerProjectionJobRow.turn_id == turn.turn_id,
                        ),
                        LearnerProjectionJobRow.run_id == run_id,
                    ),
                )
            )
        ).all()
    )
    if len(learner_rows) != 1:
        return False
    learner = learner_rows[0]
    objective = learner.projection_json
    identity = objective.get("identity")
    projection = objective.get("projection")
    if not isinstance(identity, Mapping) or not isinstance(projection, Mapping):
        return False
    expected_identity = {
        "tenant_id": row.tenant_id,
        "job_id": job.job_id,
        "command_id": row.command_id,
        "session_id": turn.session_id,
        "turn_id": turn.turn_id,
        "run_id": run_id,
        "learner_id": learner.learner_id,
        "actor_id": row.actor_id,
        "content_hash": record.request_context.content_ref.content_hash,
    }
    return (
        learner.job_id == job.job_id
        and learner.tenant_id == row.tenant_id
        and learner.command_id == row.command_id
        and learner.session_id == turn.session_id
        and learner.turn_id == turn.turn_id
        and learner.run_id == run_id
        and learner.actor_id == row.actor_id
        and learner.content_hash == record.request_context.content_ref.content_hash
        and learner.created_at == job.updated_at
        and record.updated_at <= learner.created_at
        and objective.get("schema_version") == "1.0.0"
        and dict(identity) == expected_identity
        and objective.get("command") == command_record_data(record)
        and objective.get("source_feedback_event_id") == learner.source_event_id
        and projection.get("expected_revision") == learner.expected_revision
        and projection.get("through_sequence") == learner.through_sequence
        and learner.request_sha256 == workflow_json_sha256(objective)
        and _learner_projection_claim_state_matches(learner)
    )


def _learner_projection_claim_state_matches(row: LearnerProjectionJobRow) -> bool:
    if (
        row.attempt < 0
        or row.fencing_token != row.attempt
        or row.created_at.tzinfo is None
        or row.updated_at.tzinfo is None
        or row.updated_at < row.created_at
        or row.result_sha256 is not None
        or row.result_json is not None
        or row.completed_at is not None
    ):
        return False
    if row.status == "READY":
        return (
            row.attempt == 0
            and row.lease_owner is None
            and row.lease_expires_at is None
            and row.next_attempt_at is not None
            and row.next_attempt_at.tzinfo is not None
            and row.next_attempt_at >= row.created_at
            and row.last_error_json is None
        )
    if row.status in {"CLAIMED", "RUNNING"}:
        return (
            row.attempt >= 1
            and isinstance(row.lease_owner, str)
            and bool(row.lease_owner)
            and row.lease_expires_at is not None
            and row.lease_expires_at.tzinfo is not None
            and row.lease_expires_at >= row.updated_at
            and row.next_attempt_at is None
            and row.last_error_json is None
        )
    if row.status == "RETRY_WAIT":
        return (
            row.attempt >= 1
            and row.lease_owner is None
            and row.lease_expires_at is None
            and row.next_attempt_at is not None
            and row.next_attempt_at.tzinfo is not None
            and row.next_attempt_at >= row.updated_at
            and isinstance(row.last_error_json, Mapping)
        )
    return False


async def _applying_turn_authority_matches(
    session: AsyncSession,
    *,
    row: CommandRow,
    record: CommandRecord,
    job: WorkflowJobRow,
    dispatch: JobStepReceiptRow,
    turn: AgentTurnRow,
    owner: AgentSessionRow,
    binding: SkillRef,
    arguments: Mapping[str, object],
    request_sha256: str,
    run_id: object,
    expected_world_revision: int,
) -> bool:
    completion_receipts = list(
        (
            await session.scalars(
                select(JobStepReceiptRow).where(
                    JobStepReceiptRow.tenant_id == row.tenant_id,
                    JobStepReceiptRow.job_id == job.job_id,
                    JobStepReceiptRow.step_name == "SKILL_INVOKED",
                )
            )
        ).all()
    )
    runs = list(
        (
            await session.scalars(
                select(RunRow).where(
                    RunRow.tenant_id == row.tenant_id,
                    RunRow.actor_id == row.actor_id,
                    RunRow.command_id == row.command_id,
                )
            )
        ).all()
    )
    if len(completion_receipts) != 1 or len(runs) != 1:
        return False
    completion_receipt = completion_receipts[0]
    run_row = runs[0]
    completion = completion_receipt.receipt_json
    completion_run = completion.get("run")
    run_wire = run_row.run_json
    sandbox = run_wire.get("sandbox")
    world_application = run_wire.get("world_application")
    if (
        not isinstance(completion_run, Mapping)
        or not isinstance(sandbox, Mapping)
        or not isinstance(world_application, Mapping)
    ):
        return False
    origin = request_context_data(record.request_context)
    binding_wire = {
        "skill_id": binding.skill_id,
        "skill_version_id": binding.skill_version_id,
        "artifact_sha256": binding.artifact_sha256,
        "certification_id": binding.certification_id,
    }
    world_commit = completion_run.get("world_commit")
    run_created_at = _parse_timestamp(run_wire.get("created_at"))
    run_updated_at = _parse_timestamp(run_wire.get("updated_at"))
    world_committed_at = (
        _parse_timestamp(world_commit.get("committed_at"))
        if isinstance(world_commit, Mapping)
        else None
    )
    return (
        completion_receipt.receipt_id
        == workflow_step_receipt_id(row.tenant_id, job.job_id, "SKILL_INVOKED")
        and completion_receipt.tenant_id == row.tenant_id
        and completion_receipt.job_id == job.job_id
        and completion_receipt.step_name == "SKILL_INVOKED"
        and dispatch.fencing_token <= completion_receipt.fencing_token
        and completion_receipt.fencing_token <= job.fencing_token
        and completion_receipt.completed_at >= dispatch.completed_at
        and completion_receipt.input_sha256 == request_sha256
        and completion_receipt.output_sha256 == workflow_receipt_sha256(completion)
        and set(completion)
        == {
            "schema_version",
            "invocation_id",
            "tenant_id",
            "request_sha256",
            "arguments",
            "run",
        }
        and completion.get("schema_version") == "1.0.0"
        and completion.get("invocation_id") == dispatch.receipt_json.get("invocation_id")
        and completion.get("tenant_id") == row.tenant_id
        and completion.get("request_sha256") == request_sha256
        and completion.get("arguments") == dict(arguments)
        and set(completion_run)
        == {
            "run_id",
            "session_id",
            "turn_id",
            "command_id",
            "world_id",
            "skill_ref",
            "task_success",
            "world_revision_before",
            "world_revision_after",
            "world_difference",
            "failed_actions",
            "failure_key",
            "evidence_refs",
            "world_commit",
            "request_context",
        }
        and completion_run.get("run_id") == run_id == run_row.run_id
        and completion_run.get("session_id") == turn.session_id == run_row.session_id
        and completion_run.get("turn_id") == turn.turn_id == run_row.turn_id
        and completion_run.get("command_id") == row.command_id == run_row.command_id
        and completion_run.get("world_id") == owner.world_id
        and completion_run.get("skill_ref") == binding_wire
        and completion_run.get("task_success") is True
        and completion_run.get("world_revision_before") == expected_world_revision
        and completion_run.get("world_revision_after") == expected_world_revision + 1
        and completion_run.get("failed_actions") == []
        and completion_run.get("failure_key") is None
        and isinstance(completion_run.get("evidence_refs"), list)
        and completion_run.get("request_context") == origin
        and isinstance(world_commit, Mapping)
        and set(world_commit)
        == {
            "world_id",
            "previous_revision",
            "world_revision",
            "first_event_sequence",
            "last_event_sequence",
            "state_hash",
            "committed_at",
        }
        and world_commit.get("world_id") == owner.world_id
        and world_commit.get("previous_revision") == expected_world_revision
        and world_commit.get("world_revision") == expected_world_revision + 1
        and isinstance(world_commit.get("first_event_sequence"), int)
        and not isinstance(world_commit.get("first_event_sequence"), bool)
        and world_commit.get("first_event_sequence") == world_commit.get("last_event_sequence")
        and _valid_sha256(world_commit.get("state_hash"))
        and run_row.tenant_id == row.tenant_id
        and run_row.actor_id == row.actor_id
        and run_row.content_hash == record.request_context.content_ref.content_hash
        and set(run_wire)
        == {
            "request_context",
            "run_id",
            "session_id",
            "turn_id",
            "command_id",
            "status",
            "terminal",
            "skill",
            "sandbox",
            "world_application",
            "agent_feedback",
            "created_at",
            "updated_at",
            "evidence_refs",
            "versions",
        }
        and run_wire.get("request_context") == origin
        and run_wire.get("run_id") == run_row.run_id
        and run_wire.get("session_id") == run_row.session_id
        and run_wire.get("turn_id") == run_row.turn_id
        and run_wire.get("command_id") == run_row.command_id
        and run_wire.get("status") == "SUCCEEDED"
        and run_wire.get("terminal") is True
        and run_wire.get("skill") == binding_wire
        and run_wire.get("agent_feedback") is None
        and run_wire.get("evidence_refs") == completion_run.get("evidence_refs")
        and run_wire.get("versions") == row.record_json.get("versions")
        and run_created_at == run_row.created_at
        and run_updated_at == record.updated_at
        and world_committed_at == record.updated_at
        and set(sandbox)
        == {
            "invocation_id",
            "status",
            "started_at",
            "finished_at",
            "limits",
            "usage",
            "action_intents",
            "failure",
        }
        and sandbox.get("invocation_id") == completion.get("invocation_id")
        and sandbox.get("status") == "SUCCEEDED"
        and sandbox.get("failure") is None
        and set(world_application) == {"status", "receipt", "failure"}
        and world_application.get("status") == "COMMITTED"
        and world_application.get("receipt") == world_commit
        and world_application.get("failure") is None
        and record.links.get("run") == f"/v1/runs/{run_row.run_id}"
    )


def _recoverable_job_state_matches(job: WorkflowJobRow) -> bool:
    if job.attempt < 1 or job.fencing_token != job.attempt:
        return False
    if job.status == "RETRY_WAIT":
        return (
            job.lease_owner is None
            and job.lease_expires_at is None
            and job.next_attempt_at is not None
            and isinstance(job.last_error_json, Mapping)
        )
    if job.status in {"CLAIMED", "RUNNING"}:
        return (
            isinstance(job.lease_owner, str)
            and bool(job.lease_owner)
            and job.lease_expires_at is not None
            and job.next_attempt_at is None
        )
    return False


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _skill_ref_from_wire(value: object) -> SkillRef | None:
    if not isinstance(value, Mapping) or set(value) != {
        "skill_id",
        "skill_version_id",
        "artifact_sha256",
        "certification_id",
    }:
        return None
    skill_id = value.get("skill_id")
    skill_version_id = value.get("skill_version_id")
    artifact_sha256 = value.get("artifact_sha256")
    certification_id = value.get("certification_id")
    if not all(
        isinstance(item, str)
        for item in (skill_id, skill_version_id, artifact_sha256, certification_id)
    ):
        return None
    assert isinstance(skill_id, str)
    assert isinstance(skill_version_id, str)
    assert isinstance(artifact_sha256, str)
    assert isinstance(certification_id, str)
    try:
        return SkillRef(
            skill_id=skill_id,
            skill_version_id=skill_version_id,
            artifact_sha256=artifact_sha256,
            certification_id=certification_id,
        )
    except ValueError:
        return None


async def _resource_created_authority_matches(
    session: AsyncSession, row: CommandRow, record: CommandRecord
) -> bool:
    result = record.result
    if not isinstance(result, Mapping) or result.get("result_type") != "RESOURCE_CREATED":
        return True
    resource_type = result.get("resource_type")
    resource_id = result.get("resource_id")
    resource_url = result.get("resource_url")
    if not isinstance(resource_type, str) or not isinstance(resource_id, str):
        return False
    expected_url = {
        "SKILL_BUILD": f"/v1/skill-builds/{resource_id}",
        "AGENT_SESSION": f"/v1/agent-sessions/{resource_id}",
        "SKILL_ACTIVATION": f"/v1/skill-activations/{resource_id}",
    }.get(resource_type)
    if expected_url is None or resource_url != expected_url:
        return False
    job = await session.scalar(
        select(WorkflowJobRow).where(
            WorkflowJobRow.tenant_id == row.tenant_id,
            WorkflowJobRow.command_id == row.command_id,
        )
    )
    if (
        job is None
        or job.job_id != workflow_job_id(row.tenant_id, row.command_id)
        or job.operation != record.command_type
        or job.subject_type != resource_type
        or job.subject_id != resource_id
        or job.status != "SUCCEEDED"
        or job.phase != "COMPLETE"
        or job.attempt < 1
        or job.fencing_token < 1
        or job.lease_owner is not None
        or job.lease_expires_at is not None
        or job.next_attempt_at is not None
        or job.last_error_json is not None
        or not record.accepted_at <= job.created_at <= record.updated_at <= job.updated_at
        or not _resource_job_bytes_match(job, resource_type, resource_id, record)
    ):
        return False
    if (
        not record.terminal
        or record.status.value != "APPLIED"
        or record.stage != "COMPLETE"
        or record.error is not None
    ):
        return False
    if resource_type == "SKILL_BUILD":
        resource = await session.scalar(
            select(SkillBuildRow).where(
                SkillBuildRow.tenant_id == row.tenant_id,
                SkillBuildRow.actor_id == row.actor_id,
                SkillBuildRow.command_id == row.command_id,
                SkillBuildRow.build_id == resource_id,
            )
        )
        if resource is None:
            return False
        # Import lazily: skill_builds owns the full Build authority and imports
        # CommandStore for acceptance, so a module-level import would cycle.
        from .skill_builds import validate_historical_build_authority

        return await validate_historical_build_authority(
            session, resource, row, record, job
        )
    if resource_type == "AGENT_SESSION":
        resource = await session.scalar(
            select(AgentSessionRow).where(
                AgentSessionRow.tenant_id == row.tenant_id,
                AgentSessionRow.actor_id == row.actor_id,
                AgentSessionRow.command_id == row.command_id,
                AgentSessionRow.session_id == resource_id,
            )
        )
        return (
            resource is not None
            and resource.status == "ACTIVE"
            and resource.session_json.get("session_id") == resource.session_id
            and resource.session_json.get("status") == resource.status
            and resource.session_json.get("request_context")
            == request_context_data(record.request_context)
        )
    expected_activation_id = _activation_id(row.command_id)
    if resource_id != expected_activation_id:
        return False
    resource = await session.scalar(
        select(SkillActivationRow).where(
            SkillActivationRow.tenant_id == row.tenant_id,
            SkillActivationRow.actor_id == row.actor_id,
            SkillActivationRow.content_hash == record.request_context.content_ref.content_hash,
            SkillActivationRow.activation_id == resource_id,
        )
    )
    if resource is None:
        return False
    from .activation_authority import validate_historical_activation_authority

    return await validate_historical_activation_authority(session, resource)


def _resource_job_bytes_match(
    job: WorkflowJobRow,
    resource_type: str,
    resource_id: str,
    record: CommandRecord,
) -> bool:
    value = job.job_json
    expected_key = {
        "SKILL_BUILD": "build_id",
        "AGENT_SESSION": "session_id",
        "SKILL_ACTIVATION": "activation_id",
    }.get(resource_type)
    expected_keys = {
        "SKILL_BUILD": {
            "schema_version",
            "request_context",
            "build_id",
            "build_provenance_sha256",
            "request",
        },
        "AGENT_SESSION": {"schema_version", "request_context", "session_id", "request"},
        "SKILL_ACTIVATION": {
            "schema_version",
            "request_context",
            "activation_id",
            "authority_id",
            "expected_registry_revision",
            "activation_scope",
            "skill",
            "build_provenance_sha256",
            "certification_sha256",
            "artifact_authority_sha256",
            "reason",
        },
    }.get(resource_type)
    return (
        expected_key is not None
        and expected_keys is not None
        and set(value) == expected_keys
        and value.get("schema_version") == "1.0.0"
        and value.get("request_context") == request_context_data(record.request_context)
        and value.get(expected_key) == resource_id
        and (
            resource_type == "AGENT_SESSION"
            or (
                _valid_sha256(value.get("build_provenance_sha256"))
                and (
                    resource_type != "SKILL_ACTIVATION"
                    or (
                        _valid_sha256(value.get("certification_sha256"))
                        and _valid_sha256(value.get("artifact_authority_sha256"))
                    )
                )
            )
        )
    )


def _activation_id(command_id: str) -> str:
    digest = hashlib.sha256(f"activation\x00{command_id}".encode()).hexdigest()
    return f"activation_{digest[:24]}"


def _valid_identifier(value: object) -> bool:
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    return (
        isinstance(value, str)
        and 8 <= len(value) <= 128
        and value[0] in allowed[:62]
        and all(character in allowed for character in value)
    )


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _error(code: str, stage: str, message: str, *, retryable: bool) -> Any:
    from yaya_agent_contracts import ContractError, ErrorCategory

    metadata = {
        "NOT_FOUND": (ErrorCategory.VALIDATION, False, "resource.not_found"),
        "IDEMPOTENCY_KEY_REUSED": (
            ErrorCategory.CONCURRENCY,
            False,
            "request.idempotency_conflict",
        ),
        "WORLD_REVISION_CONFLICT": (ErrorCategory.CONCURRENCY, True, "world.changed_retry"),
        "INVARIANT_VIOLATION": (ErrorCategory.INVARIANT, False, "system.invariant_violation"),
    }[code]
    return ContractError(
        code=code,
        category=metadata[0],
        retryable=retryable,
        user_message_key=metadata[2],
        stage=stage,
        message=message,
    )


def _not_found() -> Any:
    return _error("NOT_FOUND", "READ", "command not found", retryable=False)


def _idempotency_reused() -> Any:
    return _error("IDEMPOTENCY_KEY_REUSED", "ACCEPT", "idempotency key was reused", retryable=False)


def _revision_conflict() -> Any:
    return _error(
        "WORLD_REVISION_CONFLICT", "TRANSITION", "stale command revision or status", retryable=True
    )


def _invariant(stage: str, message: str) -> Any:
    return _error("INVARIANT_VIOLATION", stage, message, retryable=False)


def _encode_cursor(row: CommandRow) -> str:
    raw = json.dumps(
        {"updated_at": row.updated_at.isoformat(), "command_id": row.command_id},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(cursor + padding))
        updated_at = datetime.fromisoformat(data["updated_at"].replace("Z", "+00:00"))
        command_id = data["command_id"]
        if not isinstance(command_id, str) or updated_at.tzinfo is None:
            raise ValueError("invalid command cursor")
        return updated_at, command_id
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
    ) as error:
        raise ValueError("invalid command cursor") from error
