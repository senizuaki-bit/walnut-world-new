"""Immutable Run and Evidence projections for Game read operationIds."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    CommandRecord,
    CommandStatus,
    Failure,
    OperationContext,
    Result,
    SkillRef,
    Success,
    canonical_json_sha256,
)
from yaya_agent_runtime import (
    GameEvent,
    RunResultSnapshot,
    SkillInvocationResult,
    derive_run_outcome_event,
    side_effect_execution_id,
)

from . import run_outcomes as run_outcome_validators
from .command_store import PostgresCommandStore, validated_command_record
from .durable_llm import validated_provider_terminal_receipt
from .models import (
    AgentTurnRow,
    CommandRow,
    EvidenceRow,
    IdempotencyReceiptRow,
    JobStepReceiptRow,
    LearnerProjectionJobRow,
    ProductInteractionRow,
    RunRow,
    WorkflowJobRow,
    command_record_from_data,
    json_value,
    request_context_data,
)
from .run_outcomes import (
    ValidatedRunAuthority,
    canonical_outcome_occurred_at,
    list_validated_session_runs,
    load_final_provider_receipts,
    load_task_snapshot,
    load_validated_run,
    run_authority_sha256,
    validate_canonical_outcome_event,
    validate_terminal_projection,
)
from .skill_builds import PostgresSkillBuildStore
from .skill_invocation import (
    invocation_result_from_receipt,
    invocation_result_receipt_data,
)
from .workflow_jobs import (
    WorkflowInvariantError,
    workflow_receipt_sha256,
    workflow_step_receipt_id,
)


@dataclass(frozen=True, slots=True)
class _NonterminalRunAuthority:
    """Reduced read authority for pre-A8 legacy Runs with no AgentTurn resource."""

    result: SkillInvocationResult
    run_row: RunRow
    command: CommandRecord
    job: WorkflowJobRow
    context: OperationContext

    @property
    def run(self) -> RunResultSnapshot:
        return self.result.run


_PublicRunAuthority = ValidatedRunAuthority | _NonterminalRunAuthority


class PostgresRunEvidenceStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory
        self._builds = PostgresSkillBuildStore(
            session_factory,
            PostgresCommandStore(session_factory),
        )

    async def get_run(self, run_id: str, context: OperationContext) -> Result[dict[str, Any]]:
        try:
            async with self._sessions() as session:
                row = await session.scalar(
                    select(RunRow).where(
                        RunRow.run_id == run_id,
                        RunRow.tenant_id == context.actor.tenant_id,
                        RunRow.actor_id == context.actor.actor_id,
                    )
                )
                if row is not None:
                    validation_state = (
                        run_outcome_validators.TerminalProjectionValidationState()
                    )
                    await _validated_run_for_public_read(
                        session,
                        row,
                        context,
                        validation_state=validation_state,
                    )
        except WorkflowInvariantError as error:
            return Failure(_invariant(str(error)))
        if row is None:
            return Failure(_not_found("run"))
        return Success(dict(row.run_json))

    async def get_evidence(
        self, evidence_id: str, context: OperationContext
    ) -> Result[dict[str, Any]]:
        build_id: str | None = None
        try:
            async with self._sessions() as session:
                row = await session.scalar(
                    select(EvidenceRow).where(
                        EvidenceRow.evidence_id == evidence_id,
                        EvidenceRow.tenant_id == context.actor.tenant_id,
                        EvidenceRow.actor_id == context.actor.actor_id,
                    )
                )
                if row is not None:
                    source = _object(row.evidence_json.get("source"), "Evidence source")
                    source_type = _text(source, "source_type")
                    command_id = _text(source, "command_id")
                    if command_id != row.command_id:
                        raise WorkflowInvariantError("Evidence source Command drifted")
                    if source_type == "SKILL_BUILD":
                        build_id = _text(source, "source_id")
                    elif source_type in {"SKILL_RUN", "WORLD", "LEARNER_PROJECTOR"}:
                        run = await session.scalar(
                            select(RunRow).where(
                                RunRow.tenant_id == row.tenant_id,
                                RunRow.actor_id == row.actor_id,
                                RunRow.content_hash == row.content_hash,
                                RunRow.command_id == command_id,
                            )
                        )
                        if run is None:
                            raise WorkflowInvariantError("Evidence has no exact Run authority")
                        validation_state = (
                            run_outcome_validators.TerminalProjectionValidationState()
                        )
                        authority = await _validated_run_for_public_read(
                            session,
                            run,
                            context,
                            validation_state=validation_state,
                        )
                        if source_type == "LEARNER_PROJECTOR":
                            if not isinstance(authority, ValidatedRunAuthority):
                                raise WorkflowInvariantError(
                                    "Learner Evidence has no terminal Run authority"
                                )
                            await validate_terminal_projection(
                                session,
                                authority,
                                validation_state=validation_state,
                            )
                            learner_job = await session.scalar(
                                select(LearnerProjectionJobRow).where(
                                    LearnerProjectionJobRow.tenant_id == row.tenant_id,
                                    LearnerProjectionJobRow.actor_id == row.actor_id,
                                    LearnerProjectionJobRow.content_hash == row.content_hash,
                                    LearnerProjectionJobRow.command_id == command_id,
                                    LearnerProjectionJobRow.run_id == run.run_id,
                                    LearnerProjectionJobRow.status == "SUCCEEDED",
                                )
                            )
                            expected_id = (
                                _identifier(
                                    "evidence_learner",
                                    row.tenant_id,
                                    learner_job.job_id,
                                    "LEARNER_UPDATE",
                                )
                                if learner_job is not None
                                else None
                            )
                            if (
                                learner_job is None
                                or row.evidence_id != expected_id
                                or source.get("source_id") != learner_job.learner_id
                            ):
                                raise WorkflowInvariantError(
                                    "Evidence differs from terminal Learner authority"
                                )
                        else:
                            references = {
                                reference.evidence_id: reference
                                for reference in authority.run.evidence_refs
                            }
                            reference = references.get(row.evidence_id)
                            expected_source = (
                                authority.run.run_id
                                if source_type == "SKILL_RUN"
                                else authority.run.world_id
                            )
                            if (
                                reference is None
                                or source.get("source_id") != expected_source
                            ):
                                raise WorkflowInvariantError(
                                    "Evidence differs from its Run source authority"
                                )
                        build_id = None
                    else:
                        raise WorkflowInvariantError("Evidence source type is unsupported")
                    validate_evidence_document_authority(row)
        except WorkflowInvariantError as error:
            return Failure(_invariant(str(error)))
        if row is None:
            return Failure(_not_found("evidence"))
        if build_id is not None:
            build = await self._builds.get(build_id, context)
            if isinstance(build, Failure):
                return Failure(_invariant("Evidence Build authority drifted"))
            refs = build.value.get("evidence_refs")
            if (
                not isinstance(refs, list)
                or len(refs) != 1
                or not isinstance(refs[0], Mapping)
                or refs[0].get("evidence_id") != evidence_id
            ):
                return Failure(_invariant("Evidence differs from terminal Build"))
        return Success(dict(row.evidence_json))

    async def record_run(self, value: Mapping[str, Any], context: OperationContext) -> Result[None]:
        """Durably record an exact worker-produced Run exactly once."""
        if not _same_origin(value, context):
            return Failure(_invariant("run origin does not match worker context"))
        run_id = value.get("run_id")
        if not isinstance(run_id, str):
            return Failure(_invariant("run has no identifier"))
        try:
            created_at = _parse_timestamp(value["created_at"])
            async with self._sessions() as session, session.begin():
                existing = await session.scalar(select(RunRow).where(RunRow.run_id == run_id))
                if existing is not None:
                    return Success(None) if existing.run_json == dict(value) else Failure(_invariant("run ID reused"))
                session.add(
                    RunRow(
                        run_id=run_id,
                        tenant_id=context.actor.tenant_id,
                        actor_id=context.actor.actor_id,
                        content_hash=context.content_ref.content_hash,
                        session_id=_string(value, "session_id"),
                        turn_id=_string(value, "turn_id"),
                        command_id=_string(value, "command_id"),
                        created_at=created_at,
                        run_json=dict(value),
                    )
                )
        except (KeyError, TypeError, ValueError) as error:
            return Failure(_invariant(f"invalid run projection: {error}"))
        return Success(None)

    async def record_evidence(
        self, value: Mapping[str, Any], context: OperationContext
    ) -> Result[None]:
        """Durably record exact worker-produced Evidence exactly once."""
        if not _same_origin(value, context):
            return Failure(_invariant("evidence origin does not match worker context"))
        reference = value.get("evidence_ref")
        if not isinstance(reference, Mapping) or not isinstance(reference.get("evidence_id"), str):
            return Failure(_invariant("evidence has no identifier"))
        evidence_id = reference["evidence_id"]
        try:
            recorded_at = _parse_timestamp(value["recorded_at"])
            source = value.get("source")
            command_id = source.get("command_id") if isinstance(source, Mapping) else None
            async with self._sessions() as session, session.begin():
                existing = await session.scalar(
                    select(EvidenceRow).where(EvidenceRow.evidence_id == evidence_id)
                )
                if existing is not None:
                    return (
                        Success(None)
                        if existing.evidence_json == dict(value)
                        else Failure(_invariant("evidence ID reused"))
                    )
                session.add(
                    EvidenceRow(
                        evidence_id=evidence_id,
                        tenant_id=context.actor.tenant_id,
                        actor_id=context.actor.actor_id,
                        content_hash=context.content_ref.content_hash,
                        command_id=command_id if isinstance(command_id, str) else None,
                        recorded_at=recorded_at,
                        evidence_json=dict(value),
                    )
                )
        except (KeyError, TypeError, ValueError) as error:
            return Failure(_invariant(f"invalid evidence projection: {error}"))
        return Success(None)


async def _validated_run_for_public_read(
    session: AsyncSession,
    row: RunRow,
    context: OperationContext,
    *,
    validation_state: run_outcome_validators.TerminalProjectionValidationState | None = None,
) -> _PublicRunAuthority:
    if validation_state is None:
        validation_state = run_outcome_validators.TerminalProjectionValidationState()
    validation_state.bind_session(session)
    command_row = await session.scalar(
        select(CommandRow).where(
            CommandRow.tenant_id == row.tenant_id,
            CommandRow.actor_id == row.actor_id,
            CommandRow.command_id == row.command_id,
        )
    )
    if command_row is None:
        raise WorkflowInvariantError("Run Command authority is missing")
    try:
        command = command_record_from_data(command_row.record_json)
    except (KeyError, TypeError, ValueError) as error:
        raise WorkflowInvariantError("Run Command row is invalid") from error
    if (
        command.command_id != command_row.command_id
        or command.request_context.actor.tenant_id != command_row.tenant_id
        or command.request_context.actor.actor_id != command_row.actor_id
        or command.command_type != command_row.command_type
        or command.status.value != command_row.status
        or command.revision != command_row.revision
        or command.terminal is not command_row.terminal
        or command.accepted_at != command_row.accepted_at
        or command.updated_at != command_row.updated_at
    ):
        raise WorkflowInvariantError("Run Command row authority drifted")
    if command.terminal:
        validated = await validated_command_record(session, command_row)
        if validated is None or validated != command:
            raise WorkflowInvariantError("terminal Run Command authority drifted")
    origin = command.request_context
    expected_context = OperationContext(
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
    if (
        context.actor.tenant_id != origin.actor.tenant_id
        or context.actor.actor_id != origin.actor.actor_id
    ):
        raise WorkflowInvariantError("Run read context differs from durable authority")
    turn = await session.scalar(
        select(AgentTurnRow).where(
            AgentTurnRow.tenant_id == row.tenant_id,
            AgentTurnRow.actor_id == row.actor_id,
            AgentTurnRow.command_id == row.command_id,
        )
    )
    if turn is None:
        return await _validate_nonterminal_run_without_turn(
            session,
            row=row,
            command=command,
            context=expected_context,
        )
    authority = await load_validated_run(
        session,
        tenant_id=row.tenant_id,
        actor_id=row.actor_id,
        content_hash=row.content_hash,
        command_id=row.command_id,
        expected_context=expected_context,
        require_current_world=False,
        validation_state=validation_state,
    )
    receipt = await session.scalar(
        select(JobStepReceiptRow).where(
            JobStepReceiptRow.tenant_id == authority.job.tenant_id,
            JobStepReceiptRow.job_id == authority.job.job_id,
            JobStepReceiptRow.step_name == "OUTCOME_DERIVED",
        )
    )
    if receipt is None:
        if authority.command.terminal:
            raise WorkflowInvariantError("terminal Run has no immutable outcome/hash authority")
        return authority
    output = _object(receipt.receipt_json, "Run outcome receipt")
    outcome = _object(output.get("event"), "Run outcome event")
    if (
        receipt.receipt_id
        != workflow_step_receipt_id(
            authority.job.tenant_id,
            authority.job.job_id,
            "OUTCOME_DERIVED",
        )
        or receipt.input_sha256 != authority.result.request_sha256
        or receipt.output_sha256 != workflow_receipt_sha256(output)
        or receipt.fencing_token < 1
        or receipt.fencing_token > authority.job.fencing_token
        or set(output)
        != {"schema_version", "event", "run_sha256", "invocation_request_sha256"}
        or output.get("schema_version") != "1.0.0"
        or output.get("run_sha256") != run_authority_sha256(row.run_json)
        or output.get("invocation_request_sha256") != authority.result.request_sha256
    ):
        raise WorkflowInvariantError("Run outcome/hash receipt drifted")
    if authority.command.status in {CommandStatus.APPLIED, CommandStatus.REJECTED}:
        await validate_terminal_projection(
            session,
            authority,
            validation_state=validation_state,
        )
    elif authority.command.status is CommandStatus.FAILED:
        await _validate_provider_failed_terminal_run(session, authority, outcome)
    else:
        await validate_canonical_outcome_event(
            session,
            authority=authority,
            outcome=outcome,
            validation_state=validation_state,
        )
    return authority


async def _validate_nonterminal_run_without_turn(
    session: AsyncSession,
    *,
    row: RunRow,
    command: CommandRecord,
    context: OperationContext,
) -> _NonterminalRunAuthority:
    """Close a pre-outcome Run over Command/Job/SKILL_INVOKED durable bytes.

    Historical pre-A8 workers could persist a recoverable Run before the
    public AgentTurn resource.  Such a Run is readable only while its Command
    remains nonterminal and no outcome authority has been published.
    """

    if command.terminal or command.status not in {
        CommandStatus.RUNNING_SANDBOX,
        CommandStatus.APPLYING_WORLD,
    }:
        raise WorkflowInvariantError("terminal Run has no AgentTurn authority")
    job = await session.scalar(
        select(WorkflowJobRow).where(
            WorkflowJobRow.tenant_id == row.tenant_id,
            WorkflowJobRow.command_id == row.command_id,
            WorkflowJobRow.operation == "EXECUTE_AGENT_TURN",
            WorkflowJobRow.subject_type == "AGENT_TURN",
        )
    )
    if job is None:
        raise WorkflowInvariantError("nonterminal Run Job authority is missing")
    command_receipt = await session.scalar(
        select(IdempotencyReceiptRow).where(
            IdempotencyReceiptRow.tenant_id == row.tenant_id,
            IdempotencyReceiptRow.actor_id == row.actor_id,
            IdempotencyReceiptRow.operation == command.command_type,
            IdempotencyReceiptRow.command_id == command.command_id,
        )
    )
    receipt = await session.scalar(
        select(JobStepReceiptRow).where(
            JobStepReceiptRow.tenant_id == row.tenant_id,
            JobStepReceiptRow.job_id == job.job_id,
            JobStepReceiptRow.step_name == "SKILL_INVOKED",
        )
    )
    outcome = await session.scalar(
        select(JobStepReceiptRow.receipt_id).where(
            JobStepReceiptRow.tenant_id == row.tenant_id,
            JobStepReceiptRow.job_id == job.job_id,
            JobStepReceiptRow.step_name == "OUTCOME_DERIVED",
        )
    )
    if command_receipt is None or receipt is None or outcome is not None:
        raise WorkflowInvariantError(
            "nonterminal Run invocation/outcome authority is inconsistent"
        )
    try:
        result = invocation_result_from_receipt(receipt.receipt_json)
    except (KeyError, TypeError, ValueError) as error:
        raise WorkflowInvariantError("SKILL_INVOKED receipt is not canonical") from error
    job_wire = job.job_json
    expected_receipt = invocation_result_receipt_data(result)
    if (
        set(job_wire)
        != {
            "schema_version",
            "request_context",
            "session_id",
            "turn_id",
            "turn_sequence",
            "request",
        }
        or job_wire.get("schema_version") != "1.0.0"
        or command_receipt.accepted_at != command.accepted_at
        or job.request_sha256 != command_receipt.request_sha256
        or job_wire.get("request_context")
        != request_context_data(command.request_context)
        or job.subject_id != result.run.turn_id
        or job_wire.get("turn_id") != result.run.turn_id
        or job_wire.get("session_id") != result.run.session_id
        or command.links.get("run") != f"/v1/runs/{result.run.run_id}"
        or receipt.receipt_id
        != workflow_step_receipt_id(row.tenant_id, job.job_id, "SKILL_INVOKED")
        or receipt.fencing_token < 1
        or receipt.fencing_token > job.fencing_token
        or receipt.input_sha256 != result.request_sha256
        or receipt.output_sha256 != workflow_receipt_sha256(receipt.receipt_json)
        or receipt.receipt_json != expected_receipt
        or result.tenant_id != row.tenant_id
        or result.invocation_id
        != side_effect_execution_id(result.run.command_id, result.run.turn_id)
        or result.run.command_id != row.command_id
        or result.run.run_id != row.run_id
        or result.run.request_context.actor != context.actor
        or result.run.request_context.content_ref != context.content_ref
    ):
        raise WorkflowInvariantError("nonterminal Run durable authority drifted")
    run_outcome_validators._validate_run_row(  # pyright: ignore[reportPrivateUsage]
        row,
        result.run,
        context,
    )
    await run_outcome_validators._validate_evidence(  # pyright: ignore[reportPrivateUsage]
        session,
        result.run,
        context,
    )
    await run_outcome_validators._validate_world(  # pyright: ignore[reportPrivateUsage]
        session,
        result.run,
        context,
        require_current=False,
    )
    return _NonterminalRunAuthority(result, row, command, job, context)


async def _validate_provider_failed_terminal_run(
    session: AsyncSession,
    authority: ValidatedRunAuthority,
    outcome: Mapping[str, Any],
) -> None:
    """Preserve an objective Run after final-role Provider failure, with zero projection."""

    final_decision = await session.scalar(
        select(JobStepReceiptRow).where(
            JobStepReceiptRow.tenant_id == authority.job.tenant_id,
            JobStepReceiptRow.job_id == authority.job.job_id,
            JobStepReceiptRow.step_name == "FINAL_DECISION_DERIVED",
        )
    )
    interactions = await session.scalar(
        select(func.count(ProductInteractionRow.interaction_id)).where(
            ProductInteractionRow.tenant_id == authority.job.tenant_id,
            ProductInteractionRow.session_id == authority.run.session_id,
            ProductInteractionRow.turn_id == authority.run.turn_id,
        )
    )
    learner_jobs = await session.scalar(
        select(func.count(LearnerProjectionJobRow.job_id)).where(
            LearnerProjectionJobRow.tenant_id == authority.job.tenant_id,
            LearnerProjectionJobRow.run_id == authority.run.run_id,
        )
    )
    if (
        not authority.command.terminal
        or authority.job.status != "DEAD_LETTER"
        or authority.job.last_error_json is None
        or authority.run_row.run_json.get("agent_feedback") is not None
        or final_decision is not None
        or interactions != 0
        or learner_jobs != 0
    ):
        raise WorkflowInvariantError(
            "failed final Provider Run published a Product/Learner authority"
        )
    provider_receipts = await load_final_provider_receipts(session, authority.job)
    _validate_failed_provider_receipts(provider_receipts)
    await _validate_failed_terminal_outcome(session, authority, outcome)


def _validate_failed_provider_receipts(
    provider_receipts: tuple[JobStepReceiptRow, ...],
) -> None:
    for receipt in provider_receipts:
        validated_provider_terminal_receipt(receipt)


async def _validate_failed_terminal_outcome(
    session: AsyncSession,
    authority: ValidatedRunAuthority,
    outcome: Mapping[str, Any],
) -> None:
    task_id = _text(outcome, "task_id")
    task = await load_task_snapshot(session, task_id, authority.context)
    request = _object(authority.turn.request_json, "Turn request")
    bindings = request.get("skill_bindings")
    input_value = request.get("input")
    if (
        not isinstance(bindings, list)
        or len(bindings) != 1
        or not isinstance(bindings[0], Mapping)
        or not isinstance(input_value, Mapping)
    ):
        raise WorkflowInvariantError("failed Provider Turn root is not canonical")
    try:
        skill_ref = SkillRef(**dict(bindings[0]))
    except (TypeError, ValueError) as error:
        raise WorkflowInvariantError("failed Provider Skill binding is invalid") from error
    expected_revision = request.get("expected_world_revision")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        raise WorkflowInvariantError("failed Provider World revision is invalid")
    root = GameEvent(
        event_id=(
            "gameevent_"
            + hashlib.sha256(authority.command.command_id.encode("utf-8")).hexdigest()[:24]
        ),
        event_type="run_skill_requested",
        student_id=authority.context.actor.actor_id,
        task_id=task_id,
        session_id=authority.turn.session_id,
        turn_id=authority.turn.turn_id,
        command_id=authority.command.command_id,
        occurred_at=authority.turn.created_at,
        expected_world_revision=expected_revision,
        skill_ref=skill_ref,
        payload={"input": dict(input_value)},
    )
    failure_count = 0
    if not authority.run.task_success:
        history = await list_validated_session_runs(
            session,
            session_id=authority.run.session_id,
            through_run_id=authority.run.run_id,
            context=authority.context,
        )
        by_command = {item.command_id: item for item in history}
        turns = list(
            (
                await session.scalars(
                    select(AgentTurnRow)
                    .where(
                        AgentTurnRow.tenant_id == authority.context.actor.tenant_id,
                        AgentTurnRow.actor_id == authority.context.actor.actor_id,
                        AgentTurnRow.session_id == authority.run.session_id,
                        AgentTurnRow.turn_sequence <= authority.turn.turn_sequence,
                    )
                    .order_by(AgentTurnRow.turn_sequence.desc())
                )
            ).all()
        )
        if not turns or turns[0].command_id != authority.run.command_id:
            raise WorkflowInvariantError(
                "failed Provider history does not begin at the current Run"
            )
        expected_sequence = authority.turn.turn_sequence
        for turn in turns:
            if turn.turn_sequence != expected_sequence:
                raise WorkflowInvariantError(
                    "failed Provider history turn sequence contains a gap"
                )
            expected_sequence -= 1
            prior = by_command.get(turn.command_id)
            if prior is None:
                break
            if (
                prior.task_success
                or prior.failure_key != authority.run.failure_key
                or prior.skill_ref != authority.run.skill_ref
                or prior.world_id != authority.run.world_id
            ):
                break
            failure_count += 1
        if failure_count < 1:
            raise WorkflowInvariantError("failed Provider failure suffix is empty")
    expected = derive_run_outcome_event(
        root_event=root,
        run=authority.run,
        task=task,
        failure_count=failure_count,
        occurred_at=canonical_outcome_occurred_at(authority),
    )
    if dict(outcome) != json_value(expected):
        raise WorkflowInvariantError("failed final Provider outcome differs from its Run")


def validate_evidence_document_authority(row: EvidenceRow) -> None:
    """Validate one stored Evidence row with the writer's exact document contract."""
    value = _object(row.evidence_json, "Evidence")
    reference = _object(value.get("evidence_ref"), "Evidence reference")
    source = _object(value.get("source"), "Evidence source")
    integrity = _object(value.get("integrity"), "Evidence integrity")
    payload = _object(value.get("payload"), "Evidence payload")
    subject = _object(value.get("subject"), "Evidence subject")
    try:
        recorded_at = _parse_timestamp(value.get("recorded_at"))
        _parse_timestamp(value.get("occurred_at"))
        _parse_timestamp(reference.get("created_at"))
    except (TypeError, ValueError) as error:
        raise WorkflowInvariantError("Evidence timestamps are invalid") from error
    if (
        set(value)
        != {
            "request_context",
            "evidence_ref",
            "subject",
            "source",
            "occurred_at",
            "recorded_at",
            "integrity",
            "payload",
            "related_evidence",
            "versions",
        }
        or set(source) != {"source_type", "source_id", "command_id", "world_id"}
        or set(integrity) != {"payload_sha256", "previous_evidence_sha256"}
        or set(subject) != {"learner_id"}
        or reference.get("evidence_id") != row.evidence_id
        or reference.get("sha256") != canonical_json_sha256(payload)
        or integrity.get("payload_sha256") != reference.get("sha256")
        or integrity.get("previous_evidence_sha256") is not None
        or source.get("command_id") != row.command_id
        or recorded_at != row.recorded_at
        or not _same_stored_origin(
            value,
            tenant_id=row.tenant_id,
            actor_id=row.actor_id,
            content_hash=row.content_hash,
        )
        or not isinstance(value.get("related_evidence"), list)
        or not isinstance(value.get("versions"), Mapping)
    ):
        raise WorkflowInvariantError("Evidence terminal/hash/source authority drifted")


