"""Walnut PostgreSQL authority for Run-derived A8 role outcomes."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    CommandRecord,
    CommandStatus,
    EvidenceRef,
    EvidenceType,
    Failure,
    LlmReply,
    OperationContext,
    RuntimeEventType,
    Success,
    canonical_json_sha256,
)
from yaya_agent_runtime import (
    AgentDecision,
    GameEvent,
    RoleRouter,
    RunOutcomeInvariantError,
    RunResultSnapshot,
    SkillInvocationResult,
    TaskSnapshot,
    derive_run_outcome_event,
    operation_context_sha256,
    provider_dispatch_id,
    side_effect_execution_id,
    world_commit_receipt_sha256,
)

from walnut_backend.adapters.postgres.skill_invocation import (
    invocation_result_from_receipt,
)

from .agent_trace_identity import (
    AGENT_TRACE_OPERATION,
    AGENT_TRACE_OUTCOME,
    AgentTraceIdentityError,
    agent_trace_audit_id,
)
from .durable_llm import validated_provider_result_data
from .learner_projection_jobs import LearnerProjectionInvariantError
from .models import (
    AgentSessionRow,
    AgentTurnRow,
    AuditRow,
    CommandRow,
    EventRow,
    EvidenceRow,
    IdempotencyReceiptRow,
    JobStepReceiptRow,
    LearnerProfileRow,
    LearnerProjectionJobRow,
    ProductContentUnitRow,
    ProductInteractionRow,
    ProductWorkspaceRow,
    RegistryEntryRow,
    RunRow,
    SkillArtifactRow,
    SkillBuildRow,
    SkillCertificationRow,
    SkillRunProvenanceRow,
    WorkflowJobRow,
    WorldSnapshotRow,
    command_record_from_data,
    domain_event_data,
    domain_event_from_data,
    json_value,
    public_domain_event_data,
    request_context_data,
    request_context_from_data,
    world_snapshot_from_data,
)
from .skill_provenance import validate_run_provenance
from .workflow_jobs import (
    ClaimedWorkflowJob,
    PostgresWorkflowJobStore,
    WorkflowBoundaryError,
    WorkflowInvariantError,
    workflow_json_sha256,
    workflow_receipt_sha256,
    workflow_step_receipt_id,
)
from .world import world_commit_identifier as _world_commit_identifier

_OUTCOME_STEP = "OUTCOME_DERIVED"
_FINAL_DECISION_STEP = "FINAL_DECISION_DERIVED"


@dataclass(frozen=True, slots=True)
class ValidatedRunAuthority:
    """Typed Run plus the Walnut rows that prove its immutable identity."""

    result: SkillInvocationResult
    run_row: RunRow
    command: CommandRecord
    job: WorkflowJobRow
    turn: AgentTurnRow
    context: OperationContext
    run_provenance: SkillRunProvenanceRow

    def __post_init__(self) -> None:
        raw = self.result.run
        provenance = self.run_provenance
        actor = raw.request_context.actor
        if (
            not isinstance(provenance, SkillRunProvenanceRow)
            or provenance.run_id != raw.run_id
            or provenance.tenant_id != actor.tenant_id
            or provenance.actor_id != actor.actor_id
            or provenance.session_id != raw.session_id
            or provenance.certification_id != raw.skill_ref.certification_id
            or provenance.artifact_sha256 != raw.skill_ref.artifact_sha256
            or (raw.build_id is not None and raw.build_id != provenance.build_id)
        ):
            raise WorkflowInvariantError("Run provenance differs from immutable Run facts")

    @property
    def run(self) -> RunResultSnapshot:
        # SKILL_INVOKED receipts predate the public/context build_id field and
        # remain byte-stable in ``result``.  Every context read uses this one
        # enriched view, derived only from the already-validated immutable
        # Run-to-Build provenance row.
        return replace(self.result.run, build_id=self.run_provenance.build_id)


@dataclass(slots=True)
class TerminalProjectionValidationState:
    """One request/transaction's recursion guard and successful validation memo."""

    session: object | None = field(default=None, repr=False)
    in_progress: set[tuple[str, ...]] = field(default_factory=set)
    completed: set[tuple[str, ...]] = field(default_factory=set)
    validated_run_in_progress: set[tuple[Any, ...]] = field(default_factory=set)
    validated_runs: dict[tuple[Any, ...], ValidatedRunAuthority] = field(default_factory=dict)
    canonical_outcome_in_progress: set[tuple[str, ...]] = field(default_factory=set)
    canonical_outcomes: dict[tuple[str, ...], GameEvent] = field(default_factory=dict)

    def bind_session(self, session: object) -> None:
        if self.session is None:
            self.session = session
        elif self.session is not session:
            raise WorkflowInvariantError(
                "terminal projection validation state crossed its database session"
            )


def _terminal_projection_validation_key(
    authority: ValidatedRunAuthority,
) -> tuple[str, ...]:
    return (
        authority.job.tenant_id,
        authority.context.actor.actor_id,
        authority.context.content_ref.content_hash,
        authority.run.session_id,
        authority.run.turn_id,
        authority.run.run_id,
        authority.run.command_id,
    )


def _validated_run_load_key(
    *,
    tenant_id: str,
    actor_id: str,
    content_hash: str,
    command_id: str,
    expected_context: OperationContext,
    require_current_world: bool,
) -> tuple[Any, ...]:
    actor = expected_context.actor
    content = expected_context.content_ref
    return (
        tenant_id,
        actor_id,
        content_hash,
        command_id,
        require_current_world,
        expected_context.command_id,
        actor.tenant_id,
        actor.actor_id,
        actor.actor_type,
        tuple(actor.roles),
        content.unit_id,
        content.version,
        content.content_hash,
    )


@dataclass(frozen=True, slots=True)
class _FinalProviderRequestHistory:
    successful_tool_rounds: int
    invalid_attempts: int


@dataclass(frozen=True, slots=True)
class _FinalProviderReceiptItem:
    kind: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    decision: Mapping[str, Any] | None = None
    tool_calls: tuple[Mapping[str, Any], ...] = ()


class PostgresRunOutcomeAuthority:
    """Derive one final-role event only from Walnut-owned durable facts."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        jobs: PostgresWorkflowJobStore,
        *,
        lease_seconds: int,
    ) -> None:
        if not 30 <= lease_seconds <= 3600:
            raise ValueError("Run outcome lease must be between 30 and 3600 seconds")
        self._sessions = session_factory
        self._jobs = jobs
        self._lease_seconds = lease_seconds

    async def derive(
        self,
        claim: ClaimedWorkflowJob,
        *,
        root_event: GameEvent,
        context: OperationContext,
    ) -> GameEvent:
        if claim.operation != "EXECUTE_AGENT_TURN" or claim.subject_type != "AGENT_TURN":
            raise ValueError("Run outcome requires one Agent Turn claim")
        async with self._sessions() as session, session.begin():
            owned = await self._jobs.start_step_in_session(
                session,
                claim,
                phase="OUTCOME_AUTHORITY",
                lease_seconds=self._lease_seconds,
            )
            current = await load_validated_run(
                session,
                tenant_id=owned.tenant_id,
                actor_id=context.actor.actor_id,
                content_hash=context.content_ref.content_hash,
                command_id=owned.command_id,
                expected_context=context,
                require_current_world=True,
            )
            _validate_live_root(owned, current, root_event, context)
            task = await load_task_snapshot(session, root_event.task_id, context)
            failure_count = (
                0
                if current.run.task_success
                else await exact_failure_suffix_count(
                    session,
                    current=current,
                    context=context,
                )
            )
            try:
                outcome = derive_run_outcome_event(
                    root_event=root_event,
                    run=current.run,
                    task=task,
                    failure_count=failure_count,
                    occurred_at=canonical_outcome_occurred_at(current),
                )
            except RunOutcomeInvariantError as error:
                raise WorkflowInvariantError(
                    "Run outcome derivation rejected durable authority"
                ) from error
            await validate_canonical_outcome_event(
                session,
                authority=current,
                outcome=cast(dict[str, Any], json_value(outcome)),
            )
            output = _outcome_receipt_data(outcome, current)
            await self._jobs.record_step_in_session(
                session,
                owned,
                step_name=_OUTCOME_STEP,
                input_sha256=current.result.request_sha256,
                output=output,
            )
            return outcome

    async def record_final_decision(
        self,
        claim: ClaimedWorkflowJob,
        *,
        outcome: GameEvent,
        decision: AgentDecision,
        result: SkillInvocationResult,
        context: OperationContext,
    ) -> None:
        """Freeze the provider-derived final decision before Product projection."""

        async with self._sessions() as session, session.begin():
            owned = await self._jobs.start_step_in_session(
                session,
                claim,
                phase="FINAL_DECISION_AUTHORITY",
                lease_seconds=self._lease_seconds,
            )
            try:
                current = await load_validated_run(
                    session,
                    tenant_id=owned.tenant_id,
                    actor_id=context.actor.actor_id,
                    content_hash=context.content_ref.content_hash,
                    command_id=owned.command_id,
                    expected_context=context,
                    require_current_world=True,
                )
            except WorkflowInvariantError as error:
                raise WorkflowBoundaryError("FINAL_DECISION_LOAD_RUN") from error
            try:
                outcome_receipt = await _step_receipt(session, current.job, _OUTCOME_STEP)
                expected_outcome = _outcome_receipt_data(outcome, current)
                await validate_canonical_outcome_event(
                    session,
                    authority=current,
                    outcome=cast(dict[str, Any], json_value(outcome)),
                )
                if (
                    outcome_receipt is None
                    or outcome_receipt.input_sha256 != result.request_sha256
                    or outcome_receipt.output_sha256 != workflow_receipt_sha256(expected_outcome)
                    or outcome_receipt.receipt_json != expected_outcome
                ):
                    raise WorkflowInvariantError("final decision has no exact Run outcome receipt")
            except WorkflowInvariantError as error:
                raise WorkflowBoundaryError("OUTCOME_AUTHORITY") from error
            try:
                _validate_final_decision(outcome, decision, current.run)
            except WorkflowInvariantError as error:
                raise WorkflowBoundaryError("FINAL_DECISION_SHAPE") from error
            try:
                provider_receipts = await load_final_provider_receipts(session, current.job)
            except WorkflowInvariantError as error:
                raise WorkflowBoundaryError("PROVIDER_RECEIPT_HISTORY") from error
            try:
                await validate_agent_decision_runtime_authority(
                    session,
                    authority=current,
                    receipts=provider_receipts,
                    decision=cast(dict[str, Any], json_value(decision)),
                )
            except WorkflowInvariantError as error:
                raise WorkflowBoundaryError("RUNTIME_TRACE_AUTHORITY") from error
            try:
                validate_provider_decision_wire(
                    provider_receipts,
                    decision_draft=cast(dict[str, Any], json_value(decision.draft)),
                    evidence_refs=decision.evidence_refs,
                    decision=cast(dict[str, Any], json_value(decision)),
                )
            except WorkflowInvariantError as error:
                raise WorkflowBoundaryError("PROVIDER_DECISION_WIRE") from error
            try:
                output = _final_decision_receipt_data(
                    outcome,
                    decision,
                    result,
                    provider_receipts,
                )
                outcome_sha256 = cast(str, output["outcome_sha256"])
                await self._jobs.record_step_in_session(
                    session,
                    owned,
                    step_name=_FINAL_DECISION_STEP,
                    input_sha256=outcome_sha256,
                    output=output,
                )
            except WorkflowInvariantError as error:
                raise WorkflowBoundaryError("RECORD_RECEIPT") from error


async def load_task_snapshot(
    session: AsyncSession,
    task_id: str,
    context: OperationContext,
) -> TaskSnapshot:
    content = await session.scalar(
        select(ProductContentUnitRow).where(
            ProductContentUnitRow.tenant_id == context.actor.tenant_id,
            ProductContentUnitRow.unit_id == context.content_ref.unit_id,
            ProductContentUnitRow.version == context.content_ref.version,
            ProductContentUnitRow.content_hash == context.content_ref.content_hash,
        )
    )
    if content is None:
        raise WorkflowInvariantError("pinned ContentUnit is missing")
    task = _object(content.content_json.get("task"), "Content task")
    story = _object(task.get("story"), "Content task story")
    hints = _object(task.get("hint_policy"), "Content hint policy")
    if _text(task, "task_id") != task_id:
        raise WorkflowInvariantError("Content task differs from the accepted Turn")
    return TaskSnapshot(
        task_id=task_id,
        title=_text(task, "name"),
        goal=_text(task, "goal"),
        story=cast(str, story.get("opening", "")),
        knowledge_points=_strings(task.get("knowledge_points"), "knowledge_points"),
        request_context=_request_context(context),
        max_hint_level=_integer(hints, "max_level"),
    )


async def load_validated_run(
    session: AsyncSession,
    *,
    tenant_id: str,
    actor_id: str,
    content_hash: str,
    command_id: str,
    expected_context: OperationContext,
    require_current_world: bool,
    validation_state: TerminalProjectionValidationState | None = None,
) -> ValidatedRunAuthority:
    """Load one exact Run once per request-local validation state."""

    if validation_state is None:
        return await _load_validated_run_uncached(
            session,
            tenant_id=tenant_id,
            actor_id=actor_id,
            content_hash=content_hash,
            command_id=command_id,
            expected_context=expected_context,
            require_current_world=require_current_world,
        )
    validation_state.bind_session(session)
    key = _validated_run_load_key(
        tenant_id=tenant_id,
        actor_id=actor_id,
        content_hash=content_hash,
        command_id=command_id,
        expected_context=expected_context,
        require_current_world=require_current_world,
    )
    cached = validation_state.validated_runs.get(key)
    if cached is not None:
        return cached
    if key in validation_state.validated_run_in_progress:
        raise WorkflowInvariantError("validated Run load cycle detected")
    validation_state.validated_run_in_progress.add(key)
    try:
        authority = await _load_validated_run_uncached(
            session,
            tenant_id=tenant_id,
            actor_id=actor_id,
            content_hash=content_hash,
            command_id=command_id,
            expected_context=expected_context,
            require_current_world=require_current_world,
        )
    except BaseException:
        raise
    else:
        validation_state.validated_runs[key] = authority
        return authority
    finally:
        validation_state.validated_run_in_progress.discard(key)


async def _load_validated_run_uncached(
    session: AsyncSession,
    *,
    tenant_id: str,
    actor_id: str,
    content_hash: str,
    command_id: str,
    expected_context: OperationContext,
    require_current_world: bool,
) -> ValidatedRunAuthority:
    """Load and close one Run row against its command, job and step receipt."""

    command_row = await session.scalar(
        select(CommandRow).where(
            CommandRow.tenant_id == tenant_id,
            CommandRow.command_id == command_id,
        )
    )
    job = await session.scalar(
        select(WorkflowJobRow).where(
            WorkflowJobRow.tenant_id == tenant_id,
            WorkflowJobRow.command_id == command_id,
            WorkflowJobRow.operation == "EXECUTE_AGENT_TURN",
            WorkflowJobRow.subject_type == "AGENT_TURN",
        )
    )
    turn = await session.scalar(
        select(AgentTurnRow).where(
            AgentTurnRow.tenant_id == tenant_id,
            AgentTurnRow.actor_id == actor_id,
            AgentTurnRow.command_id == command_id,
        )
    )
    command_receipt = await session.scalar(
        select(IdempotencyReceiptRow).where(
            IdempotencyReceiptRow.tenant_id == tenant_id,
            IdempotencyReceiptRow.actor_id == actor_id,
            IdempotencyReceiptRow.operation == "EXECUTE_AGENT_TURN",
            IdempotencyReceiptRow.command_id == command_id,
        )
    )
    receipt = None
    if job is not None:
        receipt = await session.scalar(
            select(JobStepReceiptRow).where(
                JobStepReceiptRow.tenant_id == tenant_id,
                JobStepReceiptRow.job_id == job.job_id,
                JobStepReceiptRow.step_name == "SKILL_INVOKED",
            )
        )
    if (
        command_row is None
        or command_receipt is None
        or job is None
        or turn is None
        or receipt is None
    ):
        raise WorkflowInvariantError("Run Command/Job/Turn/invocation authority is incomplete")
    command = command_record_from_data(command_row.record_json)
    _validate_command_context(command, expected_context)
    command_context = _operation_context(command)
    if (
        job.command_id != command.command_id
        or job.subject_id != turn.turn_id
        or turn.command_id != command.command_id
        or turn.session_id != cast(str, job.job_json.get("session_id"))
        or command_receipt.command_id != command.command_id
        or command_receipt.operation != command.command_type
        or command_receipt.accepted_at != command.accepted_at
        or turn.created_at != command.accepted_at
        or job.request_sha256 != command_receipt.request_sha256
        or job.job_json.get("request") != turn.request_json
        or job.job_json.get("request_context") != request_context_data(command.request_context)
        or receipt.receipt_id != workflow_step_receipt_id(tenant_id, job.job_id, "SKILL_INVOKED")
        or receipt.fencing_token < 1
        or receipt.fencing_token > job.fencing_token
        or receipt.output_sha256 != workflow_receipt_sha256(receipt.receipt_json)
    ):
        raise WorkflowInvariantError("Run workflow identity or invocation receipt drifted")
    try:
        result = invocation_result_from_receipt(receipt.receipt_json)
    except (TypeError, ValueError) as error:
        raise WorkflowInvariantError("SKILL_INVOKED receipt is not canonical") from error
    run = result.run
    run_row = await session.scalar(
        select(RunRow).where(
            RunRow.tenant_id == tenant_id,
            RunRow.actor_id == actor_id,
            RunRow.content_hash == content_hash,
            RunRow.run_id == run.run_id,
            RunRow.command_id == command_id,
        )
    )
    if run_row is None:
        raise WorkflowInvariantError("SKILL_INVOKED receipt has no exact Run row")
    run_provenance = await session.scalar(
        select(SkillRunProvenanceRow).where(
            SkillRunProvenanceRow.run_id == run.run_id,
            SkillRunProvenanceRow.tenant_id == tenant_id,
            SkillRunProvenanceRow.actor_id == actor_id,
            SkillRunProvenanceRow.session_id == turn.session_id,
        )
    )
    if run_provenance is None or await validate_run_provenance(session, run_provenance) is None:
        raise WorkflowInvariantError("Run provenance is missing or corrupt")
    if (
        receipt.input_sha256 != result.request_sha256
        or result.tenant_id != tenant_id
        or result.invocation_id != side_effect_execution_id(command_id, turn.turn_id)
        or run.session_id != turn.session_id
        or run.turn_id != turn.turn_id
        or run.command_id != command_id
        or not _same_actor(run.request_context.actor, command_context.actor)
        or run.request_context.content_ref != command_context.content_ref
    ):
        raise WorkflowInvariantError("SKILL_INVOKED receipt differs from Turn authority")
    _validate_run_row(run_row, run, command_context)
    await _validate_evidence(session, run, command_context)
    await _validate_world(
        session,
        run,
        command_context,
        require_current=require_current_world,
    )
    return ValidatedRunAuthority(
        result,
        run_row,
        command,
        job,
        turn,
        command_context,
        run_provenance,
    )


async def exact_failure_suffix_count(
    session: AsyncSession,
    *,
    current: ValidatedRunAuthority,
    context: OperationContext,
    current_must_be_live: bool = True,
    validation_state: TerminalProjectionValidationState | None = None,
) -> int:
    """Prove the contiguous same-Skill/same-failure suffix through current."""

    validation_state = validation_state or TerminalProjectionValidationState()
    validation_state.bind_session(session)
    run = current.run
    if run.task_success or run.failure_key is None:
        raise WorkflowInvariantError("failure suffix requested for a successful Run")
    matching_run_commands = (
        select(RunRow.command_id.label("command_id"))
        .where(
            RunRow.tenant_id == context.actor.tenant_id,
            RunRow.actor_id == context.actor.actor_id,
            RunRow.content_hash == context.content_ref.content_hash,
        )
        .distinct()
        .subquery()
    )
    turn_rows = list(
        (
            await session.execute(
                select(
                    AgentTurnRow,
                    matching_run_commands.c.command_id,
                )
                .outerjoin(
                    matching_run_commands,
                    matching_run_commands.c.command_id == AgentTurnRow.command_id,
                )
                .where(
                    AgentTurnRow.tenant_id == context.actor.tenant_id,
                    AgentTurnRow.actor_id == context.actor.actor_id,
                    AgentTurnRow.session_id == run.session_id,
                    AgentTurnRow.turn_sequence <= current.turn.turn_sequence,
                )
                .order_by(AgentTurnRow.turn_sequence.desc())
            )
        ).all()
    )
    if not turn_rows or turn_rows[0][0].command_id != run.command_id:
        raise WorkflowInvariantError("failure history does not begin at the current Run")
    count = 0
    expected_sequence = current.turn.turn_sequence
    for index, (turn, run_command_id) in enumerate(turn_rows):
        if turn.turn_sequence != expected_sequence:
            raise WorkflowInvariantError("failure history turn sequence contains a gap")
        expected_sequence -= 1
        if index > 0 and run_command_id is None:
            break
        authority = (
            current
            if index == 0
            else await load_validated_run(
                session,
                tenant_id=context.actor.tenant_id,
                actor_id=context.actor.actor_id,
                content_hash=context.content_ref.content_hash,
                command_id=turn.command_id,
                expected_context=_context_for_command(session, context, turn.command_id),
                require_current_world=False,
                validation_state=validation_state,
            )
        )
        if index == 0:
            if current_must_be_live:
                if authority.command.terminal or authority.command.status not in {
                    CommandStatus.RUNNING_SANDBOX,
                    CommandStatus.APPLYING_WORLD,
                }:
                    raise WorkflowInvariantError("current failure Command is unexpectedly terminal")
            else:
                _validate_terminal_command(authority)
        else:
            _validate_terminal_command(authority)
            await validate_terminal_projection(
                session,
                authority,
                validation_state=validation_state,
            )
        prior = authority.run
        if (
            prior.task_success
            or prior.failure_key != run.failure_key
            or prior.skill_ref != run.skill_ref
            or prior.world_id != run.world_id
        ):
            break
        count += 1
    if count < 1:
        raise WorkflowInvariantError("canonical failure suffix is empty")
    return count


async def list_validated_session_runs(
    session: AsyncSession,
    *,
    session_id: str,
    through_run_id: str,
    context: OperationContext,
    validation_state: TerminalProjectionValidationState | None = None,
) -> tuple[RunResultSnapshot, ...]:
    validation_state = validation_state or TerminalProjectionValidationState()
    validation_state.bind_session(session)
    turns = list(
        (
            await session.scalars(
                select(AgentTurnRow)
                .join(
                    RunRow,
                    (RunRow.tenant_id == AgentTurnRow.tenant_id)
                    & (RunRow.actor_id == AgentTurnRow.actor_id)
                    & (RunRow.command_id == AgentTurnRow.command_id),
                )
                .where(
                    AgentTurnRow.tenant_id == context.actor.tenant_id,
                    AgentTurnRow.actor_id == context.actor.actor_id,
                    AgentTurnRow.session_id == session_id,
                )
                .order_by(AgentTurnRow.turn_sequence)
                .limit(201)
            )
        ).all()
    )
    if len(turns) > 200:
        raise WorkflowInvariantError("Session Run history exceeds the bounded contract")
    result: list[RunResultSnapshot] = []
    found = False
    previous_sequence = 0
    for turn in turns:
        if turn.turn_sequence <= previous_sequence:
            raise WorkflowInvariantError("Session Run history is not strictly ordered")
        previous_sequence = turn.turn_sequence
        authority = await load_validated_run(
            session,
            tenant_id=context.actor.tenant_id,
            actor_id=context.actor.actor_id,
            content_hash=context.content_ref.content_hash,
            command_id=turn.command_id,
            expected_context=_context_for_command(session, context, turn.command_id),
            require_current_world=False,
            validation_state=validation_state,
        )
        if authority.run.run_id != through_run_id:
            _validate_terminal_command(authority)
            await validate_terminal_projection(
                session,
                authority,
                validation_state=validation_state,
            )
        result.append(authority.run)
        if authority.run.run_id == through_run_id:
            found = True
            break
    if not found:
        raise WorkflowInvariantError("Session Run history does not reach through_run_id")
    return tuple(result)


async def validate_final_decision_receipt(
    session: AsyncSession,
    *,
    job: WorkflowJobRow,
    outcome: GameEvent,
    decision: AgentDecision,
    result: SkillInvocationResult,
) -> JobStepReceiptRow:
    """Close a live final decision over its immutable Provider result receipts."""

    if result.run.command_id != job.command_id:
        raise WorkflowInvariantError("final decision Run differs from its workflow Job")
    _validate_final_decision(outcome, decision, result.run)
    provider_receipts = await load_final_provider_receipts(session, job)
    raw_context = job.job_json.get("request_context")
    if not isinstance(raw_context, dict):
        raise WorkflowInvariantError("final decision Job lost its OperationContext")
    expected_context = _job_operation_context(job)
    current = await load_validated_run(
        session,
        tenant_id=job.tenant_id,
        actor_id=expected_context.actor.actor_id,
        content_hash=expected_context.content_ref.content_hash,
        command_id=job.command_id,
        expected_context=expected_context,
        require_current_world=False,
    )
    await _validate_provider_decision(
        session,
        authority=current,
        receipts=provider_receipts,
        decision=decision,
    )
    expected = _final_decision_receipt_data(
        outcome,
        decision,
        result,
        provider_receipts,
    )
    receipt = await _step_receipt(session, job, _FINAL_DECISION_STEP)
    outcome_sha256 = canonical_json_sha256(cast(dict[str, Any], json_value(outcome)))
    if (
        receipt is None
        or receipt.input_sha256 != outcome_sha256
        or receipt.output_sha256 != workflow_receipt_sha256(expected)
        or receipt.receipt_json != expected
        or receipt.fencing_token < 1
        or receipt.fencing_token > job.fencing_token
    ):
        raise WorkflowInvariantError("final decision receipt is missing or corrupt")
    return receipt


async def validate_canonical_outcome_event(
    session: AsyncSession,
    *,
    authority: ValidatedRunAuthority,
    outcome: Mapping[str, Any],
    validation_state: TerminalProjectionValidationState | None = None,
) -> GameEvent:
    """Re-derive each exact canonical outcome once in one recursive request."""

    state = validation_state or TerminalProjectionValidationState()
    state.bind_session(session)
    key = (
        *_terminal_projection_validation_key(authority),
        canonical_json_sha256(outcome),
    )
    cached = state.canonical_outcomes.get(key)
    if cached is not None:
        return cached
    if key in state.canonical_outcome_in_progress:
        raise WorkflowInvariantError("canonical outcome validation cycle detected")
    state.canonical_outcome_in_progress.add(key)
    try:
        event = await _validate_canonical_outcome_event_uncached(
            session,
            authority=authority,
            outcome=outcome,
            validation_state=state,
        )
    except BaseException:
        raise
    else:
        state.canonical_outcomes[key] = event
        return event
    finally:
        state.canonical_outcome_in_progress.discard(key)


async def _validate_canonical_outcome_event_uncached(
    session: AsyncSession,
    *,
    authority: ValidatedRunAuthority,
    outcome: Mapping[str, Any],
    validation_state: TerminalProjectionValidationState,
) -> GameEvent:
    """Re-derive the exact A8 outcome from the current durable Run graph."""

    validation_state.bind_session(session)
    task_id = await _durable_task_id(session, authority)
    task = await load_task_snapshot(session, task_id, authority.context)
    root = _durable_root_event(authority, task_id)
    if authority.command.terminal:
        _validate_terminal_command(authority)
    elif authority.command.status not in {
        CommandStatus.RUNNING_SANDBOX,
        CommandStatus.APPLYING_WORLD,
    }:
        raise WorkflowInvariantError("outcome authority has no executable or terminal Command")
    failure_count = (
        0
        if authority.run.task_success
        else await exact_failure_suffix_count(
            session,
            current=authority,
            context=authority.context,
            current_must_be_live=not authority.command.terminal,
            validation_state=validation_state,
        )
    )
    expected = derive_run_outcome_event(
        root_event=root,
        run=authority.run,
        task=task,
        failure_count=failure_count,
        occurred_at=canonical_outcome_occurred_at(authority),
    )
    if dict(outcome) != cast(dict[str, Any], json_value(expected)):
        raise WorkflowInvariantError("Run outcome event differs from its canonical suffix")
    return expected


def canonical_outcome_occurred_at(authority: ValidatedRunAuthority) -> datetime:
    """Return the earliest valid time at which all Run Evidence already exists."""

    return max(
        authority.run_row.created_at,
        *(reference.created_at for reference in authority.run.evidence_refs),
    )


async def _durable_task_id(
    session: AsyncSession,
    authority: ValidatedRunAuthority,
) -> str:
    content = await session.scalar(
        select(ProductContentUnitRow).where(
            ProductContentUnitRow.tenant_id == authority.context.actor.tenant_id,
            ProductContentUnitRow.unit_id == authority.context.content_ref.unit_id,
            ProductContentUnitRow.version == authority.context.content_ref.version,
            ProductContentUnitRow.content_hash == authority.context.content_ref.content_hash,
        )
    )
    if content is None:
        raise WorkflowInvariantError("outcome authority lost its pinned Content task")
    return _text(_object(content.content_json.get("task"), "Content task"), "task_id")


def _durable_root_event(
    authority: ValidatedRunAuthority,
    task_id: str,
) -> GameEvent:
    request = authority.turn.request_json
    bindings = request.get("skill_bindings")
    client_state = request.get("client_state")
    input_value = request.get("input")
    job_request = authority.job.job_json.get("request")
    if (
        request.get("turn_id") != authority.turn.turn_id
        or authority.job.job_json.get("turn_id") != authority.turn.turn_id
        or authority.job.job_json.get("session_id") != authority.turn.session_id
        or authority.job.job_json.get("turn_sequence") != authority.turn.turn_sequence
        or job_request != request
        or not isinstance(bindings, list)
        or len(bindings) != 1
        or not isinstance(bindings[0], Mapping)
        or not isinstance(client_state, Mapping)
        or client_state.get("client_turn_sequence") != authority.turn.turn_sequence
        or not isinstance(input_value, Mapping)
    ):
        raise WorkflowInvariantError("outcome root Turn request authority drifted")
    try:
        skill_ref = authority.run.skill_ref.__class__(**dict(bindings[0]))
    except (TypeError, ValueError) as error:
        raise WorkflowInvariantError("outcome root Skill binding is not canonical") from error
    expected_revision = _integer(request, "expected_world_revision")
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
    if (
        skill_ref != authority.run.skill_ref
        or expected_revision != authority.run.world_revision_before
    ):
        raise WorkflowInvariantError("outcome root Turn differs from its Run")
    return root


def _outcome_receipt_data(
    outcome: GameEvent,
    authority: ValidatedRunAuthority,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "event": cast(dict[str, Any], json_value(outcome)),
        "run_sha256": run_authority_sha256(authority.run_row.run_json),
        "invocation_request_sha256": authority.result.request_sha256,
    }


def _final_decision_receipt_data(
    outcome: GameEvent,
    decision: AgentDecision,
    result: SkillInvocationResult,
    provider_receipts: Sequence[JobStepReceiptRow],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "outcome_event_id": outcome.event_id,
        "outcome_sha256": canonical_json_sha256(cast(dict[str, Any], json_value(outcome))),
        "run_id": result.run.run_id,
        "invocation_request_sha256": result.request_sha256,
        "provider_result_receipts": [
            {
                "receipt_id": receipt.receipt_id,
                "step_name": receipt.step_name,
                "output_sha256": receipt.output_sha256,
            }
            for receipt in provider_receipts
        ],
        "decision": cast(dict[str, Any], json_value(decision)),
    }


def decision_feedback_wire(
    decision: Mapping[str, Any],
    run: RunResultSnapshot,
    *,
    expected_completed_at: str | None = None,
) -> dict[str, Any]:
    """Derive the one public feedback payload from a frozen final decision."""

    draft = _object(decision.get("draft"), "final Agent decision draft")
    expected_keys = {
        "draft",
        "message_key",
        "source",
        "degraded",
        "fallback_reason",
        "provider",
        "model",
        "input_tokens",
        "output_tokens",
        "tool_calls",
        "evidence_refs",
        "completed_at",
        "runtime_warnings",
        "teaching_directive",
    }
    if (
        set(decision) != expected_keys
        or decision.get("message_key") != f"agent.{draft.get('role')}.{draft.get('response_type')}"
        or decision.get("source") != "provider"
        or decision.get("degraded") is not False
        or decision.get("fallback_reason") is not None
        or decision.get("evidence_refs")
        != [_decision_evidence_ref_wire(item) for item in run.evidence_refs]
        or not isinstance(decision.get("completed_at"), str)
        or (
            expected_completed_at is not None
            and decision.get("completed_at") != expected_completed_at
        )
        or not _runtime_warnings_shape(decision.get("runtime_warnings"))
    ):
        raise WorkflowInvariantError("final Agent decision cannot derive exact feedback")
    return {
        "session_id": run.session_id,
        "turn_id": run.turn_id,
        "command_id": run.command_id,
        "run_id": run.run_id,
        "message_key": decision.get("message_key"),
        "message": draft.get("message"),
        "source": decision.get("source"),
        "degraded": decision.get("degraded"),
        "fallback_reason": decision.get("fallback_reason"),
        "evidence_refs": [_evidence_ref_wire(item) for item in run.evidence_refs],
        "completed_at": _iso(_timestamp(cast(str, decision.get("completed_at")))),
    }


_FINAL_RUNTIME_WARNINGS = frozenset(
    {
        "TRACE_WRITE_FAILED",
        "TRACE_TOOL_STARTED_WRITE_FAILED",
        "TRACE_TOOL_SUCCEEDED_WRITE_FAILED",
    }
)


def _runtime_warnings_shape(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == len(set(cast(list[object], value)))
        and all(item in _FINAL_RUNTIME_WARNINGS for item in value)
    )


async def validate_agent_decision_runtime_authority(
    session: AsyncSession,
    *,
    authority: ValidatedRunAuthority,
    receipts: Sequence[JobStepReceiptRow],
    decision: Mapping[str, Any],
    validation_state: TerminalProjectionValidationState | None = None,
) -> None:
    """Close final runtime warnings and tool records over durable trace/read facts."""

    validation_state = validation_state or TerminalProjectionValidationState()
    validation_state.bind_session(session)
    draft = _object(decision.get("draft"), "final Agent decision draft")
    role = _text(draft, "role")
    warnings = decision.get("runtime_warnings")
    tools = decision.get("tool_calls")
    if not _runtime_warnings_shape(warnings) or not isinstance(tools, list):
        raise WorkflowInvariantError("final Agent runtime authority is not canonical")
    provider_history = tuple(_parse_final_provider_receipt(receipt) for receipt in receipts)
    history = _classify_final_provider_request_history(provider_history, tools)
    expected_audit_ids = _expected_agent_trace_audit_ids(
        authority=authority,
        role=role,
        receipt_count=len(receipts),
        invalid_attempts=history.invalid_attempts,
        tools=tools,
    )
    rows = list(
        (
            await session.scalars(
                select(AuditRow).where(
                    AuditRow.tenant_id == authority.job.tenant_id,
                    AuditRow.operation == AGENT_TRACE_OPERATION,
                    or_(
                        AuditRow.audit_id.in_(expected_audit_ids),
                        and_(
                            AuditRow.record_json["command_id"].astext == authority.job.command_id,
                            AuditRow.record_json["turn_id"].astext == authority.turn.turn_id,
                            AuditRow.record_json["role"].astext == role,
                        ),
                    ),
                )
            )
        ).all()
    )
    traces: list[dict[str, Any]] = []
    for row in rows:
        trace = dict(row.record_json)
        try:
            expected_audit_id = agent_trace_audit_id(authority.job.tenant_id, trace)
        except AgentTraceIdentityError as error:
            raise WorkflowInvariantError("final Agent trace identity drifted") from error
        if (
            row.audit_id != expected_audit_id
            or row.tenant_id != authority.job.tenant_id
            or row.operation != AGENT_TRACE_OPERATION
            or row.outcome != AGENT_TRACE_OUTCOME
            or set(trace) != {"name", "turn_id", "role", "fields", "command_id", "trace_id"}
            or trace.get("turn_id") != authority.turn.turn_id
            or trace.get("role") != role
            or trace.get("command_id") != authority.job.command_id
            or trace.get("trace_id") != authority.context.trace_id
            or not isinstance(trace.get("name"), str)
            or not isinstance(trace.get("fields"), Mapping)
        ):
            raise WorkflowInvariantError("final Agent trace record drifted")
        traces.append(trace)
    expected_core = {
        "agent.turn.started": 1,
        "agent.model.requested": len(receipts),
        "agent.output.invalid": history.invalid_attempts,
        "agent.turn.finished": 1,
    }
    actual_core = {name: sum(trace["name"] == name for trace in traces) for name in expected_core}
    request_numbers = [
        cast(Mapping[str, Any], trace["fields"]).get("request_number")
        for trace in traces
        if trace["name"] == "agent.model.requested"
    ]
    repair_attempts = [
        cast(Mapping[str, Any], trace["fields"]).get("repair_attempt")
        for trace in traces
        if trace["name"] == "agent.output.invalid"
    ]
    expected_request_numbers = set(range(1, len(receipts) + 1))
    expected_repair_attempts = set(range(1, history.invalid_attempts + 1))
    if (
        any(isinstance(item, bool) or not isinstance(item, int) for item in request_numbers)
        or len(request_numbers) != len(set(request_numbers))
        or not set(request_numbers).issubset(expected_request_numbers)
        or any(isinstance(item, bool) or not isinstance(item, int) for item in repair_attempts)
        or len(repair_attempts) != len(set(repair_attempts))
        or not set(repair_attempts).issubset(expected_repair_attempts)
    ):
        raise WorkflowInvariantError("final Agent trace occurrence identity drifted")
    core_missing = any(actual_core[name] < count for name, count in expected_core.items())
    core_extra = any(actual_core[name] > count for name, count in expected_core.items())
    warning_set = set(cast(list[str], warnings))
    if core_extra or ("TRACE_WRITE_FAILED" in warning_set) != core_missing:
        raise WorkflowInvariantError("final Agent runtime warning/trace closure drifted")

    started_missing = False
    succeeded_missing = False
    expected_tool_traces: set[tuple[str, str]] = set()
    for ordinal, raw in enumerate(tools, start=1):
        tool = _object(raw, "final Agent tool record")
        name = _text(tool, "name")
        model_call_id = _text(tool, "model_call_id")
        execution_id = _tool_execution_id(
            authority.job.command_id,
            authority.turn.turn_id,
            ordinal,
            name,
        )
        if (
            set(tool)
            != {
                "execution_id",
                "model_call_id",
                "name",
                "arguments",
                "result_summary",
            }
            or not name
            or not model_call_id
            or tool.get("execution_id") != execution_id
            or not isinstance(tool.get("arguments"), Mapping)
            or not isinstance(tool.get("result_summary"), Mapping)
        ):
            raise WorkflowInvariantError("final Agent durable tool record drifted")
        expected_tool_traces.add(("agent.tool.started", execution_id))
        expected_tool_traces.add(("agent.tool.succeeded", execution_id))
        await _validate_tool_summary(
            session,
            authority=authority,
            tool=tool,
            validation_state=validation_state,
        )
        started = _tool_traces(traces, "agent.tool.started", execution_id)
        succeeded = _tool_traces(traces, "agent.tool.succeeded", execution_id)
        if len(started) > 1 or len(succeeded) > 1:
            raise WorkflowInvariantError("final Agent tool trace is duplicated")
        started_missing |= not started
        succeeded_missing |= not succeeded
        if started and cast(Mapping[str, Any], started[0]["fields"]) != {
            "execution_id": execution_id,
            "tool": name,
            "ordinal": ordinal,
        }:
            raise WorkflowInvariantError("final Agent tool start trace drifted")
        if succeeded:
            fields = cast(Mapping[str, Any], succeeded[0]["fields"])
            if (
                set(fields) != {"execution_id", "tool", "evidence_count"}
                or fields.get("tool") != name
                or isinstance(fields.get("evidence_count"), bool)
                or not isinstance(fields.get("evidence_count"), int)
                or cast(int, fields["evidence_count"]) < 0
            ):
                raise WorkflowInvariantError("final Agent tool success trace drifted")
    allowed_core_names = set(expected_core)
    for trace in traces:
        name = cast(str, trace["name"])
        if name in allowed_core_names:
            continue
        fields = cast(Mapping[str, Any], trace["fields"])
        if (name, cast(str, fields.get("execution_id"))) not in expected_tool_traces:
            raise WorkflowInvariantError("final Agent contains an unexpected runtime trace")
    if ("TRACE_TOOL_STARTED_WRITE_FAILED" in warning_set) != started_missing or (
        "TRACE_TOOL_SUCCEEDED_WRITE_FAILED" in warning_set
    ) != succeeded_missing:
        raise WorkflowInvariantError("final Agent tool warning closure drifted")


def _expected_agent_trace_audit_ids(
    *,
    authority: ValidatedRunAuthority,
    role: str,
    receipt_count: int,
    invalid_attempts: int,
    tools: Sequence[object],
) -> tuple[str, ...]:
    occurrences: list[tuple[str, Mapping[str, Any]]] = [
        ("agent.turn.started", {}),
        *(
            ("agent.model.requested", {"request_number": request_number})
            for request_number in range(1, receipt_count + 1)
        ),
        *(
            ("agent.output.invalid", {"repair_attempt": repair_attempt})
            for repair_attempt in range(1, invalid_attempts + 1)
        ),
    ]
    for ordinal, raw in enumerate(tools, start=1):
        tool = _object(raw, "final Agent tool record")
        name = _text(tool, "name")
        execution_id = _tool_execution_id(
            authority.job.command_id,
            authority.turn.turn_id,
            ordinal,
            name,
        )
        occurrences.extend(
            (
                ("agent.tool.started", {"execution_id": execution_id}),
                ("agent.tool.succeeded", {"execution_id": execution_id}),
            )
        )
    occurrences.append(("agent.turn.finished", {}))
    return tuple(
        agent_trace_audit_id(
            authority.job.tenant_id,
            {
                "name": name,
                "turn_id": authority.turn.turn_id,
                "role": role,
                "fields": fields,
                "command_id": authority.job.command_id,
                "trace_id": authority.context.trace_id,
            },
        )
        for name, fields in occurrences
    )


def _parse_final_provider_receipt(
    receipt: JobStepReceiptRow,
) -> _FinalProviderReceiptItem:
    envelope = _object(receipt.receipt_json, "final Provider receipt")
    dispatch = _object(envelope.get("dispatch"), "final Provider dispatch")
    result_data = _object(envelope.get("result"), "final Provider result")
    if (
        set(envelope) != {"schema_version", "dispatch", "result"}
        or envelope.get("schema_version") != "2.0.0"
        or set(dispatch)
        != {
            "dispatch_id",
            "request_sha256",
            "context_sha256",
            "provider",
            "model",
            "completion_sha256",
            "state",
            "generation_count",
            "raw_response_sha256",
        }
        or not isinstance(dispatch.get("dispatch_id"), str)
        or not _sha256(dispatch.get("request_sha256"))
        or not _sha256(dispatch.get("context_sha256"))
        or not isinstance(dispatch.get("provider"), str)
        or not isinstance(dispatch.get("model"), str)
        or not _sha256(dispatch.get("completion_sha256"))
        or dispatch.get("state") != "SUCCEEDED"
        or dispatch.get("generation_count") != 1
        or not _sha256(dispatch.get("raw_response_sha256"))
    ):
        raise WorkflowInvariantError("final Provider receipt envelope drifted")
    try:
        result = validated_provider_result_data(result_data)
    except (TypeError, ValueError) as error:
        raise WorkflowInvariantError("final Provider Result is not canonical") from error
    provider = cast(str, dispatch["provider"])
    model = cast(str, dispatch["model"])
    if isinstance(result, Failure):
        error = result.error
        details = error.details
        if (
            error.code != "INVARIANT_VIOLATION"
            or error.stage != "MODEL_OUTPUT"
            or ("repairable" in details and not isinstance(details.get("repairable"), bool))
            or details.get("repairable", True) is False
        ):
            raise WorkflowInvariantError("final Provider failure is not repairable model output")
        return _FinalProviderReceiptItem(
            kind="failure",
            provider=provider,
            model=model,
            input_tokens=0,
            output_tokens=0,
        )
    if not isinstance(result, Success) or not isinstance(result.value, LlmReply):
        raise WorkflowInvariantError("final Provider Result is outside Result[LlmReply]")
    reply = result.value
    output = _object(reply.output, "final Provider output")
    if (
        reply.provider != provider
        or reply.model != model
        or reply.source != "provider"
        or reply.degraded is not False
        or reply.fallback_reason is not None
        or reply.evidence_refs != ()
        or set(output) != {"kind", "decision", "tool_calls"}
        or output.get("kind") not in {"decision", "tool_calls"}
        or (
            output.get("kind") == "decision"
            and (not isinstance(output.get("decision"), Mapping) or output.get("tool_calls") != ())
        )
        or (
            output.get("kind") == "tool_calls"
            and (
                output.get("decision") is not None
                or not isinstance(output.get("tool_calls"), tuple)
            )
        )
    ):
        raise WorkflowInvariantError("final Provider success reply drifted")
    raw_tool_calls: list[Mapping[str, Any]] = []
    if output.get("kind") == "tool_calls":
        for raw_call in cast(tuple[object, ...], output["tool_calls"]):
            if (
                not isinstance(raw_call, Mapping)
                or set(raw_call) != {"call_id", "name", "arguments"}
                or not isinstance(raw_call.get("call_id"), str)
                or not raw_call.get("call_id")
                or not isinstance(raw_call.get("name"), str)
                or not raw_call.get("name")
                or not isinstance(raw_call.get("arguments"), Mapping)
            ):
                raise WorkflowInvariantError("final Provider tool call envelope drifted")
            raw_tool_calls.append(dict(raw_call))
    return _FinalProviderReceiptItem(
        kind=cast(str, output["kind"]),
        provider=provider,
        model=model,
        input_tokens=reply.input_tokens,
        output_tokens=reply.output_tokens,
        decision=(
            dict(cast(Mapping[str, Any], output["decision"]))
            if output.get("kind") == "decision"
            else None
        ),
        tool_calls=tuple(raw_tool_calls),
    )


def _classify_final_provider_request_history(
    receipts: Sequence[_FinalProviderReceiptItem],
    durable_tool_calls: object,
) -> _FinalProviderRequestHistory:
    """Derive repair/tool rounds from the terminal durable Agent decision.

    A Provider-shaped ``tool_calls`` response is not evidence that its batch
    passed Agent validation or executed.  Only the final decision's durable
    tool records prove one successful tool round; every other non-terminal
    request must fit the single repair allowance.
    """

    if (
        not isinstance(durable_tool_calls, list)
        or not receipts
        or receipts[-1].kind != "decision"
        or any(item.kind not in ("decision", "tool_calls", "failure") for item in receipts)
    ):
        raise WorkflowInvariantError("final Agent has an impossible Provider request history")
    successful_tool_rounds = int(bool(durable_tool_calls))
    invalid_attempts = len(receipts) - 1 - successful_tool_rounds
    provider_tool_call_receipts = sum(item.kind == "tool_calls" for item in receipts)
    extra_tool_call_receipts = provider_tool_call_receipts - successful_tool_rounds
    if (
        invalid_attempts not in {0, 1}
        or provider_tool_call_receipts < successful_tool_rounds
        or extra_tool_call_receipts > invalid_attempts
    ):
        raise WorkflowInvariantError("final Agent has an impossible Provider request history")
    return _FinalProviderRequestHistory(
        successful_tool_rounds=successful_tool_rounds,
        invalid_attempts=invalid_attempts,
    )


def _tool_execution_id(command_id: str, turn_id: str, ordinal: int, name: str) -> str:
    identity = f"{command_id}:{turn_id}:read:{ordinal}:{name}:v1"
    return f"toolexec_{hashlib.sha256(identity.encode()).hexdigest()[:24]}"


def _tool_traces(
    traces: Sequence[Mapping[str, Any]],
    name: str,
    execution_id: str,
) -> list[Mapping[str, Any]]:
    return [
        trace
        for trace in traces
        if trace.get("name") == name
        and isinstance(trace.get("fields"), Mapping)
        and cast(Mapping[str, Any], trace["fields"]).get("execution_id") == execution_id
    ]


async def _validate_tool_summary(
    session: AsyncSession,
    *,
    authority: ValidatedRunAuthority,
    tool: Mapping[str, Any],
    validation_state: TerminalProjectionValidationState | None = None,
) -> None:
    validation_state = validation_state or TerminalProjectionValidationState()
    validation_state.bind_session(session)
    name = _text(tool, "name")
    summary = _object(tool.get("result_summary"), "final Agent tool summary")
    arguments = _object(tool.get("arguments"), "final Agent tool arguments")
    expected: Mapping[str, Any] | None = None
    if name == "get_current_run":
        expected = _run_tool_projection(authority.run)
    elif name == "get_task_tests_summary":
        expected = {"counterexamples": []}
    elif name == "get_current_task":
        content = await session.scalar(
            select(ProductContentUnitRow).where(
                ProductContentUnitRow.tenant_id == authority.job.tenant_id,
                ProductContentUnitRow.unit_id == authority.context.content_ref.unit_id,
                ProductContentUnitRow.version == authority.context.content_ref.version,
                ProductContentUnitRow.content_hash == authority.context.content_ref.content_hash,
            )
        )
        if content is None:
            raise WorkflowInvariantError("final tool lost its Content task")
        task = _object(content.content_json.get("task"), "Content task")
        expected = {
            "task_id": task.get("task_id"),
            "title": task.get("name"),
            "goal": task.get("goal"),
            "knowledge_points": task.get("knowledge_points"),
        }
    elif name == "get_learner_profile":
        expected = await _learner_summary_at_decision(session, authority)
    elif name in {"get_current_skill", "propose_skill_patch"}:
        source, entrypoint = await _bound_skill_source(session, authority)
        if name == "get_current_skill":
            expected = {
                "binding": "bound_skill",
                "source_chars": len(source),
                "entrypoint": entrypoint,
            }
        else:
            old = arguments.get("old_text")
            new = arguments.get("new_text")
            occurrences = source.count(old) if isinstance(old, str) else 0
            if occurrences != 1 or not isinstance(new, str) or old == new:
                raise WorkflowInvariantError("final patch tool arguments drifted")
            expected = {"eligible": True, "target_occurrences": occurrences}
    elif name == "get_session_runs":
        runs = await list_validated_session_runs(
            session,
            session_id=authority.run.session_id,
            through_run_id=authority.run.run_id,
            context=authority.context,
            validation_state=validation_state,
        )
        expected = {"runs": [_run_tool_projection(item) for item in runs]}
    elif name == "get_skill_history":
        expected = await _skill_history_summary(session, authority)
    normalized_expected = None if expected is None else json_value(expected)
    if not isinstance(normalized_expected, Mapping) or not _exact_json_value_matches(
        summary, normalized_expected
    ):
        raise WorkflowInvariantError("final Agent tool result summary drifted")


def _exact_json_value_matches(actual: object, expected: object) -> bool:
    """Compare JSON authority without Python's bool/int or int/float coercion."""

    if isinstance(expected, Mapping):
        return (
            isinstance(actual, Mapping)
            and set(actual) == set(expected)
            and all(_exact_json_value_matches(actual[key], expected[key]) for key in expected)
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(
                _exact_json_value_matches(actual_item, expected_item)
                for actual_item, expected_item in zip(actual, expected, strict=True)
            )
        )
    return type(actual) is type(expected) and actual == expected