def _validate_evidence_document(row: EvidenceRow) -> None:
    """Compatibility alias for existing authority tests and internal callers."""

    validate_evidence_document_authority(row)


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowInvariantError(f"{label} is not an object")
    return dict(value)


def _text(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise WorkflowInvariantError(f"{key} is not bounded text")
    return item


def _identifier(prefix: str, *parts: str) -> str:
    framed = "\x00".join((prefix, *parts)).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(framed).hexdigest()[:24]}"


def _same_origin(value: Mapping[str, Any], context: OperationContext) -> bool:
    request_context = value.get("request_context")
    if not isinstance(request_context, Mapping):
        return False
    actor, content = request_context.get("actor"), request_context.get("content_ref")
    return (
        isinstance(actor, Mapping)
        and isinstance(content, Mapping)
        and actor.get("tenant_id") == context.actor.tenant_id
        and actor.get("actor_id") == context.actor.actor_id
        and content.get("content_hash") == context.content_ref.content_hash
    )


def _same_stored_origin(
    value: Mapping[str, Any], *, tenant_id: str, actor_id: str, content_hash: str
) -> bool:
    request_context = value.get("request_context")
    if not isinstance(request_context, Mapping):
        return False
    actor, content = request_context.get("actor"), request_context.get("content_ref")
    return (
        isinstance(actor, Mapping)
        and isinstance(content, Mapping)
        and actor.get("tenant_id") == tenant_id
        and actor.get("actor_id") == actor_id
        and content.get("content_hash") == content_hash
    )


def _string(value: Mapping[str, Any], name: str) -> str:
    item = value[name]
    if not isinstance(item, str):
        raise TypeError(f"{name} must be a string")
    return item


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError("timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed


def _not_found(resource: str) -> Any:
    return _error("NOT_FOUND", "READ", f"{resource} not found")


def _invariant(message: str) -> Any:
    return _error("INVARIANT_VIOLATION", "READ", message)


def _error(code: str, stage: str, message: str) -> Any:
    from yaya_agent_contracts import ContractError, ErrorCategory

    category, message_key = {
        "NOT_FOUND": (ErrorCategory.VALIDATION, "resource.not_found"),
        "INVARIANT_VIOLATION": (ErrorCategory.INVARIANT, "system.invariant_violation"),
    }[code]
    return ContractError(
        code=code,
        category=category,
        retryable=False,
        user_message_key=message_key,
        stage=stage,
        message=message,
    )