async def _learner_summary_at_decision(
    session: AsyncSession,
    authority: ValidatedRunAuthority,
) -> dict[str, Any]:
    learner_id = authority.context.actor.actor_id
    owner = await session.scalar(
        select(AgentSessionRow).where(
            AgentSessionRow.tenant_id == authority.job.tenant_id,
            AgentSessionRow.actor_id == authority.context.actor.actor_id,
            AgentSessionRow.session_id == authority.run.session_id,
        )
    )
    if owner is None or owner.session_json.get("learner_id") != learner_id:
        raise WorkflowInvariantError("final learner tool lost its Session identity")
    handoff = await session.scalar(
        select(LearnerProjectionJobRow).where(
            LearnerProjectionJobRow.tenant_id == authority.job.tenant_id,
            LearnerProjectionJobRow.job_id == authority.job.job_id,
        )
    )
    if handoff is None:
        profile = await session.scalar(
            select(LearnerProfileRow).where(
                LearnerProfileRow.tenant_id == authority.job.tenant_id,
                LearnerProfileRow.learner_id == learner_id,
                LearnerProfileRow.actor_id == authority.context.actor.actor_id,
                LearnerProfileRow.content_hash == authority.context.content_ref.content_hash,
            )
        )
        if profile is None or profile.profile_sha256 != canonical_json_sha256(profile.profile_json):
            raise WorkflowInvariantError("final learner tool profile drifted")
        value = profile.profile_json
    elif handoff.expected_revision == 0:
        value = {"revision": 0, "competencies": {}}
    else:
        previous = await session.scalar(
            select(LearnerProjectionJobRow).where(
                LearnerProjectionJobRow.tenant_id == handoff.tenant_id,
                LearnerProjectionJobRow.learner_id == handoff.learner_id,
                LearnerProjectionJobRow.expected_revision == handoff.expected_revision - 1,
                LearnerProjectionJobRow.status == "SUCCEEDED",
            )
        )
        receipt = (
            None
            if previous is None
            else await session.scalar(
                select(JobStepReceiptRow).where(
                    JobStepReceiptRow.tenant_id == previous.tenant_id,
                    JobStepReceiptRow.job_id == previous.job_id,
                    JobStepReceiptRow.step_name == "LEARNER_PROJECTION_COMMITTED",
                )
            )
        )
        if receipt is None or receipt.output_sha256 != workflow_receipt_sha256(
            receipt.receipt_json
        ):
            raise WorkflowInvariantError("final learner tool lost prior immutable profile")
        learner_authority = _object(receipt.receipt_json.get("learner"), "prior Learner authority")
        value = _object(learner_authority.get("profile"), "prior Learner profile")
        if (
            learner_authority.get("profile_sha256") != canonical_json_sha256(value)
            or value.get("revision") != handoff.expected_revision
        ):
            raise WorkflowInvariantError("final learner tool prior profile drifted")
    revision = value.get("revision")
    competencies = value.get("competencies")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or not isinstance(competencies, Mapping)
    ):
        raise WorkflowInvariantError("final learner tool summary is invalid")
    return {
        "student_id": learner_id,
        "revision": revision,
        "competencies": dict(competencies),
    }


def _run_tool_projection(run: RunResultSnapshot) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "task_success": run.task_success,
        "world_revision_before": run.world_revision_before,
        "world_revision_after": run.world_revision_after,
        "world_difference": run.world_difference,
        "failed_actions": run.failed_actions,
        "failure_key": run.failure_key,
        "evidence_ids": [item.evidence_id for item in run.evidence_refs],
    }


async def _bound_skill_source(
    session: AsyncSession,
    authority: ValidatedRunAuthority,
) -> tuple[str, str]:
    reference = authority.run.skill_ref
    certification = await session.scalar(
        select(SkillCertificationRow).where(
            SkillCertificationRow.tenant_id == authority.job.tenant_id,
            SkillCertificationRow.certification_id == reference.certification_id,
        )
    )
    build = (
        None
        if certification is None
        else await session.scalar(
            select(SkillBuildRow).where(
                SkillBuildRow.tenant_id == authority.job.tenant_id,
                SkillBuildRow.build_id == certification.build_id,
            )
        )
    )
    bundle = _object(
        None if build is None else build.request_json.get("source_bundle"),
        "bound Skill source bundle",
    )
    entrypoint = _text(bundle, "entrypoint")
    files = bundle.get("files")
    if not isinstance(files, list):
        raise WorkflowInvariantError("bound Skill source files drifted")
    selected = next(
        (item for item in files if isinstance(item, Mapping) and item.get("path") == entrypoint),
        None,
    )
    if selected is None:
        raise WorkflowInvariantError("bound Skill entrypoint disappeared")
    return _text(selected, "content"), entrypoint


async def _skill_history_summary(
    session: AsyncSession,
    authority: ValidatedRunAuthority,
) -> dict[str, Any]:
    rows = list(
        (
            await session.scalars(
                select(RegistryEntryRow)
                .where(
                    RegistryEntryRow.tenant_id == authority.job.tenant_id,
                    RegistryEntryRow.actor_id == authority.context.actor.actor_id,
                    RegistryEntryRow.content_hash == authority.context.content_ref.content_hash,
                    RegistryEntryRow.skill_id == authority.run.skill_ref.skill_id,
                )
                .order_by(RegistryEntryRow.revision)
            )
        ).all()
    )
    versions: list[dict[str, Any]] = []
    for row in rows:
        certification = await session.scalar(
            select(SkillCertificationRow).where(
                SkillCertificationRow.tenant_id == row.tenant_id,
                SkillCertificationRow.certification_id == row.certification_id,
            )
        )
        artifact = (
            None
            if certification is None
            else await session.scalar(
                select(SkillArtifactRow).where(
                    SkillArtifactRow.tenant_id == row.tenant_id,
                    SkillArtifactRow.build_id == certification.build_id,
                    SkillArtifactRow.artifact_sha256 == row.artifact_sha256,
                )
            )
        )
        if certification is None or artifact is None:
            raise WorkflowInvariantError("final skill history tool authority drifted")
        versions.append(
            {
                "skill_id": row.skill_id,
                "skill_version_id": row.skill_version_id,
                "source_sha256": artifact.source_sha256,
                "change_summary": "Certified student Skill version.",
            }
        )
    return {"versions": versions}


async def _step_receipt(
    session: AsyncSession,
    job: WorkflowJobRow,
    step_name: str,
) -> JobStepReceiptRow | None:
    return await session.scalar(
        select(JobStepReceiptRow).where(
            JobStepReceiptRow.tenant_id == job.tenant_id,
            JobStepReceiptRow.job_id == job.job_id,
            JobStepReceiptRow.step_name == step_name,
        )
    )


def _bounded_provider_receipt_rows(
    rows: Sequence[JobStepReceiptRow],
    *,
    namespace: str,
    max_results: int,
    authority_label: str,
) -> tuple[list[JobStepReceiptRow], dict[str, JobStepReceiptRow]]:
    provider_prefix = f"{namespace}_PROVIDER_"
    result_prefix = f"{provider_prefix}RESULT_"
    dispatch_prefix = f"{provider_prefix}DISPATCH_"
    results = sorted(
        (row for row in rows if row.step_name.startswith(result_prefix)),
        key=lambda row: row.step_name,
    )
    dispatches = sorted(
        (row for row in rows if row.step_name.startswith(dispatch_prefix)),
        key=lambda row: row.step_name,
    )
    if not results or len(results) > max_results or len(dispatches) != len(results):
        raise WorkflowInvariantError(
            f"{authority_label} decision has no bounded Provider result history"
        )
    by_name = {row.step_name: row for row in rows}
    if len(by_name) != len(rows):
        raise WorkflowInvariantError("workflow contains duplicate step receipt names")
    expected_provider_names = {
        *(
            f"{provider_prefix}DISPATCH_{item:02d}"
            for item in range(1, len(results) + 1)
        ),
        *(
            f"{provider_prefix}RESULT_{item:02d}"
            for item in range(1, len(results) + 1)
        ),
    }
    actual_provider_names = {
        row.step_name for row in rows if row.step_name.startswith(provider_prefix)
    }
    if actual_provider_names != expected_provider_names:
        raise WorkflowInvariantError(
            f"{authority_label} Provider receipt history drifted"
        )
    return results, by_name


async def _load_provider_receipts(
    session: AsyncSession,
    job: WorkflowJobRow,
    *,
    namespace: str,
    ordinal_base: int,
    max_results: int,
    authority_label: str,
) -> tuple[JobStepReceiptRow, ...]:
    rows = list(
        (
            await session.scalars(
                select(JobStepReceiptRow).where(
                    JobStepReceiptRow.tenant_id == job.tenant_id,
                    JobStepReceiptRow.job_id == job.job_id,
                )
            )
        ).all()
    )
    results, by_name = _bounded_provider_receipt_rows(
        rows,
        namespace=namespace,
        max_results=max_results,
        authority_label=authority_label,
    )
    raw_context = job.job_json.get("request_context")
    if not isinstance(raw_context, dict):
        raise WorkflowInvariantError(
            f"{authority_label} Provider job lost its OperationContext"
        )
    try:
        context_sha256 = operation_context_sha256(_job_operation_context(job))
    except (KeyError, TypeError, ValueError) as error:
        raise WorkflowInvariantError(
            f"{authority_label} Provider OperationContext is not canonical"
        ) from error
    provider_prefix = f"{namespace}_PROVIDER_"
    for ordinal, result in enumerate(results, start=1):
        expected_result = f"{provider_prefix}RESULT_{ordinal:02d}"
        expected_dispatch = f"{provider_prefix}DISPATCH_{ordinal:02d}"
        dispatch = by_name.get(expected_dispatch)
        dispatch_value = dispatch.receipt_json if dispatch is not None else {}
        result_value = result.receipt_json
        result_dispatch = (
            result_value.get("dispatch") if isinstance(result_value, Mapping) else None
        )
        expected_ordinal = ordinal_base + ordinal
        expected_dispatch_id = provider_dispatch_id(
            job.tenant_id,
            job.job_id,
            expected_ordinal,
            dispatch.input_sha256 if dispatch is not None else "0" * 64,
        )
        if (
            result.step_name != expected_result
            or dispatch is None
            or dispatch.step_name != expected_dispatch
            or result.receipt_id
            != workflow_step_receipt_id(job.tenant_id, job.job_id, expected_result)
            or dispatch.receipt_id
            != workflow_step_receipt_id(job.tenant_id, job.job_id, expected_dispatch)
            or result.output_sha256 != workflow_receipt_sha256(result.receipt_json)
            or dispatch.output_sha256 != workflow_receipt_sha256(dispatch.receipt_json)
            or result.fencing_token < 1
            or result.fencing_token > job.fencing_token
            or dispatch.fencing_token < 1
            or dispatch.fencing_token > result.fencing_token
            or dispatch.input_sha256 != result.input_sha256
            or dispatch.completed_at > result.completed_at
            or set(dispatch_value)
            != {
                "schema_version",
                "ordinal",
                "dispatch_id",
                "request_sha256",
                "context_sha256",
                "provider",
                "model",
                "command_id",
                "turn_id",
                "timeout_ms",
            }
            or dispatch_value.get("schema_version") != "2.0.0"
            or dispatch_value.get("ordinal") != expected_ordinal
            or dispatch_value.get("dispatch_id") != expected_dispatch_id
            or dispatch_value.get("command_id") != job.command_id
            or dispatch_value.get("turn_id") != job.subject_id
            or dispatch_value.get("request_sha256") != dispatch.input_sha256
            or dispatch_value.get("context_sha256") != context_sha256
            or not _sha256(dispatch_value.get("request_sha256"))
            or not _sha256(dispatch_value.get("context_sha256"))
            or not isinstance(result_dispatch, Mapping)
            or any(
                result_dispatch.get(key) != dispatch_value.get(key)
                for key in (
                    "dispatch_id",
                    "request_sha256",
                    "context_sha256",
                    "provider",
                    "model",
                )
            )
        ):
            raise WorkflowInvariantError(
                f"{authority_label} Provider receipt history drifted"
            )
    return tuple(results)


async def load_final_provider_receipts(
    session: AsyncSession,
    job: WorkflowJobRow,
) -> tuple[JobStepReceiptRow, ...]:
    return await _load_provider_receipts(
        session,
        job,
        namespace="FINAL",
        ordinal_base=100,
        max_results=3,
        authority_label="final",
    )


async def load_patch_provider_receipts(
    session: AsyncSession,
    job: WorkflowJobRow,
) -> tuple[JobStepReceiptRow, ...]:
    return await _load_provider_receipts(
        session,
        job,
        namespace="PATCH",
        ordinal_base=200,
        max_results=2,
        authority_label="Patch",
    )


async def _validate_provider_decision(
    session: AsyncSession,
    *,
    authority: ValidatedRunAuthority,
    receipts: Sequence[JobStepReceiptRow],
    decision: AgentDecision,
) -> None:
    await validate_agent_decision_runtime_authority(
        session,
        authority=authority,
        receipts=receipts,
        decision=cast(dict[str, Any], json_value(decision)),
    )
    validate_provider_decision_wire(
        receipts,
        decision_draft=cast(dict[str, Any], json_value(decision.draft)),
        evidence_refs=decision.evidence_refs,
        decision=cast(dict[str, Any], json_value(decision)),
    )


def validate_provider_decision_wire(
    receipts: Sequence[JobStepReceiptRow],
    *,
    decision_draft: Mapping[str, Any],
    evidence_refs: Sequence[EvidenceRef],
    decision: Mapping[str, Any] | None = None,
) -> None:
    """Bind the terminal Provider raw draft to its normalized durable draft."""

    if not receipts:
        raise WorkflowInvariantError("final Agent decision has no Provider receipts")
    provider_history = tuple(_parse_final_provider_receipt(receipt) for receipt in receipts)
    provider_names = {item.provider for item in provider_history}
    model_names = {item.model for item in provider_history}
    total_input_tokens = sum(item.input_tokens for item in provider_history)
    total_output_tokens = sum(item.output_tokens for item in provider_history)
    raw_tool_call_batches = [
        item.tool_calls for item in provider_history if item.kind == "tool_calls"
    ]
    terminal = provider_history[-1]
    raw_decision = _object(terminal.decision, "final Provider decision")
    expected_provider = decision.get("provider") if decision is not None else None
    expected_model = decision.get("model") if decision is not None else None
    expected_input_tokens = decision.get("input_tokens") if decision is not None else None
    expected_output_tokens = decision.get("output_tokens") if decision is not None else None
    decision_tools = decision.get("tool_calls") if decision is not None else None
    history = _classify_final_provider_request_history(
        provider_history,
        decision_tools if decision is not None else [],
    )
    if (
        len(provider_names) != 1
        or len(model_names) != 1
        or terminal.kind != "decision"
        or terminal.tool_calls
        or (decision is not None and terminal.provider != expected_provider)
        or (decision is not None and terminal.model != expected_model)
        or (decision is not None and total_input_tokens != expected_input_tokens)
        or (decision is not None and total_output_tokens != expected_output_tokens)
        or (
            decision is not None
            and not _provider_tool_calls_match(
                raw_tool_call_batches,
                decision_tools,
                successful_tool_rounds=history.successful_tool_rounds,
            )
        )
        or not _provider_draft_matches(
            raw_decision,
            decision_draft,
            evidence_refs=evidence_refs,
        )
    ):
        raise WorkflowInvariantError("final Agent decision differs from Provider authority")


def _provider_tool_calls_match(
    raw_batches: Sequence[Sequence[Mapping[str, Any]]],
    durable_calls: object,
    *,
    successful_tool_rounds: int,
) -> bool:
    if not isinstance(durable_calls, list):
        return False
    if successful_tool_rounds == 0:
        return not durable_calls
    if successful_tool_rounds != 1 or not durable_calls:
        return False
    return any(_provider_tool_call_batch_matches(batch, durable_calls) for batch in raw_batches)


def _provider_tool_call_batch_matches(
    raw_calls: Sequence[Mapping[str, Any]],
    durable_calls: Sequence[object],
) -> bool:
    if len(raw_calls) != len(durable_calls):
        return False
    for raw, durable in zip(raw_calls, durable_calls, strict=True):
        if (
            not isinstance(durable, Mapping)
            or set(durable)
            != {
                "execution_id",
                "model_call_id",
                "name",
                "arguments",
                "result_summary",
            }
            or raw.get("call_id") != durable.get("model_call_id")
            or raw.get("name") != durable.get("name")
            or raw.get("arguments") != durable.get("arguments")
            or not isinstance(durable.get("execution_id"), str)
            or not isinstance(durable.get("result_summary"), Mapping)
        ):
            return False
    return True


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _provider_draft_matches(
    raw: Mapping[str, Any],
    durable: Mapping[str, Any],
    *,
    evidence_refs: Sequence[EvidenceRef],
) -> bool:
    expected_keys = {
        "role",
        "response_type",
        "message",
        "question",
        "hint_level",
        "learner_inference",
        "skill_patch",
        "requires_student_confirmation",
    }
    if set(raw) != expected_keys or set(durable) != expected_keys:
        return False
    if not _provider_public_copy_shape(raw) or not _provider_public_copy_shape(durable):
        return False
    normalized = dict(raw)
    raw_inference = raw.get("learner_inference")
    durable_inference = durable.get("learner_inference")
    if raw_inference is None or durable_inference is None:
        if raw_inference is not None or durable_inference is not None:
            return False
    else:
        if not isinstance(raw_inference, Mapping) or not isinstance(durable_inference, Mapping):
            return False
        inference_keys = {
            "concept",
            "score_delta",
            "confidence",
            "reason",
            "evidence_ids",
        }
        if set(raw_inference) != inference_keys or set(durable_inference) != inference_keys:
            return False
        raw_ids = raw_inference.get("evidence_ids")
        if (
            isinstance(raw_ids, str | bytes | bytearray)
            or not isinstance(raw_ids, Sequence)
            or any(not isinstance(item, str) for item in raw_ids)
        ):
            return False
        aliases = {
            f"evidence_{index:03d}": reference.evidence_id
            for index, reference in enumerate(evidence_refs, start=1)
        }
        try:
            resolved = [aliases[cast(str, item)] for item in raw_ids]
        except KeyError:
            return False
        normalized_inference = dict(raw_inference)
        normalized_inference["evidence_ids"] = resolved
        normalized["learner_inference"] = normalized_inference
    # The Agent validator intentionally replaces untrusted Provider prose with
    # deterministic public copy after validating the raw draft.  Bind every
    # field that survives that policy transform, while retaining the frozen
    # schema bounds for the discarded Provider message/question.
    normalized["message"] = durable.get("message")
    normalized["question"] = durable.get("question")
    return normalized == dict(durable)


def _provider_public_copy_shape(value: Mapping[str, Any]) -> bool:
    message = value.get("message")
    question = value.get("question")
    return (
        isinstance(message, str)
        and 1 <= len(message) <= 4000
        and (question is None or (isinstance(question, str) and 1 <= len(question) <= 1000))
    )


def _validate_final_decision(
    outcome: GameEvent,
    decision: AgentDecision,
    run: RunResultSnapshot,
) -> None:
    route = RoleRouter().route(outcome)
    if (
        not route.should_run
        or route.role not in {"teaching_agent", "bug_agent", "book_agent"}
        or decision.role != route.role
        or decision.source != "provider"
        or decision.degraded
        or decision.fallback_reason is not None
        or decision.teaching_directive is None
        or decision.teaching_directive.patch_eligible
        or decision.teaching_directive.full_solution_eligible
        or decision.draft.skill_patch is not None
        or set(decision.evidence_refs) != set(run.evidence_refs)
        or outcome.run_id != run.run_id
        or outcome.command_id != run.command_id
        or outcome.session_id != run.session_id
        or outcome.turn_id != run.turn_id
        or outcome.skill_ref != run.skill_ref
        or outcome.evidence_refs != run.evidence_refs
        or (outcome.event_type == "task_completed") != run.task_success
    ):
        raise WorkflowInvariantError("final Agent decision is not closed over its Run outcome")
    if route.role == "book_agent" and decision.response_type != "growth_summary":
        raise WorkflowInvariantError("book_agent must publish one growth_summary")


def _validate_live_root(
    claim: ClaimedWorkflowJob,
    authority: ValidatedRunAuthority,
    root_event: GameEvent,
    context: OperationContext,
) -> None:
    run = authority.run
    if (
        claim.command_id != root_event.command_id
        or claim.subject_id != root_event.turn_id
        or root_event.event_type != "run_skill_requested"
        or root_event.student_id != context.actor.actor_id
        or run.session_id != root_event.session_id
        or run.turn_id != root_event.turn_id
        or run.command_id != root_event.command_id
        or run.skill_ref != root_event.skill_ref
        or run.world_revision_before != root_event.expected_world_revision
        or authority.command.terminal
        or authority.command.status
        not in {CommandStatus.RUNNING_SANDBOX, CommandStatus.APPLYING_WORLD}
    ):
        raise WorkflowInvariantError("live Turn root does not close the canonical Run")


def _validate_command_context(command: CommandRecord, context: OperationContext) -> None:
    if (
        command.command_type != "EXECUTE_AGENT_TURN"
        or command.command_id != context.command_id
        or not _same_actor(command.request_context.actor, context.actor)
        or command.request_context.content_ref != context.content_ref
    ):
        raise WorkflowInvariantError("Run Command context differs from durable authority")


def _validate_run_row(
    row: RunRow,
    run: RunResultSnapshot,
    context: OperationContext,
) -> None:
    wire = row.run_json
    feedback = wire.get("agent_feedback")
    evidence = wire.get("evidence_refs")
    world = _object(wire.get("world_application"), "Run world_application")
    created_at = _timestamp(_text(wire, "created_at"))
    if (
        row.run_id != run.run_id
        or row.session_id != run.session_id
        or row.turn_id != run.turn_id
        or row.command_id != run.command_id
        or row.created_at != created_at
        or wire.get("run_id") != run.run_id
        or wire.get("session_id") != run.session_id
        or wire.get("turn_id") != run.turn_id
        or wire.get("command_id") != run.command_id
        or wire.get("skill") != cast(dict[str, Any], json_value(run.skill_ref))
        or wire.get("terminal") is not True
        or evidence != [_evidence_ref_wire(item) for item in run.evidence_refs]
        or _object(wire.get("request_context"), "Run request_context")
        != _request_context_wire(run.request_context)
        or (feedback is not None and not isinstance(feedback, Mapping))
    ):
        raise WorkflowInvariantError("Run row, wire and invocation receipt differ")
    if run.task_success:
        if wire.get("status") != "SUCCEEDED" or world.get("status") != "COMMITTED":
            raise WorkflowInvariantError("successful Run wire is not committed")
        if run.world_commit is None or world.get("receipt") != _world_receipt_wire(
            run.world_commit
        ):
            raise WorkflowInvariantError("Run World receipt differs from typed authority")
    elif wire.get("status") not in {"REJECTED", "FAILED"} or world.get("status") == "COMMITTED":
        raise WorkflowInvariantError("failed Run wire has an invalid terminal status")
    if (
        not _same_actor(context.actor, run.request_context.actor)
        or context.content_ref != run.request_context.content_ref
    ):
        raise WorkflowInvariantError("Run request_context differs from operation authority")


async def _validate_evidence(
    session: AsyncSession,
    run: RunResultSnapshot,
    context: OperationContext,
) -> None:
    expected_types = (
        {EvidenceType.SANDBOX_LOG, EvidenceType.WORLD_COMMIT}
        if run.task_success
        else {EvidenceType.SANDBOX_LOG}
    )
    if (
        len(run.evidence_refs) != len(expected_types)
        or {item.evidence_type for item in run.evidence_refs} != expected_types
        or any(item.sha256 is None for item in run.evidence_refs)
    ):
        raise WorkflowInvariantError("Run has an invalid Evidence set")
    for reference in run.evidence_refs:
        row = await session.scalar(
            select(EvidenceRow).where(
                EvidenceRow.tenant_id == context.actor.tenant_id,
                EvidenceRow.actor_id == context.actor.actor_id,
                EvidenceRow.content_hash == context.content_ref.content_hash,
                EvidenceRow.evidence_id == reference.evidence_id,
                EvidenceRow.command_id == run.command_id,
            )
        )
        if row is None:
            raise WorkflowInvariantError("Run Evidence is missing")
        document = row.evidence_json
        evidence_ref = _object(document.get("evidence_ref"), "Evidence reference")
        integrity = _object(document.get("integrity"), "Evidence integrity")
        source = _object(document.get("source"), "Evidence source")
        payload = _object(document.get("payload"), "Evidence payload")
        if (
            evidence_ref != _evidence_ref_wire(reference)
            or integrity.get("payload_sha256") != reference.sha256
            or canonical_json_sha256(payload) != reference.sha256
            or source.get("command_id") != run.command_id
            or source.get("world_id") != run.world_id
            or _object(document.get("request_context"), "Evidence request_context")
            != _request_context_wire(run.request_context)
        ):
            raise WorkflowInvariantError("Run Evidence row or digest drifted")
        if reference.evidence_type is EvidenceType.SANDBOX_LOG:
            if source.get("source_type") != "SKILL_RUN" or source.get("source_id") != run.run_id:
                raise WorkflowInvariantError("SANDBOX_LOG Evidence source drifted")
        else:
            receipt = run.world_commit
            if (
                receipt is None
                or source.get("source_type") != "WORLD"
                or source.get("source_id") != run.world_id
                or reference.sha256 != world_commit_receipt_sha256(receipt)
            ):
                raise WorkflowInvariantError("WORLD_COMMIT Evidence source drifted")


async def _validate_world(
    session: AsyncSession,
    run: RunResultSnapshot,
    context: OperationContext,
    *,
    require_current: bool,
) -> None:
    row = await session.scalar(
        select(WorldSnapshotRow).where(
            WorldSnapshotRow.tenant_id == context.actor.tenant_id,
            WorldSnapshotRow.actor_id == context.actor.actor_id,
            WorldSnapshotRow.content_hash == context.content_ref.content_hash,
            WorldSnapshotRow.world_id == run.world_id,
        )
    )
    if row is None:
        raise WorkflowInvariantError("Run World snapshot is missing")
    snapshot = world_snapshot_from_data(row.snapshot_json)
    if (
        snapshot.request_context.actor != context.actor
        or snapshot.request_context.content_ref != context.content_ref
        or snapshot.world_id != run.world_id
        or snapshot.state_hash != canonical_json_sha256(snapshot.state)
        or (require_current and snapshot.revision != run.world_revision_after)
        or (not require_current and snapshot.revision < run.world_revision_after)
    ):
        raise WorkflowInvariantError("Run World snapshot authority drifted")
    if run.world_commit is None:
        if run.task_success or run.world_revision_after != run.world_revision_before:
            raise WorkflowInvariantError("uncommitted Run claims a World change")
        return
    receipt = run.world_commit
    event = await session.scalar(
        select(EventRow).where(
            EventRow.tenant_id == context.actor.tenant_id,
            EventRow.stream_id == f"world:{run.world_id}",
            EventRow.sequence == receipt.last_event_sequence,
        )
    )
    if event is None:
        raise WorkflowInvariantError("World receipt has no exact event")
    payload = _object(event.event_json.get("payload"), "World event payload")
    if (
        receipt.first_event_sequence != receipt.last_event_sequence
        or receipt.previous_revision != run.world_revision_before
        or receipt.world_revision != run.world_revision_after
        or payload.get("commit_id")
        != _world_commit_identifier(
            context.actor.tenant_id,
            f"world:{run.world_id}",
            run.run_id,
            run.world_revision_before,
        )
        or event.stream_id != f"world:{run.world_id}"
        or event.sequence != receipt.last_event_sequence
        or event.occurred_at != receipt.committed_at
        or event.event_json.get("event_type") != RuntimeEventType.WORLD_COMMITTED.value
        or event.event_json.get("command_id") != run.command_id
        or event.event_json.get("event_id") != event.event_id
        or event.event_json.get("stream_id") != event.stream_id
        or event.event_json.get("sequence") != event.sequence
        or payload.get("run_id") != run.run_id
        or payload.get("world_id") != run.world_id
        or payload.get("previous_world_revision") != receipt.previous_revision
        or payload.get("world_revision") != receipt.world_revision
        or payload.get("state_hash") != receipt.state_hash
        or payload.get("committed_at") != _iso(receipt.committed_at)
        or payload.get("evidence_refs")
        != [
            _evidence_ref_wire(item)
            for item in run.evidence_refs
            if item.evidence_type is EvidenceType.WORLD_COMMIT
        ]
        or (
            snapshot.revision == receipt.world_revision
            and snapshot.state_hash != receipt.state_hash
        )
    ):
        raise WorkflowInvariantError("World event differs from the Run receipt")


def _validate_terminal_command(authority: ValidatedRunAuthority) -> None:
    expected = CommandStatus.APPLIED if authority.run.task_success else CommandStatus.REJECTED
    if (
        not authority.command.terminal
        or authority.command.status is not expected
        or authority.job.status != "SUCCEEDED"
        or authority.command.evidence_refs != authority.run.evidence_refs
        or authority.command.links.get("run") != f"/v1/runs/{authority.run.run_id}"
    ):
        raise WorkflowInvariantError("prior terminal Command differs from its Run")


async def validate_terminal_projection(
    session: AsyncSession,
    authority: ValidatedRunAuthority,
    *,
    validation_state: TerminalProjectionValidationState | None = None,
) -> None:
    """Validate each exact terminal Run at most once in one recursive request."""

    state = validation_state or TerminalProjectionValidationState()
    state.bind_session(session)
    key = _terminal_projection_validation_key(authority)
    if key in state.completed:
        return
    if key in state.in_progress:
        raise WorkflowInvariantError("terminal projection validation cycle detected")
    state.in_progress.add(key)
    try:
        await _validate_terminal_projection_uncached(
            session,
            authority,
            validation_state=state,
        )
    except BaseException:
        raise
    else:
        state.completed.add(key)
    finally:
        state.in_progress.discard(key)


async def _validate_terminal_projection_uncached(
    session: AsyncSession,
    authority: ValidatedRunAuthority,
    *,
    validation_state: TerminalProjectionValidationState,
) -> None:
    """Prove the prior outcome, final decision, Interaction and learner source once."""

    interactions = list(
        (
            await session.scalars(
                select(ProductInteractionRow).where(
                    ProductInteractionRow.tenant_id == authority.job.tenant_id,
                    ProductInteractionRow.actor_id == authority.context.actor.actor_id,
                    ProductInteractionRow.session_id == authority.run.session_id,
                    ProductInteractionRow.turn_id == authority.run.turn_id,
                )
            )
        ).all()
    )
    if len(interactions) != 1:
        raise WorkflowInvariantError("prior terminal Run has no unique Product Interaction")
    interaction = interactions[0]
    value = interaction.interaction_json
    source = _object(value.get("projection_source"), "Interaction projection source")
    source_without_hash = dict(source)
    source_sha256 = source_without_hash.pop("source_sha256", None)
    feedback = _object(value.get("feedback"), "Interaction feedback")
    feedback_event_wire = _object(
        value.get("feedback_event"),
        "Interaction feedback event",
    )
    outcome_receipt = await _step_receipt(session, authority.job, _OUTCOME_STEP)
    decision_receipt = await _step_receipt(
        session,
        authority.job,
        _FINAL_DECISION_STEP,
    )
    terminal_receipt = await _step_receipt(session, authority.job, "TURN_COMPLETED")
    if outcome_receipt is None or decision_receipt is None or terminal_receipt is None:
        raise WorkflowInvariantError("prior terminal projection receipts are incomplete")
    for receipt in (outcome_receipt, decision_receipt, terminal_receipt):
        if (
            receipt.fencing_token < 1
            or receipt.fencing_token > authority.job.fencing_token
            or receipt.output_sha256 != workflow_receipt_sha256(receipt.receipt_json)
        ):
            raise WorkflowInvariantError("prior terminal projection receipt drifted")
    outcome_data = outcome_receipt.receipt_json
    outcome_event = _object(outcome_data.get("event"), "Run outcome event")
    canonical_outcome = await validate_canonical_outcome_event(
        session,
        authority=authority,
        outcome=outcome_event,
        validation_state=validation_state,
    )
    expected_event_type = "task_completed" if authority.run.task_success else "run_failed"
    failure_count = outcome_event.get("failure_count")
    if (
        outcome_data.get("schema_version") != "1.0.0"
        or outcome_data.get("run_sha256") != run_authority_sha256(authority.run_row.run_json)
        or outcome_data.get("invocation_request_sha256") != authority.result.request_sha256
        or outcome_receipt.input_sha256 != authority.result.request_sha256
        or outcome_event.get("event_type") != expected_event_type
        or outcome_event.get("session_id") != authority.run.session_id
        or outcome_event.get("turn_id") != authority.run.turn_id
        or outcome_event.get("command_id") != authority.run.command_id
        or outcome_event.get("run_id") != authority.run.run_id
        or outcome_event.get("student_id") != authority.context.actor.actor_id
        or outcome_event.get("skill_ref")
        != cast(dict[str, Any], json_value(authority.run.skill_ref))
        or outcome_event.get("evidence_refs")
        != [_decision_evidence_ref_wire(item) for item in authority.run.evidence_refs]
        or (
            authority.run.task_success
            and (failure_count != 0 or outcome_event.get("failure_key") is not None)
        )
        or (
            not authority.run.task_success
            and (
                isinstance(failure_count, bool)
                or not isinstance(failure_count, int)
                or failure_count < 1
                or outcome_event.get("failure_key") != authority.run.failure_key
            )
        )
    ):
        raise WorkflowInvariantError("prior Run outcome event differs from its Run")
    expected_role = RoleRouter().route(canonical_outcome).role
    outcome_sha256 = canonical_json_sha256(outcome_event)
    decision_data = decision_receipt.receipt_json
    decision = _object(decision_data.get("decision"), "final Agent decision")
    draft = _object(decision.get("draft"), "final Agent decision draft")
    directive = _object(
        decision.get("teaching_directive"),
        "final Agent TeachingDirective",
    )
    provider_receipts = await load_final_provider_receipts(session, authority.job)
    expected_provider_refs = [
        {
            "receipt_id": receipt.receipt_id,
            "step_name": receipt.step_name,
            "output_sha256": receipt.output_sha256,
        }
        for receipt in provider_receipts
    ]
    decision_evidence_wire = [
        _decision_evidence_ref_wire(item) for item in authority.run.evidence_refs
    ]
    if (
        decision_data.get("schema_version") != "1.0.0"
        or decision_data.get("outcome_event_id") != outcome_event.get("event_id")
        or decision_data.get("outcome_sha256") != outcome_sha256
        or decision_data.get("run_id") != authority.run.run_id
        or decision_data.get("invocation_request_sha256") != authority.result.request_sha256
        or decision_data.get("provider_result_receipts") != expected_provider_refs
        or decision_receipt.input_sha256 != outcome_sha256
        or draft.get("role") != expected_role
        or decision.get("source") != "provider"
        or decision.get("degraded") is not False
        or decision.get("fallback_reason") is not None
        or decision.get("evidence_refs") != decision_evidence_wire
        or directive.get("patch_eligible") is not False
        or directive.get("full_solution_eligible") is not False
        or draft.get("skill_patch") is not None
        or (expected_role == "book_agent" and draft.get("response_type") != "growth_summary")
    ):
        raise WorkflowInvariantError("prior final Agent decision authority drifted")
    await validate_agent_decision_runtime_authority(
        session,
        authority=authority,
        receipts=provider_receipts,
        decision=decision,
        validation_state=validation_state,
    )
    validate_provider_decision_wire(
        provider_receipts,
        decision_draft=draft,
        evidence_refs=authority.run.evidence_refs,
        decision=decision,
    )
    expected_feedback = decision_feedback_wire(
        decision,
        authority.run,
        expected_completed_at=_decision_timestamp(
            max(
                authority.run_row.created_at,
                *(item.created_at for item in authority.run.evidence_refs),
            )
        ),
    )
    feedback_sha256 = canonical_json_sha256(expected_feedback)
    if (
        feedback != expected_feedback
        or authority.run_row.run_json.get("agent_feedback") != expected_feedback
        or interaction.tenant_id != authority.job.tenant_id
        or interaction.actor_id != authority.context.actor.actor_id
        or interaction.session_id != authority.run.session_id
        or interaction.turn_id != authority.run.turn_id
        or value.get("interaction_id") != interaction.interaction_id
        or value.get("session_id") != interaction.session_id
        or value.get("turn_id") != interaction.turn_id
        or value.get("sequence") != interaction.sequence
        or value.get("role") != expected_role
        or value.get("response_type") != draft.get("response_type")
        or value.get("question") != draft.get("question")
        or value.get("hint_level") != draft.get("hint_level")
        or source.get("role") != expected_role
        or source.get("response_type") != draft.get("response_type")
        or source.get("question") != draft.get("question")
        or source.get("hint_level") != draft.get("hint_level")
        or source.get("feedback_sha256") != feedback_sha256
        or source_sha256 != canonical_json_sha256(source_without_hash)
        or terminal_receipt.input_sha256 != authority.result.request_sha256
        or terminal_receipt.receipt_json != source
        or terminal_receipt.output_sha256 != workflow_receipt_sha256(source)
    ):
        raise WorkflowInvariantError("prior Product Interaction authority drifted")
    feedback_event = await session.scalar(
        select(EventRow).where(
            EventRow.tenant_id == authority.job.tenant_id,
            EventRow.event_id == source.get("feedback_event_id"),
        )
    )
    if feedback_event is None:
        raise WorkflowInvariantError("prior Interaction feedback Event authority drifted")
    try:
        runtime_feedback_event = domain_event_from_data(feedback_event.event_json)
        durable_feedback_event_wire = domain_event_data(runtime_feedback_event)
        public_feedback_event_wire = public_domain_event_data(runtime_feedback_event)
        event_payload = public_feedback_event_wire.pop("payload")
        public_feedback_event_wire["feedback_sha256"] = feedback_sha256
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkflowInvariantError("prior Interaction feedback Event authority drifted") from exc
    if (
        feedback_event.event_json != durable_feedback_event_wire
        or feedback_event.event_json.get("event_type")
        != RuntimeEventType.AGENT_TURN_FEEDBACK_READY.value
        or feedback_event.event_json.get("command_id") != authority.run.command_id
        or event_payload != expected_feedback
        or feedback_event_wire.get("event_id") != feedback_event.event_id
        or feedback_event_wire != public_feedback_event_wire
    ):
        raise WorkflowInvariantError("prior Interaction feedback Event authority drifted")
    learner = await session.scalar(
        select(LearnerProjectionJobRow).where(
            LearnerProjectionJobRow.tenant_id == authority.job.tenant_id,
            LearnerProjectionJobRow.job_id == authority.job.job_id,
        )
    )
    if (
        learner is None
        or learner.status != "SUCCEEDED"
        or learner.result_json is None
        or learner.result_sha256 != workflow_json_sha256(learner.result_json)
        or learner.request_sha256 != workflow_json_sha256(learner.projection_json)
        or learner.actor_id != authority.context.actor.actor_id
        or learner.content_hash != authority.context.content_ref.content_hash
        or learner.source_event_id != feedback_event.event_id
        or learner.projection_json.get("source_feedback_event_id") != feedback_event.event_id
        or learner.projection_json.get("source_evidence_ids")
        != [item.evidence_id for item in authority.run.evidence_refs]
        or learner.projection_json.get("outcome") != outcome_event
        or learner.projection_json.get("final_decision") != decision
        or learner.projection_json.get("outcome_receipt") != _step_receipt_wire(outcome_receipt)
        or learner.projection_json.get("final_decision_receipt")
        != _step_receipt_wire(decision_receipt)
    ):
        raise WorkflowInvariantError("prior Learner projection authority drifted")
    profile = await session.scalar(
        select(LearnerProfileRow).where(
            LearnerProfileRow.tenant_id == learner.tenant_id,
            LearnerProfileRow.learner_id == learner.learner_id,
            LearnerProfileRow.actor_id == learner.actor_id,
            LearnerProfileRow.content_hash == learner.content_hash,
        )
    )
    learner_event = await session.scalar(
        select(EventRow).where(
            EventRow.tenant_id == learner.tenant_id,
            EventRow.stream_id == f"learner:{learner.learner_id}",
            EventRow.sequence == learner.through_sequence,
        )
    )
    result_learner = _object(
        learner.result_json.get("learner"),
        "Learner terminal result",
    )
    evidence_id = _text(result_learner, "evidence_id")
    learner_evidence = await session.scalar(
        select(EvidenceRow).where(
            EvidenceRow.tenant_id == learner.tenant_id,
            EvidenceRow.actor_id == learner.actor_id,
            EvidenceRow.content_hash == learner.content_hash,
            EvidenceRow.command_id == learner.command_id,
            EvidenceRow.evidence_id == evidence_id,
        )
    )
    workspace = await session.scalar(
        select(ProductWorkspaceRow).where(
            ProductWorkspaceRow.tenant_id == learner.tenant_id,
            ProductWorkspaceRow.actor_id == learner.actor_id,
            ProductWorkspaceRow.session_id == learner.session_id,
        )
    )
    profile_value = profile.profile_json if profile is not None else {}
    learner_payload = (
        _object(learner_event.event_json.get("payload"), "Learner update payload")
        if learner_event is not None
        else {}
    )
    if (
        profile is None
        or profile.profile_sha256 != canonical_json_sha256(profile_value)
        or _integer(profile_value, "revision") < learner.expected_revision + 1
        or _integer(profile_value, "projected_through_sequence") < learner.through_sequence
        or learner_event is None
        or learner_event.event_json.get("event_type")
        != RuntimeEventType.LEARNER_MODEL_UPDATED.value
        or learner_event.event_json.get("command_id") != learner.command_id
        or learner_event.event_json.get("causation_id") != learner.source_event_id
        or learner_payload.get("learner_id") != learner.learner_id
        or learner_payload.get("previous_revision") != learner.expected_revision
        or learner_payload.get("learner_revision") != learner.expected_revision + 1
        or learner_payload.get("projected_through_sequence") != learner.through_sequence
        or learner_evidence is None
        or canonical_json_sha256(learner_evidence.evidence_json)
        != _text(result_learner, "evidence_sha256")
        or workspace is None
        or workspace.workspace_json.get("last_interaction_sequence", 0) < interaction.sequence
    ):
        raise WorkflowInvariantError("prior Learner/Product terminal closure drifted")
    await _validate_terminal_learner_authority(
        session,
        authority=authority,
        learner=learner,
        validation_state=validation_state,
    )


async def _validate_terminal_learner_authority(
    session: AsyncSession,
    *,
    authority: ValidatedRunAuthority,
    learner: LearnerProjectionJobRow,
    validation_state: TerminalProjectionValidationState,
) -> None:
    from walnut_backend.workers.turn_projection import (
        validate_terminal_learner_row_in_session,
    )

    try:
        await validate_terminal_learner_row_in_session(
            session,
            current=authority,
            learner_job=learner,
            validation_state=validation_state,
        )
    except LearnerProjectionInvariantError as error:
        raise WorkflowInvariantError(
            "terminal learner projection authority drifted"
        ) from error


def _outcome_role(event_type: str, failure_count: object) -> str:
    if event_type == "task_completed":
        return "book_agent"
    if isinstance(failure_count, bool) or not isinstance(failure_count, int):
        raise WorkflowInvariantError("Run failure outcome has no integer suffix")
    return "bug_agent" if failure_count >= 3 else "teaching_agent"


def run_authority_sha256(value: Mapping[str, Any]) -> str:
    """Hash the immutable Run wire while excluding its terminal feedback projection."""

    immutable = dict(value)
    immutable.pop("agent_feedback", None)
    immutable.pop("updated_at", None)
    return canonical_json_sha256(immutable)


def _step_receipt_wire(receipt: JobStepReceiptRow) -> dict[str, Any]:
    return {
        "receipt_id": receipt.receipt_id,
        "step_name": receipt.step_name,
        "fencing_token": receipt.fencing_token,
        "input_sha256": receipt.input_sha256,
        "output_sha256": receipt.output_sha256,
        "receipt_json": dict(receipt.receipt_json),
        "completed_at": _iso(receipt.completed_at),
    }


def _context_for_command(
    session: AsyncSession,
    fallback: OperationContext,
    command_id: str,
) -> OperationContext:
    # The caller already loaded the command in the same repeatable transaction.
    # Historical turns share actor/content authority; only command_id changes.
    del session
    return OperationContext(
        request_id=fallback.request_id,
        correlation_id=fallback.correlation_id,
        trace_id=fallback.trace_id,
        requested_at=fallback.requested_at,
        actor=fallback.actor,
        content_ref=fallback.content_ref,
        schema_version=fallback.schema_version,
        command_id=command_id,
        causation_id=None,
        deadline_at=None,
    )


def _job_operation_context(job: WorkflowJobRow) -> OperationContext:
    raw = job.job_json.get("request_context")
    if not isinstance(raw, dict):
        raise WorkflowInvariantError("workflow Job lost its request context")
    try:
        origin = request_context_from_data(raw)
        return OperationContext(
            request_id=origin.request_id,
            correlation_id=origin.correlation_id,
            trace_id=origin.trace_id,
            requested_at=origin.requested_at,
            actor=origin.actor,
            content_ref=origin.content_ref,
            schema_version=origin.schema_version,
            command_id=job.command_id,
            causation_id=None,
            deadline_at=None,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise WorkflowInvariantError("workflow Job request context is invalid") from error


def _operation_context(command: CommandRecord) -> OperationContext:
    origin = command.request_context
    return OperationContext(
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


def _same_actor(left: object, right: object) -> bool:
    return (
        getattr(left, "tenant_id", None),
        getattr(left, "actor_id", None),
        getattr(left, "actor_type", None),
    ) == (
        getattr(right, "tenant_id", None),
        getattr(right, "actor_id", None),
        getattr(right, "actor_type", None),
    )


def _request_context(context: OperationContext) -> Any:
    from yaya_agent_contracts import RequestContext

    return RequestContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        trace_id=context.trace_id,
        requested_at=context.requested_at,
        actor=context.actor,
        content_ref=context.content_ref,
        schema_version=context.schema_version,
    )


def _request_context_wire(context: Any) -> dict[str, Any]:
    return {
        "request_id": context.request_id,
        "correlation_id": context.correlation_id,
        "trace_id": context.trace_id,
        "requested_at": context.requested_at.isoformat(),
        "actor": cast(dict[str, Any], json_value(context.actor)),
        "content_ref": cast(dict[str, Any], json_value(context.content_ref)),
        "schema_version": context.schema_version,
    }


def _evidence_ref_wire(reference: EvidenceRef) -> dict[str, Any]:
    value: dict[str, Any] = {
        "evidence_id": reference.evidence_id,
        "evidence_type": reference.evidence_type.value,
        "created_at": _iso(reference.created_at),
    }
    if reference.sha256 is not None:
        value["sha256"] = reference.sha256
    if reference.uri is not None:
        value["uri"] = reference.uri
    return value


def evidence_ref_wire(reference: EvidenceRef) -> dict[str, Any]:
    """Return the exact nullable-field-eliding public EvidenceRef wire."""

    return _evidence_ref_wire(reference)


def _decision_evidence_ref_wire(reference: EvidenceRef) -> dict[str, Any]:
    """Return the exact EvidenceRef shape emitted inside AgentDecision JSON."""

    return cast(dict[str, Any], json_value(reference))


def _decision_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise WorkflowInvariantError("decision timestamp must include a timezone")
    return value.astimezone(UTC).isoformat()


def _world_receipt_wire(value: Any) -> dict[str, Any]:
    return {
        "world_id": value.world_id,
        "previous_revision": value.previous_revision,
        "world_revision": value.world_revision,
        "first_event_sequence": value.first_event_sequence,
        "last_event_sequence": value.last_event_sequence,
        "committed_at": _iso(value.committed_at),
        "state_hash": value.state_hash,
    }


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowInvariantError(f"{label} must be an object")
    return dict(value)


def _text(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise WorkflowInvariantError(f"{key} must be text")
    return item


def _integer(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise WorkflowInvariantError(f"{key} must be an integer")
    return item


def _strings(value: object, label: str) -> tuple[str, ...]:
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        raise WorkflowInvariantError(f"{label} must be an array")
    if any(not isinstance(item, str) for item in value):
        raise WorkflowInvariantError(f"{label} must contain text")
    return tuple(cast(Sequence[str], value))


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise WorkflowInvariantError("timestamp must include a timezone")
    return parsed


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise WorkflowInvariantError("timestamp must include a timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "PostgresRunOutcomeAuthority",
    "ValidatedRunAuthority",
    "decision_feedback_wire",
    "exact_failure_suffix_count",
    "evidence_ref_wire",
    "list_validated_session_runs",
    "load_task_snapshot",
    "load_final_provider_receipts",
    "load_patch_provider_receipts",
    "load_validated_run",
    "run_authority_sha256",
    "validate_final_decision_receipt",
    "validate_canonical_outcome_event",
    "validate_provider_decision_wire",
    "validate_terminal_projection",
]
