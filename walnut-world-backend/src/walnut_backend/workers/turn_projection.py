"""Atomic terminal projection for one fenced Agent Turn workflow."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    AgentTurnFeedback,
    CommandRecord,
    CommandStatus,
    CommandTransition,
    ContractError,
    ErrorCategory,
    EvidenceRef,
    EvidenceType,
    Failure,
    OperationContext,
    RuntimeEvent,
    RuntimeEventType,
    UncommittedEvent,
    canonical_json_sha256,
)
from yaya_agent_runtime import (
    LEARNER_PROJECTION_POLICY_VERSION,
    PEDAGOGY_POLICY_VERSION,
    REVIEW_POLICY_VERSION,
    AgentDecision,
    CompetencyProjection,
    EvidenceStage,
    GameEvent,
    LearnerCompetencySummary,
    LearnerProjectionPolicy,
    PedagogyEvidence,
    PedagogyEvidenceOutcome,
    PedagogyInput,
    PedagogyPolicy,
    ProjectionEvidence,
    ProjectionInput,
    ProjectionOutcome,
    RoleRouter,
    SkillInvocationResult,
    TaskRelation,
)

from walnut_backend.adapters.postgres.command_store import PostgresCommandStore
from walnut_backend.adapters.postgres.event_store import append_events_in_session
from walnut_backend.adapters.postgres.learner_projection_jobs import (
    ClaimedLearnerProjectionJob,
    LearnerProjectionInvariantError,
    PostgresLearnerProjectionJobStore,
)
from walnut_backend.adapters.postgres.models import (
    AgentProfileRow,
    AgentSessionRow,
    AgentTurnRow,
    CommandRow,
    CurrentSessionBindingRow,
    EventRow,
    EvidenceRow,
    JobStepReceiptRow,
    LaunchAuthorityRow,
    LearnerProfileRow,
    LearnerProjectionJobRow,
    ProductContentUnitRow,
    ProductDraftRevisionRow,
    ProductDraftRow,
    ProductInteractionRow,
    ProductSkillPatchEvidenceRow,
    ProductSkillPatchProposalRow,
    ProductSkillPatchRequestRow,
    ProductWorkspaceRow,
    RegistryHeadRow,
    RunRow,
    SkillActivationRow,
    SkillBuildProvenanceRow,
    SkillCertificationRevocationRow,
    SkillCertificationRow,
    SkillRunProvenanceRow,
    WorkflowJobRow,
    WorldSnapshotRow,
    WorldStreamRow,
    command_record_data,
    command_record_from_data,
    domain_event_data,
    domain_event_from_data,
    error_data,
    error_from_data,
    json_value,
    public_domain_event_data,
    request_context_data,
)
from walnut_backend.adapters.postgres.product_drafts import draft_resource
from walnut_backend.adapters.postgres.product_interactions import (
    _interaction_projection_kind,
    _run_interactions_have_authority,
    _skill_patch_interaction_has_authority,
)
from walnut_backend.adapters.postgres.product_workspaces import (
    refresh_workspace_in_session,
    workspace_authority_matches,
)
from walnut_backend.adapters.postgres.run_outcomes import (
    TerminalProjectionValidationState,
    decision_feedback_wire,
    load_final_provider_receipts,
    load_validated_run,
    run_authority_sha256,
    validate_agent_decision_runtime_authority,
    validate_canonical_outcome_event,
    validate_final_decision_receipt,
    validate_provider_decision_wire,
    validate_terminal_projection,
)
from walnut_backend.adapters.postgres.skill_provenance import validate_run_provenance
from walnut_backend.adapters.postgres.workflow_jobs import (
    ClaimedWorkflowJob,
    PostgresWorkflowJobStore,
    WorkflowInvariantError,
    workflow_json_sha256,
    workflow_receipt_sha256,
    workflow_step_receipt_id,
)

if TYPE_CHECKING:
    from walnut_backend.workers.turn_worker import _TurnAuthority


async def project_learner_handoff(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    learner_jobs: PostgresLearnerProjectionJobStore,
    commands: PostgresCommandStore,
    claim: ClaimedLearnerProjectionJob,
    lease_seconds: int,
) -> None:
    """Commit every post-handoff projection under one independent learner fence."""

    async with session_factory() as session, session.begin():
        owned = await learner_jobs.start_in_session(
            session,
            claim,
            lease_seconds=lease_seconds,
        )
        objective = dict(owned.projection)
        parent = await _waiting_parent(session, owned)
        command = await _command(session, owned.command_id, owned.tenant_id)
        context = _operation_context(command)
        current = await load_validated_run(
            session,
            tenant_id=owned.tenant_id,
            actor_id=owned.actor_id,
            content_hash=owned.content_hash,
            command_id=owned.command_id,
            expected_context=context,
            require_current_world=True,
        )
        if current.command != command or current.job.job_id != owned.job_id:
            raise LearnerProjectionInvariantError(
                "learner objective no longer has its exact Run authority"
            )
        feedback_event = await _validate_learner_objective(
            session,
            claim=owned,
            objective=objective,
            command=command,
            current=current,
            parent=parent,
        )
        learner = await session.scalar(
            select(LearnerProfileRow)
            .where(
                LearnerProfileRow.tenant_id == owned.tenant_id,
                LearnerProfileRow.learner_id == owned.learner_id,
                LearnerProfileRow.actor_id == owned.actor_id,
                LearnerProfileRow.content_hash == owned.content_hash,
            )
            .with_for_update()
        )
        session_row = await session.scalar(
            select(AgentSessionRow)
            .where(
                AgentSessionRow.tenant_id == owned.tenant_id,
                AgentSessionRow.actor_id == owned.actor_id,
                AgentSessionRow.session_id == owned.session_id,
                AgentSessionRow.status == "ACTIVE",
            )
            .with_for_update()
        )
        if learner is None or session_row is None:
            raise LearnerProjectionInvariantError(
                "learner hand-off lost its Learner or Session authority"
            )
        projection = _object(objective.get("projection"), "projection objective")
        recorded_at = _datetime(_text(projection, "recorded_at"))
        task = _object(objective.get("task"), "task objective")
        feedback = _object(objective.get("feedback"), "feedback objective")
        decision = _object(objective.get("final_decision"), "final decision objective")
        learner_result = await _project_learner(
            session,
            claim=owned,
            command=command,
            context=context,
            result=current.result,
            learner=learner,
            task_id=_text(task, "task_id"),
            concept=_text(task, "concept"),
            feedback_event=feedback_event,
            recorded_at=recorded_at,
        )
        interaction = await _project_interaction(
            session,
            claim=owned,
            decision=decision,
            context=context,
            session_id=owned.session_id,
            turn_id=owned.turn_id,
            feedback=feedback,
            feedback_sha256=_text(objective, "feedback_sha256"),
            feedback_event=feedback_event,
            committed_at=recorded_at,
            session_row=session_row,
        )
        workspace = await refresh_workspace_in_session(
            session,
            tenant_id=owned.tenant_id,
            actor_id=owned.actor_id,
            session_id=owned.session_id,
            updated_at=recorded_at,
        )
        source = _object(interaction.get("projection_source"), "Interaction source")
        terminal_receipt = await learner_jobs.record_turn_completed_in_session(
            session,
            owned,
            input_sha256=current.result.request_sha256,
            output=source,
        )
        terminal = _terminal_command(
            command,
            current.result,
            objective=objective,
            updated_at=recorded_at,
        )
        transitioned = await commands.transition_in_session(
            session,
            CommandTransition(command, terminal),
            context,
        )
        if isinstance(transitioned, Failure):
            raise LearnerProjectionInvariantError("learner terminal Command CAS was lost")
        await session.flush()
        projection_receipt = await learner_jobs.record_projection_committed_in_session(
            session,
            owned,
            input_sha256=owned.request_sha256,
            output=_projection_commit_authority(
                learner=learner,
                learner_result=learner_result,
                interaction=interaction,
                workspace=workspace,
                command=terminal,
            ),
        )
        closure = _terminal_closure(
            learner=learner,
            learner_result=learner_result,
            interaction=interaction,
            workspace=workspace,
            receipt=terminal_receipt,
            projection_receipt=projection_receipt,
            command=terminal,
        )
        await learner_jobs.complete_in_session(session, owned, result=closure)


async def validate_learner_handoff_terminal(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    claim: ClaimedLearnerProjectionJob,
) -> None:
    """Fail loud unless an ACK-lost commit has one exact terminal closure."""

    async with session_factory() as session:
        learner_job = await session.scalar(
            select(LearnerProjectionJobRow).where(
                LearnerProjectionJobRow.tenant_id == claim.tenant_id,
                LearnerProjectionJobRow.job_id == claim.job_id,
            )
        )
        parent = await session.scalar(
            select(WorkflowJobRow).where(
                WorkflowJobRow.tenant_id == claim.tenant_id,
                WorkflowJobRow.job_id == claim.job_id,
            )
        )
        if (
            learner_job is None
            or parent is None
            or learner_job.status != "SUCCEEDED"
            or learner_job.request_sha256 != claim.request_sha256
            or learner_job.projection_json != dict(claim.projection)
            or learner_job.result_json is None
            or learner_job.result_sha256 != workflow_json_sha256(learner_job.result_json)
            or parent.status != "SUCCEEDED"
            or parent.phase != "COMPLETE"
        ):
            raise LearnerProjectionInvariantError(
                "learner acknowledgement has no canonical terminal rows"
            )
        command = await _command(session, claim.command_id, claim.tenant_id)
        context = _operation_context(command)
        current = await load_validated_run(
            session,
            tenant_id=claim.tenant_id,
            actor_id=claim.actor_id,
            content_hash=claim.content_hash,
            command_id=claim.command_id,
            expected_context=context,
            require_current_world=True,
        )
        await _validate_terminal_objective_core(
            session,
            claim=claim,
            current=current,
        )
        _validate_terminal_command_from_run(command, current.result)
        await validate_terminal_projection(session, current)
        learner = await session.scalar(
            select(LearnerProfileRow).where(
                LearnerProfileRow.tenant_id == claim.tenant_id,
                LearnerProfileRow.learner_id == claim.learner_id,
                LearnerProfileRow.actor_id == claim.actor_id,
                LearnerProfileRow.content_hash == claim.content_hash,
            )
        )
        interaction = await session.scalar(
            select(ProductInteractionRow).where(
                ProductInteractionRow.tenant_id == claim.tenant_id,
                ProductInteractionRow.session_id == claim.session_id,
                ProductInteractionRow.turn_id == claim.turn_id,
            )
        )
        workspace = await session.scalar(
            select(ProductWorkspaceRow).where(
                ProductWorkspaceRow.tenant_id == claim.tenant_id,
                ProductWorkspaceRow.actor_id == claim.actor_id,
                ProductWorkspaceRow.session_id == claim.session_id,
            )
        )
        receipt = await session.scalar(
            select(JobStepReceiptRow).where(
                JobStepReceiptRow.tenant_id == claim.tenant_id,
                JobStepReceiptRow.job_id == claim.job_id,
                JobStepReceiptRow.step_name == "TURN_COMPLETED",
            )
        )
        projection_receipt = await session.scalar(
            select(JobStepReceiptRow).where(
                JobStepReceiptRow.tenant_id == claim.tenant_id,
                JobStepReceiptRow.job_id == claim.job_id,
                JobStepReceiptRow.step_name == "LEARNER_PROJECTION_COMMITTED",
            )
        )
        if (
            learner is None
            or interaction is None
            or workspace is None
            or receipt is None
            or projection_receipt is None
        ):
            raise LearnerProjectionInvariantError("terminal learner projection is incomplete")
        await _validate_projection_chain_head(
            session,
            claim=claim,
            learner=learner,
            workspace=workspace,
        )
        learner_result = await _terminal_learner_result(
            session,
            claim=claim,
            learner=learner,
            command=command,
            result=current.result,
        )
        _validate_terminal_result_closure(
            stored=learner_job.result_json,
            claim=claim,
            learner=learner,
            learner_result=learner_result,
            interaction=dict(interaction.interaction_json),
            workspace=workspace,
            receipt=receipt,
            projection_receipt=projection_receipt,
            command=command,
        )


async def validate_terminal_learner_row_in_session(
    session: AsyncSession,
    *,
    current: Any,
    learner_job: LearnerProjectionJobRow,
    validation_state: TerminalProjectionValidationState | None = None,
) -> None:
    """Strict historical validator shared by ACK recovery and later Turn reads."""

    state = validation_state or TerminalProjectionValidationState()
    state.bind_session(session)
    if (
        learner_job.status != "SUCCEEDED"
        or learner_job.result_json is None
        or learner_job.completed_at is None
        or learner_job.request_sha256 != workflow_json_sha256(learner_job.projection_json)
        or learner_job.result_sha256 != workflow_json_sha256(learner_job.result_json)
    ):
        raise LearnerProjectionInvariantError(
            "terminal learner row has no exact objective/result bytes"
        )
    claim = ClaimedLearnerProjectionJob(
        job_id=learner_job.job_id,
        tenant_id=learner_job.tenant_id,
        command_id=learner_job.command_id,
        session_id=learner_job.session_id,
        turn_id=learner_job.turn_id,
        run_id=learner_job.run_id,
        learner_id=learner_job.learner_id,
        actor_id=learner_job.actor_id,
        content_hash=learner_job.content_hash,
        source_event_id=learner_job.source_event_id,
        expected_revision=learner_job.expected_revision,
        through_sequence=learner_job.through_sequence,
        status="SUCCEEDED",
        attempt=learner_job.attempt,
        fencing_token=learner_job.fencing_token,
        lease_owner=learner_job.lease_owner or "terminal-validator",
        lease_expires_at=learner_job.lease_expires_at or learner_job.completed_at,
        request_sha256=learner_job.request_sha256,
        projection=dict(learner_job.projection_json),
        created_at=learner_job.created_at,
    )
    parent = await session.scalar(
        select(WorkflowJobRow).where(
            WorkflowJobRow.tenant_id == claim.tenant_id,
            WorkflowJobRow.job_id == claim.job_id,
        )
    )
    if parent is None or parent.status != "SUCCEEDED" or parent.phase != "COMPLETE":
        raise LearnerProjectionInvariantError("terminal learner parent is not complete")
    objective = dict(claim.projection)
    preterminal = command_record_from_data(
        _object(objective.get("command"), "hand-off Command objective")
    )
    _validate_preterminal_command(preterminal, current.command, current.result)
    await _validate_terminal_objective_core(session, claim=claim, current=current)
    await _validate_learner_objective(
        session,
        claim=claim,
        objective=objective,
        command=preterminal,
        current=current,
        parent=parent,
        validation_state=state,
    )
    learner = await session.scalar(
        select(LearnerProfileRow).where(
            LearnerProfileRow.tenant_id == claim.tenant_id,
            LearnerProfileRow.learner_id == claim.learner_id,
            LearnerProfileRow.actor_id == claim.actor_id,
            LearnerProfileRow.content_hash == claim.content_hash,
        )
    )
    interaction = await session.scalar(
        select(ProductInteractionRow).where(
            ProductInteractionRow.tenant_id == claim.tenant_id,
            ProductInteractionRow.actor_id == claim.actor_id,
            ProductInteractionRow.session_id == claim.session_id,
            ProductInteractionRow.turn_id == claim.turn_id,
        )
    )
    workspace = await session.scalar(
        select(ProductWorkspaceRow).where(
            ProductWorkspaceRow.tenant_id == claim.tenant_id,
            ProductWorkspaceRow.actor_id == claim.actor_id,
            ProductWorkspaceRow.session_id == claim.session_id,
        )
    )
    receipt = await session.scalar(
        select(JobStepReceiptRow).where(
            JobStepReceiptRow.tenant_id == claim.tenant_id,
            JobStepReceiptRow.job_id == claim.job_id,
            JobStepReceiptRow.step_name == "TURN_COMPLETED",
        )
    )
    projection_receipt = await session.scalar(
        select(JobStepReceiptRow).where(
            JobStepReceiptRow.tenant_id == claim.tenant_id,
            JobStepReceiptRow.job_id == claim.job_id,
            JobStepReceiptRow.step_name == "LEARNER_PROJECTION_COMMITTED",
        )
    )
    if (
        learner is None
        or interaction is None
        or workspace is None
        or receipt is None
        or projection_receipt is None
    ):
        raise LearnerProjectionInvariantError("terminal learner projection is incomplete")
    await _validate_projection_chain_head(
        session,
        claim=claim,
        learner=learner,
        workspace=workspace,
    )
    learner_result = await _terminal_learner_result(
        session,
        claim=claim,
        learner=learner,
        command=current.command,
        result=current.result,
    )
    _validate_terminal_result_closure(
        stored=learner_job.result_json,
        claim=claim,
        learner=learner,
        learner_result=learner_result,
        interaction=dict(interaction.interaction_json),
        workspace=workspace,
        receipt=receipt,
        projection_receipt=projection_receipt,
        command=current.command,
    )


async def _validate_projection_chain_head(
    session: AsyncSession,
    *,
    claim: ClaimedLearnerProjectionJob,
    learner: LearnerProfileRow,
    workspace: ProductWorkspaceRow,
) -> None:
    """Replay immutable projection receipts through the current mutable heads."""

    rows = list(
        (
            await session.scalars(
                select(LearnerProjectionJobRow)
                .where(
                    LearnerProjectionJobRow.tenant_id == claim.tenant_id,
                    LearnerProjectionJobRow.learner_id == claim.learner_id,
                    LearnerProjectionJobRow.actor_id == claim.actor_id,
                    LearnerProjectionJobRow.content_hash == claim.content_hash,
                    LearnerProjectionJobRow.status == "SUCCEEDED",
                )
                .order_by(LearnerProjectionJobRow.expected_revision)
            )
        ).all()
    )
    current_profile = dict(learner.profile_json)
    current_revision = _integer(current_profile, "revision")
    if len(rows) != current_revision or current_revision < claim.expected_revision + 1:
        raise LearnerProjectionInvariantError("Learner projection receipt chain has a gap")
    previous_profile: dict[str, Any] | None = None
    previous_workspace: dict[str, Any] | None = None
    previous_interaction_sequences: dict[str, int] = {}
    for ordinal, row in enumerate(rows):
        if (
            row.expected_revision != ordinal
            or row.through_sequence != ordinal + 1
            or row.result_json is None
            or row.request_sha256 != workflow_json_sha256(row.projection_json)
            or row.result_sha256 != workflow_json_sha256(row.result_json)
        ):
            raise LearnerProjectionInvariantError(
                "Learner projection receipt chain is not contiguous"
            )
        projection_receipt = await session.scalar(
            select(JobStepReceiptRow).where(
                JobStepReceiptRow.tenant_id == row.tenant_id,
                JobStepReceiptRow.job_id == row.job_id,
                JobStepReceiptRow.step_name == "LEARNER_PROJECTION_COMMITTED",
            )
        )
        if (
            projection_receipt is None
            or projection_receipt.receipt_id
            != workflow_step_receipt_id(
                row.tenant_id,
                row.job_id,
                "LEARNER_PROJECTION_COMMITTED",
            )
            or projection_receipt.input_sha256 != row.request_sha256
            or projection_receipt.output_sha256
            != workflow_receipt_sha256(projection_receipt.receipt_json)
            or row.result_json.get("projection_receipt") != _step_receipt_wire(projection_receipt)
        ):
            raise LearnerProjectionInvariantError(
                "Learner projection receipt chain lost immutable bytes"
            )
        commit = projection_receipt.receipt_json
        learner_commit = _object(commit.get("learner"), "chain Learner authority")
        profile = _object(learner_commit.get("profile"), "chain Learner profile")
        learner_projection = _object(learner_commit.get("projection"), "chain Learner projection")
        workspace_commit = _object(commit.get("workspace"), "chain Workspace authority")
        workspace_value = _object(workspace_commit.get("workspace"), "chain Workspace")
        interaction_commit = _object(commit.get("interaction"), "chain Interaction authority")
        interaction = _object(interaction_commit.get("interaction"), "chain Interaction")
        projection_objective = _object(
            row.projection_json.get("projection"),
            "chain Interaction objective",
        )
        interaction_source = _object(
            interaction.get("projection_source"),
            "chain Interaction projection source",
        )
        interaction_id = _text(projection_objective, "interaction_id")
        interaction_sequence = _integer(projection_objective, "interaction_sequence")
        durable_interaction = await session.scalar(
            select(ProductInteractionRow).where(
                ProductInteractionRow.tenant_id == row.tenant_id,
                ProductInteractionRow.actor_id == row.actor_id,
                ProductInteractionRow.session_id == row.session_id,
                ProductInteractionRow.interaction_id == interaction_id,
            )
        )
        interaction_prefix_count = await session.scalar(
            select(func.count(ProductInteractionRow.interaction_row_id)).where(
                ProductInteractionRow.tenant_id == row.tenant_id,
                ProductInteractionRow.actor_id == row.actor_id,
                ProductInteractionRow.session_id == row.session_id,
                ProductInteractionRow.sequence <= interaction_sequence,
            )
        )
        previous_interaction_sequence = previous_interaction_sequences.get(row.session_id)
        update = _object(learner_projection.get("learner_update"), "chain Learner update")
        profile_refs = profile.get("evidence_refs")
        update_refs = update.get("evidence_refs")
        if (
            learner_commit.get("profile_sha256") != canonical_json_sha256(profile)
            or learner_commit.get("projection_sha256") != canonical_json_sha256(learner_projection)
            or _integer(profile, "revision") != ordinal + 1
            or _integer(profile, "projected_through_sequence") != ordinal + 1
            or profile.get("model_version") != LEARNER_PROJECTION_POLICY_VERSION
            or profile.get("review_policy_version") != REVIEW_POLICY_VERSION
            or not isinstance(profile_refs, list)
            or not isinstance(update_refs, list)
            or not update_refs
            or update.get("previous_revision") != ordinal
            or update.get("learner_revision") != ordinal + 1
            or update.get("projected_through_sequence") != ordinal + 1
            or profile.get("updated_at") != update.get("updated_at")
            or interaction_commit.get("interaction_sha256") != canonical_json_sha256(interaction)
            or interaction.get("interaction_id") != interaction_id
            or interaction.get("sequence") != interaction_sequence
            or interaction.get("session_id") != row.session_id
            or interaction.get("turn_id") != row.turn_id
            or interaction_source.get("command_id") != row.command_id
            or durable_interaction is None
            or durable_interaction.turn_id != row.turn_id
            or durable_interaction.sequence != interaction_sequence
            or durable_interaction.interaction_json != interaction
            or interaction_prefix_count != interaction_sequence
            or (
                previous_interaction_sequence is not None
                and interaction_sequence <= previous_interaction_sequence
            )
            or workspace_commit.get("workspace_revision")
            != _integer(workspace_value, "workspace_revision")
            or workspace_commit.get("workspace_sha256") != canonical_json_sha256(workspace_value)
            or workspace_value.get("last_interaction_sequence") != interaction_sequence
        ):
            raise LearnerProjectionInvariantError("Learner projection receipt chain bytes drifted")
        previous_interaction_sequences[row.session_id] = interaction_sequence
        prior_refs = [] if previous_profile is None else previous_profile.get("evidence_refs")
        terminal_objective = _object(
            row.projection_json.get("terminal_command"),
            "chain terminal Command objective",
        )
        source_refs = terminal_objective.get("evidence_refs")
        source_ids = [
            item["evidence_id"]
            for item in update_refs
            if isinstance(item, Mapping) and isinstance(item.get("evidence_id"), str)
        ]
        if (
            not isinstance(prior_refs, list)
            or update_refs != source_refs
            or learner_projection.get("source_evidence_ids") != source_ids
            or row.projection_json.get("source_evidence_ids") != source_ids
            or profile_refs != _merge_learner_evidence_catalog(prior_refs, update_refs)
        ):
            raise LearnerProjectionInvariantError(
                "Learner Evidence catalog does not follow its immutable chain"
            )
        _validate_learner_profile_evidence_catalog(profile)
        previous_competencies = (
            {}
            if previous_profile is None
            else _object(previous_profile.get("competencies"), "prior Learner competencies")
        )
        competencies = _object(profile.get("competencies"), "Learner competencies")
        changed = update.get("changed_competency_ids")
        actual_changed = sorted(
            key
            for key in set(previous_competencies) | set(competencies)
            if previous_competencies.get(key) != competencies.get(key)
        )
        task_objective = _object(row.projection_json.get("task"), "chain Learner task")
        if (
            not isinstance(changed, list)
            or changed != actual_changed
            or task_objective.get("concept") not in changed
        ):
            raise LearnerProjectionInvariantError(
                "Learner competency chain changed outside its declared catalog compaction"
            )
        if previous_workspace is not None:
            if (
                _integer(workspace_value, "workspace_revision")
                <= _integer(previous_workspace, "workspace_revision")
                or workspace_value.get("workspace_id") != previous_workspace.get("workspace_id")
                or workspace_value.get("content_ref") != previous_workspace.get("content_ref")
                or not _workspace_session_chain_matches(
                    previous_workspace.get("session"),
                    workspace_value.get("session"),
                )
                or not _workspace_world_chain_matches(
                    previous_workspace.get("world_checkpoint"),
                    workspace_value.get("world_checkpoint"),
                )
                or not _workspace_draft_chain_matches(
                    previous_workspace.get("skill_draft_refs"),
                    workspace_value.get("skill_draft_refs"),
                )
                or not _workspace_static_chain_matches(
                    previous_workspace,
                    workspace_value,
                )
            ):
                raise LearnerProjectionInvariantError(
                    "Workspace immutable projection chain drifted"
                )
        previous_profile = profile
        previous_workspace = workspace_value
    if previous_profile != current_profile or learner.profile_sha256 != canonical_json_sha256(
        current_profile
    ):
        raise LearnerProjectionInvariantError(
            "Learner mutable head differs from immutable projection receipts"
        )
    if previous_workspace is None:
        raise LearnerProjectionInvariantError("Workspace projection receipt chain is empty")
    await _validate_current_workspace_head(
        session,
        claim=claim,
        workspace=workspace,
        last_projection=previous_workspace,
    )


async def _validate_current_workspace_head(
    session: AsyncSession,
    *,
    claim: ClaimedLearnerProjectionJob,
    workspace: ProductWorkspaceRow,
    last_projection: Mapping[str, Any],
) -> None:
    """Close the mutable workspace against its live durable authorities.

    A later Turn acceptance or Draft write legitimately refreshes the workspace
    after an earlier learner receipt was frozen.  Historical replay therefore
    keeps every receipt byte/hash exact, but proves the newer mutable head from
    Session, World, Draft and Interaction authorities instead of requiring it
    to equal the last learner receipt snapshot.
    """

    owner = await session.scalar(
        select(AgentSessionRow).where(
            AgentSessionRow.tenant_id == claim.tenant_id,
            AgentSessionRow.actor_id == claim.actor_id,
            AgentSessionRow.session_id == claim.session_id,
        )
    )
    snapshot = (
        await session.scalar(
            select(WorldSnapshotRow).where(
                WorldSnapshotRow.tenant_id == claim.tenant_id,
                WorldSnapshotRow.actor_id == claim.actor_id,
                WorldSnapshotRow.world_id == owner.world_id,
                WorldSnapshotRow.content_hash == claim.content_hash,
            )
        )
        if owner is not None
        else None
    )
    drafts = list(
        await session.scalars(
            select(ProductDraftRow)
            .where(
                ProductDraftRow.tenant_id == claim.tenant_id,
                ProductDraftRow.actor_id == claim.actor_id,
                ProductDraftRow.session_id == claim.session_id,
            )
            .order_by(ProductDraftRow.skill_id, ProductDraftRow.draft_id)
        )
    )
    interaction_high_watermark = await session.scalar(
        select(func.coalesce(func.max(ProductInteractionRow.sequence), 0)).where(
            ProductInteractionRow.tenant_id == claim.tenant_id,
            ProductInteractionRow.actor_id == claim.actor_id,
            ProductInteractionRow.session_id == claim.session_id,
        )
    )
    current = workspace.workspace_json
    if (
        owner is None
        or snapshot is None
        or not workspace_authority_matches(
            workspace,
            owner,
            snapshot,
            drafts,
            int(interaction_high_watermark or 0),
        )
        or workspace.workspace_revision < _integer(last_projection, "workspace_revision")
        or current.get("workspace_id") != last_projection.get("workspace_id")
        or current.get("content_ref") != last_projection.get("content_ref")
        or _integer(current, "last_interaction_sequence")
        < _integer(last_projection, "last_interaction_sequence")
    ):
        raise LearnerProjectionInvariantError(
            "Workspace mutable head has no exact durable authority"
        )


def _workspace_session_chain_matches(previous: object, current: object) -> bool:
    if not isinstance(previous, Mapping) or not isinstance(current, Mapping):
        return False
    mutable = {"last_turn_sequence", "updated_at"}
    previous_stable = {key: value for key, value in previous.items() if key not in mutable}
    current_stable = {key: value for key, value in current.items() if key not in mutable}
    previous_sequence = previous.get("last_turn_sequence", 0)
    current_sequence = current.get("last_turn_sequence", 0)
    return (
        previous_stable == current_stable
        and isinstance(previous_sequence, int)
        and not isinstance(previous_sequence, bool)
        and isinstance(current_sequence, int)
        and not isinstance(current_sequence, bool)
        and current_sequence >= previous_sequence
    )


def _workspace_draft_chain_matches(previous: object, current: object) -> bool:
    if not isinstance(previous, list) or not isinstance(current, list):
        return False
    if not all(isinstance(item, Mapping) for item in (*previous, *current)):
        return False
    previous_by_id = {item.get("draft_id"): item for item in previous}
    current_by_id = {item.get("draft_id"): item for item in current}
    if None in previous_by_id or None in current_by_id or set(previous_by_id) != set(current_by_id):
        return False
    for draft_id, previous_ref in previous_by_id.items():
        current_ref = current_by_id[draft_id]
        previous_revision = previous_ref.get("revision")
        current_revision = current_ref.get("revision")
        if (
            previous_ref.get("skill_id") != current_ref.get("skill_id")
            or previous_ref.get("url") != current_ref.get("url")
            or isinstance(previous_revision, bool)
            or not isinstance(previous_revision, int)
            or isinstance(current_revision, bool)
            or not isinstance(current_revision, int)
            or current_revision < previous_revision
            or (
                current_revision == previous_revision
                and previous_ref.get("draft_sha256") != current_ref.get("draft_sha256")
            )
        ):
            return False
    return True


def _workspace_world_chain_matches(previous: object, current: object) -> bool:
    if not isinstance(previous, Mapping) or not isinstance(current, Mapping):
        return False
    previous_revision = previous.get("world_revision")
    current_revision = current.get("world_revision")
    previous_sequence = previous.get("last_event_sequence")
    current_sequence = current.get("last_event_sequence")
    return (
        previous.get("world_id") == current.get("world_id")
        and isinstance(previous_revision, int)
        and not isinstance(previous_revision, bool)
        and isinstance(current_revision, int)
        and not isinstance(current_revision, bool)
        and current_revision >= previous_revision
        and isinstance(previous_sequence, int)
        and not isinstance(previous_sequence, bool)
        and isinstance(current_sequence, int)
        and not isinstance(current_sequence, bool)
        and current_sequence >= previous_sequence
        and (
            current_revision != previous_revision
            or (
                current_sequence == previous_sequence
                and current.get("state_hash") == previous.get("state_hash")
            )
        )
    )


def _workspace_static_chain_matches(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> bool:
    mutable = {
        "workspace_revision",
        "session",
        "world_checkpoint",
        "skill_draft_refs",
        "last_interaction_sequence",
        "updated_at",
    }
    return {key: value for key, value in previous.items() if key not in mutable} == {
        key: value for key, value in current.items() if key not in mutable
    } and _datetime(_text(previous, "updated_at")) <= _datetime(_text(current, "updated_at"))


def _validate_preterminal_command(
    preterminal: CommandRecord,
    terminal: CommandRecord,
    result: SkillInvocationResult,
) -> None:
    expected_status = (
        CommandStatus.APPLYING_WORLD if result.run.task_success else CommandStatus.RUNNING_SANDBOX
    )
    expected_stage = "WORLD_COMMIT" if result.run.task_success else "SANDBOX"
    expected_terminal_links = {
        **dict(preterminal.links),
        "world_snapshot": f"/v1/worlds/{result.run.world_id}/snapshot",
    }
    if (
        preterminal.command_id != terminal.command_id
        or preterminal.command_type != terminal.command_type
        or preterminal.request_context != terminal.request_context
        or preterminal.versions != terminal.versions
        or preterminal.accepted_at != terminal.accepted_at
        or preterminal.status is not expected_status
        or preterminal.stage != expected_stage
        or preterminal.terminal
        or preterminal.result is not None
        or preterminal.error is not None
        or preterminal.evidence_refs
        or preterminal.revision + 1 != terminal.revision
        or preterminal.links.get("run") != f"/v1/runs/{result.run.run_id}"
        or dict(terminal.links) != expected_terminal_links
    ):
        raise LearnerProjectionInvariantError(
            "hand-off Command objective differs from terminal Command authority"
        )


async def finish_turn_projection(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    commands: PostgresCommandStore,
    jobs: PostgresWorkflowJobStore,
    authority: _TurnAuthority,
    outcome: GameEvent,
    decision: AgentDecision,
    result: SkillInvocationResult,
    lease_seconds: int,
) -> None:
    """Publish final feedback and atomically hand off learner/Product projection."""

    learner_jobs = PostgresLearnerProjectionJobStore(session_factory)
    async with session_factory() as session, session.begin():
        existing = await session.scalar(
            select(LearnerProjectionJobRow).where(
                LearnerProjectionJobRow.tenant_id == authority.claim.tenant_id,
                LearnerProjectionJobRow.job_id == authority.claim.job_id,
            )
        )
        if existing is not None:
            if (
                existing.command_id != authority.claim.command_id
                or existing.turn_id != authority.event.turn_id
                or existing.status
                not in {
                    "READY",
                    "CLAIMED",
                    "RUNNING",
                    "RETRY_WAIT",
                    "SUCCEEDED",
                    "DEAD_LETTER",
                }
            ):
                raise LearnerProjectionInvariantError(
                    "existing learner hand-off conflicts with the Turn authority"
                )
            # A prior transaction committed the hand-off but its acknowledgement
            # was lost. Never replay feedback or re-enqueue; the independent
            # learner process owns every state from here.
            return
        claim = await jobs.start_step_in_session(
            session,
            authority.claim,
            phase="LEARNER_HANDOFF",
            lease_seconds=lease_seconds,
        )
        command = await _command(session, claim.command_id, claim.tenant_id)
        context = _operation_context(command)
        run_row, learner, session_row = await _close_authority(
            session,
            authority=authority,
            command=command,
            context=context,
            outcome=outcome,
            decision=decision,
            result=result,
        )
        # Freeze one causal timestamp for every later learner-owned write.  The
        # accepted Command can originate from a host clock slightly ahead of
        # PostgreSQL, so a bare clock_timestamp() is not necessarily a valid
        # CommandTransition timestamp.
        now = max(
            await _database_now(session),
            command.updated_at,
            decision.completed_at,
            authority.event.occurred_at,
            run_row.created_at,
            learner.updated_at,
            session_row.updated_at,
            *(item.created_at for item in result.run.evidence_refs),
        )

        feedback = _feedback(authority, decision, result)
        feedback_wire = _feedback_wire(feedback)
        feedback_sha256 = canonical_json_sha256(feedback_wire)
        feedback_event = await _append_feedback_event(
            session,
            authority=authority,
            context=context,
            result=result,
            feedback=feedback_wire,
            occurred_at=decision.completed_at,
        )

        run_wire = dict(run_row.run_json)
        if run_wire.get("agent_feedback") is not None:
            raise WorkflowInvariantError("Run already has a conflicting Agent feedback projection")
        run_wire["agent_feedback"] = feedback_wire
        run_wire["updated_at"] = _timestamp(decision.completed_at)
        run_row.run_json = run_wire
        await session.flush()
        objective, expected_revision, through_sequence = await _learner_objective(
            session,
            authority=authority,
            claim=claim,
            command=command,
            result=result,
            outcome=outcome,
            decision=decision,
            learner=learner,
            session_row=session_row,
            run_row=run_row,
            feedback=feedback_wire,
            feedback_sha256=feedback_sha256,
            feedback_event=feedback_event,
            recorded_at=now,
        )
        await learner_jobs.enqueue_and_handoff_in_session(
            session,
            claim,
            command_id=command.command_id,
            session_id=authority.event.session_id,
            turn_id=authority.event.turn_id,
            run_id=result.run.run_id,
            learner_id=authority.learner_id,
            actor_id=context.actor.actor_id,
            content_hash=context.content_ref.content_hash,
            source_event_id=feedback_event.event_id,
            expected_revision=expected_revision,
            through_sequence=through_sequence,
            projection=objective,
            recorded_at=now,
        )


async def finish_skill_patch_proposal(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    commands: PostgresCommandStore,
    jobs: PostgresWorkflowJobStore,
    authority: _TurnAuthority,
    decision: AgentDecision,
    lease_seconds: int,
) -> None:
    """Atomically publish one no-Run Patch proposal from a selected failure."""

    proposal = decision.draft.skill_patch
    if proposal is None or decision.response_type != "skill_patch":
        raise WorkflowInvariantError("Patch projection requires one typed proposal")
    agent_wire = cast(dict[str, Any], json_value(proposal))
    async with session_factory() as session, session.begin():
        reservation = await session.scalar(
            select(ProductSkillPatchRequestRow)
            .where(
                ProductSkillPatchRequestRow.tenant_id == authority.claim.tenant_id,
                ProductSkillPatchRequestRow.command_id == authority.command.command_id,
                ProductSkillPatchRequestRow.requested_interaction_id
                == proposal.failed.interaction_id,
            )
            .with_for_update()
        )
        existing = await session.scalar(
            select(ProductSkillPatchProposalRow).where(
                ProductSkillPatchProposalRow.tenant_id == authority.claim.tenant_id,
                ProductSkillPatchProposalRow.request_command_id
                == authority.command.command_id,
            )
        )
        if reservation is not None and reservation.status == "PROPOSED":
            replay_owner = await session.scalar(
                select(AgentSessionRow).where(
                    AgentSessionRow.tenant_id == reservation.tenant_id,
                    AgentSessionRow.actor_id == reservation.actor_id,
                    AgentSessionRow.session_id == reservation.session_id,
                )
            )
            replay_interaction = (
                await session.scalar(
                    select(ProductInteractionRow).where(
                        ProductInteractionRow.tenant_id == reservation.tenant_id,
                        ProductInteractionRow.actor_id == reservation.actor_id,
                        ProductInteractionRow.session_id == reservation.session_id,
                        ProductInteractionRow.interaction_id == existing.interaction_id,
                    )
                )
                if existing is not None
                else None
            )
            if (
                existing is None
                or replay_owner is None
                or replay_interaction is None
                or reservation.proposal_id != existing.patch_id
                or existing.agent_proposal_id != proposal.proposal_id
                or existing.agent_proposal_sha256 != proposal.proposal_sha256
                or not await _skill_patch_interaction_has_authority(
                    session, replay_interaction, replay_owner
                )
            ):
                raise WorkflowInvariantError("Patch proposal ACK recovery authority drifted")
            return
        claim = await jobs.start_step_in_session(
            session,
            authority.claim,
            phase="PRODUCT_PROJECTION",
            lease_seconds=lease_seconds,
        )
        command = await _command(session, claim.command_id, claim.tenant_id)
        context = _operation_context(command)
        if (
            reservation is None
            or reservation.status != "PENDING"
            or reservation.proposal_id is not None
            or reservation.actor_id != context.actor.actor_id
            or reservation.session_id != authority.event.session_id
            or reservation.turn_id != authority.event.turn_id
            or reservation.authority_sha256
            != canonical_json_sha256(cast(dict[str, Any], json_value(authority.event)))
            or command.status is not CommandStatus.VALIDATING
            or command.stage != "POLICY"
            or command.terminal
        ):
            raise WorkflowInvariantError("Patch request reservation or Command drifted")
        owner = await session.scalar(
            select(AgentSessionRow)
            .where(
                AgentSessionRow.tenant_id == claim.tenant_id,
                AgentSessionRow.actor_id == context.actor.actor_id,
                AgentSessionRow.session_id == authority.event.session_id,
                AgentSessionRow.status == "ACTIVE",
            )
            .with_for_update()
        )
        request_turn = await session.scalar(
            select(AgentTurnRow).where(
                AgentTurnRow.tenant_id == claim.tenant_id,
                AgentTurnRow.actor_id == context.actor.actor_id,
                AgentTurnRow.session_id == authority.event.session_id,
                AgentTurnRow.turn_id == authority.event.turn_id,
                AgentTurnRow.command_id == command.command_id,
            )
        )
        selected = await session.scalar(
            select(ProductInteractionRow).where(
                ProductInteractionRow.tenant_id == claim.tenant_id,
                ProductInteractionRow.actor_id == context.actor.actor_id,
                ProductInteractionRow.session_id == authority.event.session_id,
                ProductInteractionRow.interaction_id == proposal.failed.interaction_id,
                ProductInteractionRow.sequence == proposal.failed.interaction_sequence,
                ProductInteractionRow.interaction_revision
                == proposal.failed.interaction_revision,
            )
        )
        current_draft = await session.scalar(
            select(ProductDraftRow)
            .where(
                ProductDraftRow.tenant_id == claim.tenant_id,
                ProductDraftRow.actor_id == context.actor.actor_id,
                ProductDraftRow.session_id == authority.event.session_id,
                ProductDraftRow.draft_id == proposal.target.draft_id,
            )
            .with_for_update()
        )
        base = await session.scalar(
            select(ProductDraftRevisionRow).where(
                ProductDraftRevisionRow.tenant_id == claim.tenant_id,
                ProductDraftRevisionRow.actor_id == context.actor.actor_id,
                ProductDraftRevisionRow.session_id == authority.event.session_id,
                ProductDraftRevisionRow.draft_id == proposal.target.draft_id,
                ProductDraftRevisionRow.revision == proposal.target.draft_revision,
                ProductDraftRevisionRow.draft_sha256 == proposal.target.draft_sha256,
            )
        )
        build_provenance = await session.scalar(
            select(SkillBuildProvenanceRow).where(
                SkillBuildProvenanceRow.build_id == proposal.failed.build_id,
                SkillBuildProvenanceRow.tenant_id == claim.tenant_id,
                SkillBuildProvenanceRow.actor_id == context.actor.actor_id,
                SkillBuildProvenanceRow.session_id == authority.event.session_id,
                SkillBuildProvenanceRow.draft_revision_row_id
                == (base.draft_revision_row_id if base is not None else -1),
            )
        )
        run_provenance = await session.scalar(
            select(SkillRunProvenanceRow).where(
                SkillRunProvenanceRow.run_id == proposal.failed.run_id,
                SkillRunProvenanceRow.build_id == proposal.failed.build_id,
                SkillRunProvenanceRow.tenant_id == claim.tenant_id,
                SkillRunProvenanceRow.actor_id == context.actor.actor_id,
                SkillRunProvenanceRow.session_id == authority.event.session_id,
            )
        )
        evidence_ids = [item.evidence_id for item in proposal.failed.evidence_refs]
        evidence_rows = list(
            (
                await session.scalars(
                    select(EvidenceRow).where(
                        EvidenceRow.tenant_id == claim.tenant_id,
                        EvidenceRow.actor_id == context.actor.actor_id,
                        EvidenceRow.evidence_id.in_(evidence_ids),
                    )
                )
            ).all()
        )
        side_effect_receipts = list(
            (
                await session.scalars(
                    select(JobStepReceiptRow).where(
                        JobStepReceiptRow.tenant_id == claim.tenant_id,
                        JobStepReceiptRow.job_id == claim.job_id,
                        JobStepReceiptRow.step_name.in_(
                            ("SKILL_INVOKED", "SANDBOX_DISPATCHED", "WORLD_COMMITTED")
                        ),
                    )
                )
            ).all()
        )
        request_runs = list(
            (
                await session.scalars(
                    select(RunRow).where(
                        RunRow.tenant_id == claim.tenant_id,
                        RunRow.actor_id == context.actor.actor_id,
                        RunRow.session_id == authority.event.session_id,
                        RunRow.command_id == command.command_id,
                    )
                )
            ).all()
        )
        if (
            owner is None
            or request_turn is None
            or selected is None
            or current_draft is None
            or base is None
            or build_provenance is None
            or run_provenance is None
            or current_draft.revision != base.revision
            or current_draft.draft_sha256 != base.draft_sha256
            or current_draft.draft_json != base.draft_json
            or base.skill_id != proposal.target.skill_id
            or base.source_bundle_sha256 != proposal.target.source_bundle_sha256
            or base.entrypoint != proposal.target.entrypoint
            or build_provenance.draft_revision_row_id != base.draft_revision_row_id
            or build_provenance.draft_sha256 != base.draft_sha256
            or run_provenance.draft_revision_row_id != base.draft_revision_row_id
            or run_provenance.draft_sha256 != base.draft_sha256
            or len(evidence_rows) != len(evidence_ids)
            or request_turn.turn_sequence != owner.session_json.get("last_turn_sequence")
            or side_effect_receipts
            or request_runs
            or not await _run_interactions_have_authority(session, [selected], owner)
        ):
            raise WorkflowInvariantError(
                "Patch proposal selected failure or Draft provenance drifted"
            )
        evidence_by_id = {row.evidence_id: row for row in evidence_rows}
        evidence_wire = [_evidence_ref_wire(item) for item in proposal.failed.evidence_refs]
        if any(
            evidence_by_id[item.evidence_id].evidence_json.get("evidence_ref")
            != _evidence_ref_wire(item)
            for item in proposal.failed.evidence_refs
        ):
            raise WorkflowInvariantError("Patch proposal Evidence bytes drifted")
        interaction_sequence = int(
            await session.scalar(
                select(func.max(ProductInteractionRow.sequence)).where(
                    ProductInteractionRow.tenant_id == claim.tenant_id,
                    ProductInteractionRow.actor_id == context.actor.actor_id,
                    ProductInteractionRow.session_id == authority.event.session_id,
                )
            )
            or 0
        ) + 1
        if selected.sequence != interaction_sequence - 1:
            raise WorkflowInvariantError("selected failure is no longer the current Interaction")
        now = max(
            await _database_now(session),
            command.updated_at,
            decision.completed_at,
            request_turn.created_at,
            base.created_at,
            *(item.created_at for item in proposal.failed.evidence_refs),
        )
        interaction_id = _identifier("interaction", claim.tenant_id, claim.job_id)
        public_patch_id = "patch_" + hashlib.sha256(
            "\x00".join(
                (
                    claim.tenant_id,
                    command.command_id,
                    proposal.proposal_id,
                    proposal.proposal_sha256,
                )
            ).encode("utf-8")
        ).hexdigest()[:32]
        operation = {
            "operation": "UPSERT_FILE",
            "path": proposal.operation.path,
            "previous_content_sha256": proposal.operation.previous_content_sha256,
            "content": proposal.operation.content,
            "content_sha256": proposal.operation.content_sha256,
        }
        source_bundle = cast(dict[str, Any], base.draft_json["source_bundle"])
        projected_bundle = {
            **source_bundle,
            "files": [
                {
                    "path": operation["path"],
                    "content": operation["content"],
                    "content_sha256": operation["content_sha256"],
                }
                if item.get("path") == operation["path"]
                else dict(item)
                for item in cast(list[dict[str, Any]], source_bundle["files"])
            ],
        }
        result_draft = draft_resource(
            {
                "session_id": base.session_id,
                "draft_id": base.draft_id,
                "skill_id": base.skill_id,
                "content_ref": base.draft_json["content_ref"],
                "display_name": base.draft_json["display_name"],
                "source_bundle": projected_bundle,
            },
            cast(Mapping[str, Any], base.draft_json["request_context"]),
            base.revision + 1,
            cast(datetime, current_draft.created_at),
            now,
            public_patch_id,
        )
        patch: dict[str, Any] = {
            "patch_id": public_patch_id,
            "interaction_id": interaction_id,
            "session_id": base.session_id,
            "turn_id": authority.event.turn_id,
            "draft_id": base.draft_id,
            "skill_id": base.skill_id,
            "base_draft_revision": base.revision,
            "base_draft_sha256": base.draft_sha256,
            "operations": [operation],
            "result_draft_sha256": result_draft["draft_sha256"],
            "rationale": proposal.rationale,
            "requires_student_confirmation": True,
            "evidence_refs": evidence_wire,
            "created_at": _timestamp(now),
        }
        patch["patch_sha256"] = canonical_json_sha256(patch)
        feedback = {
            "session_id": authority.event.session_id,
            "turn_id": authority.event.turn_id,
            "command_id": command.command_id,
            "run_id": None,
            "message_key": decision.message_key,
            "message": decision.message,
            "source": decision.source,
            "degraded": decision.degraded,
            "fallback_reason": decision.fallback_reason,
            "evidence_refs": evidence_wire,
            "completed_at": _timestamp(decision.completed_at),
        }
        feedback_sha256 = canonical_json_sha256(feedback)
        stream_id = f"agent-session:{authority.event.session_id}"
        appended = await append_events_in_session(
            session,
            stream_id,
            await _stream_sequence(session, claim.tenant_id, stream_id),
            (
                UncommittedEvent(
                    event_type=RuntimeEventType.AGENT_TURN_FEEDBACK_READY.value,
                    event_version=1,
                    producer="walnut_agent_runtime",
                    trace_id=context.trace_id,
                    command_id=command.command_id,
                    correlation_id=context.correlation_id,
                    causation_id=proposal.failed.feedback_event_id,
                    content_ref=context.content_ref,
                    payload=feedback,
                ),
            ),
            context,
            world_id=None,
            event_model=RuntimeEvent,
            occurred_at=decision.completed_at,
        )
        feedback_event = cast(RuntimeEvent, appended.events[0])
        source = {
            "receipt_id": workflow_step_receipt_id(
                claim.tenant_id, claim.job_id, "TURN_COMPLETED"
            ),
            "source_type": "AGENT_TURN_PRODUCT_PROJECTION",
            "source_revision": 1,
            "actor": cast(dict[str, Any], json_value(context.actor)),
            "content_ref": cast(dict[str, Any], json_value(context.content_ref)),
            "interaction_id": interaction_id,
            "session_id": authority.event.session_id,
            "turn_id": authority.event.turn_id,
            "sequence": interaction_sequence,
            "command_id": command.command_id,
            "feedback_event_id": feedback_event.event_id,
            "feedback_sha256": feedback_sha256,
            "role": "teaching_agent",
            "response_type": "skill_patch",
            "question": None,
            "hint_level": 4,
            "skill_patch_sha256": patch["patch_sha256"],
            "committed_at": _timestamp(now),
        }
        source["source_sha256"] = canonical_json_sha256(source)
        event_wire = cast(
            dict[str, Any], json_value(public_domain_event_data(feedback_event))
        )
        event_wire.pop("payload")
        event_wire["feedback_sha256"] = feedback_sha256
        interaction = {
            "request_context": request_context_data(context),
            "interaction_id": interaction_id,
            "session_id": authority.event.session_id,
            "turn_id": authority.event.turn_id,
            "sequence": interaction_sequence,
            "interaction_revision": 1,
            "projection_source": source,
            "role": "teaching_agent",
            "response_type": "skill_patch",
            "question": None,
            "hint_level": 4,
            "feedback": feedback,
            "feedback_event": event_wire,
            "skill_patch": patch,
            "patch_decision": None,
            "created_at": _timestamp(now),
            "updated_at": _timestamp(now),
            "links": {
                "self": (
                    f"/product-experience/v1/sessions/{base.session_id}/"
                    f"agent-interactions/{interaction_id}"
                ),
                "session_workspace": (
                    f"/product-experience/v1/sessions/{base.session_id}/workspace"
                ),
                "skill_draft": (
                    f"/product-experience/v1/sessions/{base.session_id}/"
                    f"skill-drafts/{base.draft_id}"
                ),
            },
        }
        session.add(
            ProductInteractionRow(
                tenant_id=claim.tenant_id,
                actor_id=context.actor.actor_id,
                session_id=base.session_id,
                interaction_id=interaction_id,
                turn_id=authority.event.turn_id,
                sequence=interaction_sequence,
                interaction_revision=1,
                created_at=now,
                updated_at=now,
                interaction_json=interaction,
            )
        )
        # These append-only authorities intentionally do not expose ORM
        # relationships.  Flush each FK layer explicitly so PostgreSQL, rather
        # than mapper insertion order, remains the authority for the DAG.
        await session.flush()
        session.add(
            ProductSkillPatchProposalRow(
                patch_id=public_patch_id,
                tenant_id=claim.tenant_id,
                actor_id=context.actor.actor_id,
                session_id=base.session_id,
                interaction_id=interaction_id,
                requested_interaction_id=proposal.failed.interaction_id,
                turn_id=authority.event.turn_id,
                request_command_id=command.command_id,
                requested_interaction_revision=proposal.failed.interaction_revision,
                requested_interaction_sequence=proposal.failed.interaction_sequence,
                requested_failure_suffix_end_sequence=(
                    proposal.failed.same_failure_suffix_end_sequence
                ),
                failed_turn_id=proposal.failed.turn_id,
                failed_command_id=proposal.failed.command_id,
                task_id=proposal.failed.task_id,
                world_id=proposal.failed.world_id,
                failure_count=proposal.failed.failure_count,
                failure_key=proposal.failed.failure_key,
                feedback_event_id=proposal.failed.feedback_event_id,
                projection_receipt_id=proposal.failed.projection_receipt_id,
                draft_id=base.draft_id,
                skill_id=base.skill_id,
                base_draft_revision_row_id=base.draft_revision_row_id,
                base_draft_revision=base.revision,
                base_draft_sha256=base.draft_sha256,
                source_bundle_sha256=base.source_bundle_sha256,
                entrypoint=base.entrypoint,
                entrypoint_sha256=proposal.target.entrypoint_sha256,
                previous_content_sha256=proposal.operation.previous_content_sha256,
                content_sha256=proposal.operation.content_sha256,
                result_draft_sha256=result_draft["draft_sha256"],
                patch_sha256=cast(str, patch["patch_sha256"]),
                agent_proposal_id=proposal.proposal_id,
                agent_proposal_sha256=proposal.proposal_sha256,
                failed_build_id=proposal.failed.build_id,
                failed_run_id=proposal.failed.run_id,
                proposal_json=patch,
                agent_proposal_json=agent_wire,
                created_at=now,
            )
        )
        await session.flush()
        for evidence_ref, evidence_wire_item in zip(
            proposal.failed.evidence_refs, evidence_wire, strict=True
        ):
            session.add(
                ProductSkillPatchEvidenceRow(
                    patch_id=public_patch_id,
                    evidence_id=evidence_ref.evidence_id,
                    evidence_type=evidence_ref.evidence_type.value,
                    evidence_sha256=cast(str, evidence_ref.sha256),
                    evidence_created_at=evidence_ref.created_at,
                    evidence_ref_json=evidence_wire_item,
                )
            )
        await jobs.record_step_in_session(
            session,
            claim,
            step_name="PATCH_PROPOSAL_DERIVED",
            input_sha256=proposal.proposal_sha256,
            output=agent_wire,
        )
        await jobs.record_step_in_session(
            session,
            claim,
            step_name="TURN_COMPLETED",
            input_sha256=proposal.proposal_sha256,
            output=source,
        )
        terminal = replace(
            command,
            status=CommandStatus.APPLIED,
            stage="COMPLETE",
            terminal=True,
            result={
                "result_type": "NO_EFFECT",
                "reason_code": "SKILL_PATCH_PROPOSED",
            },
            error=None,
            evidence_refs=proposal.failed.evidence_refs,
            links={"self": command.links["self"]},
            revision=command.revision + 1,
            updated_at=now,
        )
        transitioned = await commands.transition_in_session(
            session,
            CommandTransition(command, terminal),
            context,
        )
        if isinstance(transitioned, Failure):
            raise WorkflowInvariantError("Patch terminal Command CAS was lost")
        reservation.status = "PROPOSED"
        reservation.proposal_id = public_patch_id
        reservation.updated_at = now
        await refresh_workspace_in_session(
            session,
            tenant_id=claim.tenant_id,
            actor_id=context.actor.actor_id,
            session_id=base.session_id,
            updated_at=now,
        )
        await jobs.finish_in_session(session, claim, status="SUCCEEDED")


async def finish_hint_interaction(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    commands: PostgresCommandStore,
    jobs: PostgresWorkflowJobStore,
    authority: _TurnAuthority,
    decision: AgentDecision,
    lease_seconds: int,
) -> None:
    """Atomically publish one no-Run teaching hint as an AgentInteraction.

    A hint explains the current situation to the student.  It never compiles or
    executes the Skill, so it produces no Run, no Evidence and no World event;
    the only durable products are one AgentInteraction, its feedback Event and
    the frozen TeachingDirective receipt that authorized the response.
    """

    directive = decision.teaching_directive
    if (
        directive is None
        or decision.response_type not in {"question", "hint"}
        or decision.draft.skill_patch is not None
        # A hint produces no Evidence of its own, but it may cite the compile
        # rejection its event carries -- that citation is what lets 叮当 talk
        # about the actual error instead of re-asking an opening question. It
        # must cite exactly that Evidence and nothing else.
        or decision.evidence_refs != authority.event.evidence_refs
    ):
        raise WorkflowInvariantError("hint projection requires one no-Evidence teaching response")
    decision_wire = cast(dict[str, Any], json_value(decision))
    request_sha256 = canonical_json_sha256(cast(dict[str, Any], json_value(authority.event)))
    async with session_factory() as session, session.begin():
        owner = await session.scalar(
            select(AgentSessionRow)
            .where(
                AgentSessionRow.tenant_id == authority.claim.tenant_id,
                AgentSessionRow.actor_id == authority.context.actor.actor_id,
                AgentSessionRow.session_id == authority.event.session_id,
                AgentSessionRow.status == "ACTIVE",
            )
            .with_for_update()
        )
        replayed = await session.scalar(
            select(ProductInteractionRow).where(
                ProductInteractionRow.tenant_id == authority.claim.tenant_id,
                ProductInteractionRow.actor_id == authority.context.actor.actor_id,
                ProductInteractionRow.session_id == authority.event.session_id,
                ProductInteractionRow.turn_id == authority.event.turn_id,
            )
        )
        if replayed is not None:
            # The whole projection commits in one transaction, so an existing
            # Interaction for this Turn can only be this hint replayed after an
            # ACK-unknown restart.  Re-close its identity instead of appending a
            # second one.  The full read-side closure cannot be used here: it
            # requires the workflow job to be SUCCEEDED and unleased, which is
            # by construction false while this worker still holds the claim.
            replayed_feedback = replayed.interaction_json.get("feedback")
            replayed_terminal = await session.scalar(
                select(JobStepReceiptRow).where(
                    JobStepReceiptRow.tenant_id == authority.claim.tenant_id,
                    JobStepReceiptRow.job_id == authority.claim.job_id,
                    JobStepReceiptRow.step_name == "TURN_COMPLETED",
                )
            )
            if (
                owner is None
                or replayed_terminal is None
                or not isinstance(replayed_feedback, Mapping)
                or _interaction_projection_kind(replayed.interaction_json) != "HINT_NO_RUN"
                or replayed_feedback.get("command_id") != authority.command.command_id
                or replayed_feedback.get("run_id") is not None
                or replayed.interaction_json.get("projection_source")
                != replayed_terminal.receipt_json
            ):
                raise WorkflowInvariantError("hint ACK recovery authority drifted")
            return
        claim = await jobs.start_step_in_session(
            session,
            authority.claim,
            phase="PRODUCT_PROJECTION",
            lease_seconds=lease_seconds,
        )
        command = await _command(session, claim.command_id, claim.tenant_id)
        context = _operation_context(command)
        request_turn = await session.scalar(
            select(AgentTurnRow).where(
                AgentTurnRow.tenant_id == claim.tenant_id,
                AgentTurnRow.actor_id == context.actor.actor_id,
                AgentTurnRow.session_id == authority.event.session_id,
                AgentTurnRow.turn_id == authority.event.turn_id,
                AgentTurnRow.command_id == command.command_id,
            )
        )
        side_effect_receipts = list(
            (
                await session.scalars(
                    select(JobStepReceiptRow).where(
                        JobStepReceiptRow.tenant_id == claim.tenant_id,
                        JobStepReceiptRow.job_id == claim.job_id,
                        JobStepReceiptRow.step_name.in_(
                            ("SKILL_INVOKED", "SANDBOX_DISPATCHED", "WORLD_COMMITTED")
                        ),
                    )
                )
            ).all()
        )
        request_runs = list(
            (
                await session.scalars(
                    select(RunRow).where(
                        RunRow.tenant_id == claim.tenant_id,
                        RunRow.actor_id == context.actor.actor_id,
                        RunRow.session_id == authority.event.session_id,
                        RunRow.command_id == command.command_id,
                    )
                )
            ).all()
        )
        request_evidence = list(
            (
                await session.scalars(
                    select(EvidenceRow).where(
                        EvidenceRow.tenant_id == claim.tenant_id,
                        EvidenceRow.actor_id == context.actor.actor_id,
                        EvidenceRow.command_id == command.command_id,
                    )
                )
            ).all()
        )
        if (
            owner is None
            or request_turn is None
            or command.status is not CommandStatus.VALIDATING
            or command.stage != "POLICY"
            or command.terminal
            or command.request_context != authority.command.request_context
            or command.versions != authority.command.versions
            or request_turn.turn_sequence != owner.session_json.get("last_turn_sequence")
            or side_effect_receipts
            or request_runs
            or request_evidence
        ):
            raise WorkflowInvariantError("hint Command, Turn or no-Run boundary drifted")
        interaction_sequence = int(
            await session.scalar(
                select(func.max(ProductInteractionRow.sequence)).where(
                    ProductInteractionRow.tenant_id == claim.tenant_id,
                    ProductInteractionRow.actor_id == context.actor.actor_id,
                    ProductInteractionRow.session_id == authority.event.session_id,
                )
            )
            or 0
        ) + 1
        now = max(
            await _database_now(session),
            command.updated_at,
            decision.completed_at,
            request_turn.created_at,
        )
        interaction_id = _identifier("interaction", claim.tenant_id, claim.job_id)
        feedback = {
            "session_id": authority.event.session_id,
            "turn_id": authority.event.turn_id,
            "command_id": command.command_id,
            "run_id": None,
            "message_key": decision.message_key,
            "message": decision.message,
            "source": decision.source,
            "degraded": decision.degraded,
            "fallback_reason": decision.fallback_reason,
            # Whatever the hint cited travels with the feedback, so a teacher
            # reading this later can see which failure the advice was about.
            # The decision owns no Evidence; these are the Build rejections it
            # was allowed to observe.
            "evidence_refs": decision_wire.get("evidence_refs", []),
            "completed_at": _timestamp(decision.completed_at),
        }
        feedback_sha256 = canonical_json_sha256(feedback)
        stream_id = f"agent-session:{authority.event.session_id}"
        appended = await append_events_in_session(
            session,
            stream_id,
            await _stream_sequence(session, claim.tenant_id, stream_id),
            (
                UncommittedEvent(
                    event_type=RuntimeEventType.AGENT_TURN_FEEDBACK_READY.value,
                    event_version=1,
                    producer="walnut_agent_runtime",
                    trace_id=context.trace_id,
                    command_id=command.command_id,
                    correlation_id=context.correlation_id,
                    causation_id=command.command_id,
                    content_ref=context.content_ref,
                    payload=feedback,
                ),
            ),
            context,
            world_id=None,
            event_model=RuntimeEvent,
            occurred_at=decision.completed_at,
        )
        feedback_event = cast(RuntimeEvent, appended.events[0])
        source = {
            "receipt_id": workflow_step_receipt_id(
                claim.tenant_id, claim.job_id, "TURN_COMPLETED"
            ),
            "source_type": "AGENT_TURN_PRODUCT_PROJECTION",
            "source_revision": 1,
            "actor": cast(dict[str, Any], json_value(context.actor)),
            "content_ref": cast(dict[str, Any], json_value(context.content_ref)),
            "interaction_id": interaction_id,
            "session_id": authority.event.session_id,
            "turn_id": authority.event.turn_id,
            "sequence": interaction_sequence,
            "command_id": command.command_id,
            "feedback_event_id": feedback_event.event_id,
            "feedback_sha256": feedback_sha256,
            "role": decision.role,
            "response_type": decision.response_type,
            "question": decision.draft.question,
            "hint_level": decision.draft.hint_level,
            "skill_patch_sha256": None,
            "committed_at": _timestamp(now),
        }
        source["source_sha256"] = canonical_json_sha256(source)
        event_wire = cast(dict[str, Any], json_value(public_domain_event_data(feedback_event)))
        event_wire.pop("payload")
        event_wire["feedback_sha256"] = feedback_sha256
        interaction = {
            "request_context": request_context_data(context),
            "interaction_id": interaction_id,
            "session_id": authority.event.session_id,
            "turn_id": authority.event.turn_id,
            "sequence": interaction_sequence,
            "interaction_revision": 1,
            "projection_source": source,
            "role": decision.role,
            "response_type": decision.response_type,
            "question": decision.draft.question,
            "hint_level": decision.draft.hint_level,
            "feedback": feedback,
            "feedback_event": event_wire,
            "skill_patch": None,
            "patch_decision": None,
            "created_at": _timestamp(now),
            "updated_at": _timestamp(now),
            "links": {
                "self": (
                    f"/product-experience/v1/sessions/{authority.event.session_id}/"
                    f"agent-interactions/{interaction_id}"
                ),
                "session_workspace": (
                    f"/product-experience/v1/sessions/{authority.event.session_id}/workspace"
                ),
                "skill_draft": None,
            },
        }
        session.add(
            ProductInteractionRow(
                tenant_id=claim.tenant_id,
                actor_id=context.actor.actor_id,
                session_id=authority.event.session_id,
                interaction_id=interaction_id,
                turn_id=authority.event.turn_id,
                sequence=interaction_sequence,
                interaction_revision=1,
                created_at=now,
                updated_at=now,
                interaction_json=interaction,
            )
        )
        await session.flush()
        await jobs.record_step_in_session(
            session,
            claim,
            step_name="HINT_DECISION_DERIVED",
            input_sha256=request_sha256,
            output={"decision": decision_wire},
        )
        await jobs.record_step_in_session(
            session,
            claim,
            step_name="TURN_COMPLETED",
            input_sha256=request_sha256,
            output=source,
        )
        terminal = replace(
            command,
            status=CommandStatus.APPLIED,
            stage="COMPLETE",
            terminal=True,
            result={
                "result_type": "NO_EFFECT",
                "reason_code": "HINT_DELIVERED",
            },
            error=None,
            evidence_refs=(),
            links={"self": command.links["self"]},
            revision=command.revision + 1,
            updated_at=now,
        )
        transitioned = await commands.transition_in_session(
            session,
            CommandTransition(command, terminal),
            context,
        )
        if isinstance(transitioned, Failure):
            raise WorkflowInvariantError("hint terminal Command CAS was lost")
        await refresh_workspace_in_session(
            session,
            tenant_id=claim.tenant_id,
            actor_id=context.actor.actor_id,
            session_id=authority.event.session_id,
            updated_at=now,
        )
        await jobs.finish_in_session(session, claim, status="SUCCEEDED")


async def _learner_objective(
    session: AsyncSession,
    *,
    authority: _TurnAuthority,
    claim: ClaimedWorkflowJob,
    command: CommandRecord,
    result: SkillInvocationResult,
    outcome: GameEvent,
    decision: AgentDecision,
    learner: LearnerProfileRow,
    session_row: AgentSessionRow,
    run_row: RunRow,
    feedback: Mapping[str, Any],
    feedback_sha256: str,
    feedback_event: RuntimeEvent,
    recorded_at: datetime,
) -> tuple[dict[str, Any], int, int]:
    profile = dict(learner.profile_json)
    if learner.profile_sha256 != canonical_json_sha256(profile):
        raise WorkflowInvariantError("Learner profile hash is not canonical at hand-off")
    expected_revision = _integer(profile, "revision")
    projected_through = _integer(profile, "projected_through_sequence")
    learner_expected = await _stream_sequence(
        session,
        authority.context.actor.tenant_id,
        f"learner:{authority.learner_id}",
    )
    through_sequence = 1 if learner_expected == "NO_STREAM" else cast(int, learner_expected) + 1
    if projected_through != through_sequence - 1:
        raise WorkflowInvariantError("Learner profile and stream high-watermarks differ")
    knowledge_points = authority.task.get("knowledge_points")
    if (
        isinstance(knowledge_points, str | bytes | bytearray)
        or not isinstance(knowledge_points, Sequence)
        or not knowledge_points
        or not isinstance(knowledge_points[0], str)
    ):
        raise WorkflowInvariantError("Content task has no learner projection concept")
    concept = knowledge_points[0]
    interaction_count = await session.scalar(
        select(func.count()).where(
            ProductInteractionRow.tenant_id == authority.context.actor.tenant_id,
            ProductInteractionRow.actor_id == authority.context.actor.actor_id,
            ProductInteractionRow.session_id == authority.event.session_id,
            ProductInteractionRow.turn_id == authority.event.turn_id,
        )
    )
    if interaction_count:
        raise WorkflowInvariantError("Turn already has a Product Interaction before hand-off")
    interaction_high_watermark = await session.scalar(
        select(func.max(ProductInteractionRow.sequence)).where(
            ProductInteractionRow.tenant_id == authority.context.actor.tenant_id,
            ProductInteractionRow.actor_id == authority.context.actor.actor_id,
            ProductInteractionRow.session_id == authority.event.session_id,
        )
    )
    interaction_sequence = int(interaction_high_watermark or 0) + 1
    outcome_receipt = await _required_step_receipt(session, claim, "OUTCOME_DERIVED")
    final_receipt = await _required_step_receipt(session, claim, "FINAL_DECISION_DERIVED")
    feedback_event_wire = cast(dict[str, Any], json_value(domain_event_data(feedback_event)))
    decision_wire = cast(dict[str, Any], json_value(decision))
    outcome_wire = cast(dict[str, Any], json_value(outcome))
    terminal_result = _terminal_result(result)
    terminal_error = _terminal_error(result)
    assistance = await _closed_learner_assistance(
        session,
        tenant_id=claim.tenant_id,
        actor_id=authority.context.actor.actor_id,
        session_id=authority.event.session_id,
        run_id=result.run.run_id,
    )
    objective = {
        "schema_version": "1.0.0",
        "identity": {
            "tenant_id": claim.tenant_id,
            "job_id": claim.job_id,
            "command_id": command.command_id,
            "session_id": authority.event.session_id,
            "turn_id": authority.event.turn_id,
            "run_id": result.run.run_id,
            "learner_id": authority.learner_id,
            "actor_id": authority.context.actor.actor_id,
            "content_hash": authority.context.content_ref.content_hash,
        },
        "command": cast(dict[str, Any], json_value(command_record_data(command))),
        "run": {
            "run_id": result.run.run_id,
            "task_success": result.run.task_success,
            "failure_key": result.run.failure_key,
            "invocation_request_sha256": result.request_sha256,
            "run_authority_sha256": run_authority_sha256(run_row.run_json),
            "run_feedback_sha256": canonical_json_sha256(run_row.run_json),
        },
        "assistance": assistance,
        "task": {
            "task_id": authority.event.task_id,
            "concept": concept,
            "task_sha256": canonical_json_sha256(dict(authority.task)),
        },
        "source_feedback_event_id": feedback_event.event_id,
        "source_feedback_event_sha256": canonical_json_sha256(feedback_event_wire),
        "source_feedback_event": feedback_event_wire,
        "source_evidence_ids": [item.evidence_id for item in result.run.evidence_refs],
        "feedback": dict(feedback),
        "feedback_sha256": feedback_sha256,
        "outcome": outcome_wire,
        "outcome_receipt": _step_receipt_wire(outcome_receipt),
        "final_decision": decision_wire,
        "final_decision_receipt": _step_receipt_wire(final_receipt),
        "projection": {
            "expected_revision": expected_revision,
            "through_sequence": through_sequence,
            "recorded_at": _timestamp(recorded_at),
            "interaction_id": _identifier(
                "interaction", authority.context.actor.tenant_id, claim.job_id
            ),
            "interaction_sequence": interaction_sequence,
        },
        "terminal_command": {
            "status": (
                CommandStatus.APPLIED.value
                if result.run.task_success
                else CommandStatus.REJECTED.value
            ),
            "result": terminal_result,
            "error": terminal_error,
            "evidence_refs": [_evidence_ref_wire(item) for item in result.run.evidence_refs],
            "links": {
                **dict(command.links),
                "run": f"/v1/runs/{result.run.run_id}",
                "world_snapshot": f"/v1/worlds/{result.run.world_id}/snapshot",
            },
        },
        "session": {
            "session_id": session_row.session_id,
            "world_id": session_row.world_id,
        },
    }
    return objective, expected_revision, through_sequence


async def _closed_learner_assistance(
    session: AsyncSession,
    *,
    tenant_id: str,
    actor_id: str,
    session_id: str,
    run_id: str,
) -> dict[str, Any]:
    """Freeze a validated Run→Build→Draft assistance graph in the hand-off."""

    run = await session.scalar(
        select(SkillRunProvenanceRow).where(
            SkillRunProvenanceRow.run_id == run_id,
            SkillRunProvenanceRow.tenant_id == tenant_id,
            SkillRunProvenanceRow.actor_id == actor_id,
            SkillRunProvenanceRow.session_id == session_id,
        )
    )
    build = (
        await validate_run_provenance(session, run)
        if run is not None
        else None
    )
    if run is None or build is None:
        raise LearnerProjectionInvariantError(
            "Learner Run provenance is missing or corrupt"
        )
    used_skill_patch = run.assistance_authority == "SKILL_PATCH"
    return {
        "authority_version": "1.0.0",
        "provenance_kind": run.provenance_kind,
        "run_id": run.run_id,
        "run_authority_sha256": run.authority_sha256,
        "build_id": build.build_id,
        "build_authority_sha256": build.authority_sha256,
        "activation_id": run.activation_id,
        "activation_sha256": run.activation_sha256,
        "activation_authority_sha256": run.activation_authority_sha256,
        "registry_revision": run.registry_revision,
        "certification_id": run.certification_id,
        "certification_sha256": run.certification_sha256,
        "certification_authority_sha256": run.certification_authority_sha256,
        "artifact_sha256": run.artifact_sha256,
        "artifact_authority_sha256": run.artifact_authority_sha256,
        "draft_revision_row_id": run.draft_revision_row_id,
        "origin_accepted_revision_row_id": build.origin_accepted_revision_row_id,
        "draft_sha256": run.draft_sha256,
        "assistance_authority": run.assistance_authority,
        "patch_id": build.patch_id,
        "patch_decision_id": build.patch_decision_id,
        "used_skill_patch": used_skill_patch,
    }


async def _waiting_parent(
    session: AsyncSession,
    claim: ClaimedLearnerProjectionJob,
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
        raise LearnerProjectionInvariantError("learner worker does not own an exact waiting Turn")
    return row


async def _validate_terminal_objective_core(
    session: AsyncSession,
    *,
    claim: ClaimedLearnerProjectionJob,
    current: Any,
) -> None:
    objective = dict(claim.projection)
    identity = _object(objective.get("identity"), "terminal learner identity")
    run = _object(objective.get("run"), "terminal learner Run")
    assistance = _object(objective.get("assistance"), "terminal learner assistance")
    task = _object(objective.get("task"), "terminal learner task")
    projection = _object(objective.get("projection"), "terminal projection objective")
    command_wire = _object(objective.get("command"), "hand-off Command objective")
    content = await session.scalar(
        select(ProductContentUnitRow).where(
            ProductContentUnitRow.tenant_id == claim.tenant_id,
            ProductContentUnitRow.unit_id == current.context.content_ref.unit_id,
            ProductContentUnitRow.version == current.context.content_ref.version,
            ProductContentUnitRow.content_hash == claim.content_hash,
        )
    )
    durable_task = (
        _object(content.content_json.get("task"), "terminal Content task")
        if content is not None
        else None
    )
    knowledge_points = durable_task.get("knowledge_points") if durable_task is not None else None
    concept = task.get("concept")
    expected_identity = {
        "tenant_id": claim.tenant_id,
        "job_id": claim.job_id,
        "command_id": claim.command_id,
        "session_id": claim.session_id,
        "turn_id": claim.turn_id,
        "run_id": claim.run_id,
        "learner_id": claim.learner_id,
        "actor_id": claim.actor_id,
        "content_hash": claim.content_hash,
    }
    durable_assistance = await _closed_learner_assistance(
        session,
        tenant_id=claim.tenant_id,
        actor_id=claim.actor_id,
        session_id=claim.session_id,
        run_id=claim.run_id,
    )
    if (
        objective.get("schema_version") != "1.0.0"
        or identity != expected_identity
        or run.get("run_id") != current.run.run_id
        or run.get("task_success") != current.run.task_success
        or run.get("failure_key") != current.run.failure_key
        or run.get("invocation_request_sha256") != current.result.request_sha256
        or run.get("run_authority_sha256") != run_authority_sha256(current.run_row.run_json)
        or run.get("run_feedback_sha256") != canonical_json_sha256(current.run_row.run_json)
        or assistance != durable_assistance
        or objective.get("source_evidence_ids")
        != [item.evidence_id for item in current.run.evidence_refs]
        or durable_task is None
        or task.get("task_id") != durable_task.get("task_id")
        or task.get("task_sha256") != canonical_json_sha256(durable_task)
        or not isinstance(concept, str)
        or isinstance(knowledge_points, str | bytes | bytearray)
        or not isinstance(knowledge_points, Sequence)
        or concept not in knowledge_points
        or _integer(projection, "expected_revision") != claim.expected_revision
        or _integer(projection, "through_sequence") != claim.through_sequence
        or _integer(projection, "interaction_sequence") < 1
        or _text(projection, "interaction_id")
        != _identifier("interaction", claim.tenant_id, claim.job_id)
        or command_wire.get("command_id") != current.command.command_id
        or command_wire.get("command_type") != current.command.command_type
        or command_wire.get("request_context")
        != request_context_data(current.command.request_context)
        or command_wire.get("versions")
        != cast(dict[str, Any], json_value(current.command.versions))
    ):
        raise LearnerProjectionInvariantError(
            "terminal learner objective differs from durable authorities"
        )


async def _validate_learner_objective(
    session: AsyncSession,
    *,
    claim: ClaimedLearnerProjectionJob,
    objective: Mapping[str, Any],
    command: CommandRecord,
    current: Any,
    parent: WorkflowJobRow,
    validation_state: TerminalProjectionValidationState | None = None,
) -> RuntimeEvent:
    identity = _object(objective.get("identity"), "learner objective identity")
    expected_identity = {
        "tenant_id": claim.tenant_id,
        "job_id": claim.job_id,
        "command_id": claim.command_id,
        "session_id": claim.session_id,
        "turn_id": claim.turn_id,
        "run_id": claim.run_id,
        "learner_id": claim.learner_id,
        "actor_id": claim.actor_id,
        "content_hash": claim.content_hash,
    }
    run = _object(objective.get("run"), "learner objective Run")
    assistance = _object(objective.get("assistance"), "learner objective assistance")
    task = _object(objective.get("task"), "learner objective task")
    projection = _object(objective.get("projection"), "learner objective projection")
    feedback = _object(objective.get("feedback"), "learner objective feedback")
    session_objective = _object(objective.get("session"), "learner objective Session")
    terminal_objective = _object(
        objective.get("terminal_command"),
        "terminal Command objective",
    )
    evidence_ids = [item.evidence_id for item in current.run.evidence_refs]
    if (
        set(objective)
        != {
            "schema_version",
            "identity",
            "command",
            "run",
            "assistance",
            "task",
            "source_feedback_event_id",
            "source_feedback_event_sha256",
            "source_feedback_event",
            "source_evidence_ids",
            "feedback",
            "feedback_sha256",
            "outcome",
            "outcome_receipt",
            "final_decision",
            "final_decision_receipt",
            "projection",
            "terminal_command",
            "session",
        }
        or objective.get("schema_version") != "1.0.0"
        or identity != expected_identity
        or set(run)
        != {
            "run_id",
            "task_success",
            "failure_key",
            "invocation_request_sha256",
            "run_authority_sha256",
            "run_feedback_sha256",
        }
        or set(assistance)
        != {
            "authority_version",
            "provenance_kind",
            "run_id",
            "run_authority_sha256",
            "build_id",
            "build_authority_sha256",
            "activation_id",
            "activation_sha256",
            "activation_authority_sha256",
            "registry_revision",
            "certification_id",
            "certification_sha256",
            "certification_authority_sha256",
            "artifact_sha256",
            "artifact_authority_sha256",
            "draft_revision_row_id",
            "origin_accepted_revision_row_id",
            "draft_sha256",
            "assistance_authority",
            "patch_id",
            "patch_decision_id",
            "used_skill_patch",
        }
        or set(task) != {"task_id", "concept", "task_sha256"}
        or set(projection)
        != {
            "expected_revision",
            "through_sequence",
            "recorded_at",
            "interaction_id",
            "interaction_sequence",
        }
        or set(terminal_objective) != {"status", "result", "error", "evidence_refs", "links"}
        or session_objective != {"session_id": claim.session_id, "world_id": current.run.world_id}
        or claim.source_event_id != objective.get("source_feedback_event_id")
        or claim.expected_revision != _integer(projection, "expected_revision")
        or claim.through_sequence != _integer(projection, "through_sequence")
        or objective.get("command")
        != cast(dict[str, Any], json_value(command_record_data(command)))
        or run.get("run_id") != current.run.run_id == claim.run_id
        or run.get("task_success") != current.run.task_success
        or run.get("failure_key") != current.run.failure_key
        or run.get("invocation_request_sha256") != current.result.request_sha256
        or run.get("run_authority_sha256") != run_authority_sha256(current.run_row.run_json)
        or run.get("run_feedback_sha256") != canonical_json_sha256(current.run_row.run_json)
        or assistance
        != await _closed_learner_assistance(
            session,
            tenant_id=claim.tenant_id,
            actor_id=claim.actor_id,
            session_id=claim.session_id,
            run_id=claim.run_id,
        )
        or objective.get("source_evidence_ids") != evidence_ids
        or objective.get("feedback_sha256") != canonical_json_sha256(feedback)
        or parent.request_sha256 != current.job.request_sha256
    ):
        raise LearnerProjectionInvariantError("learner objective immutable closure drifted")
    content = await session.scalar(
        select(ProductContentUnitRow).where(
            ProductContentUnitRow.tenant_id == claim.tenant_id,
            ProductContentUnitRow.unit_id == command.request_context.content_ref.unit_id,
            ProductContentUnitRow.version == command.request_context.content_ref.version,
            ProductContentUnitRow.content_hash == claim.content_hash,
        )
    )
    durable_task = (
        _object(content.content_json.get("task"), "durable Content task")
        if content is not None
        else None
    )
    if (
        durable_task is None
        or task.get("task_id") != durable_task.get("task_id")
        or task.get("task_sha256") != canonical_json_sha256(durable_task)
        or not isinstance(task.get("concept"), str)
        or task.get("concept") not in durable_task.get("knowledge_points", ())
    ):
        raise LearnerProjectionInvariantError("learner objective Content task drifted")
    event_row = await session.scalar(
        select(EventRow).where(
            EventRow.tenant_id == claim.tenant_id,
            EventRow.event_id == claim.source_event_id,
        )
    )
    event_wire = _object(
        objective.get("source_feedback_event"),
        "learner objective feedback event",
    )
    if (
        event_row is None
        or event_row.event_json != event_wire
        or objective.get("source_feedback_event_sha256") != canonical_json_sha256(event_wire)
        or event_wire.get("event_type") != RuntimeEventType.AGENT_TURN_FEEDBACK_READY.value
        or event_wire.get("command_id") != claim.command_id
        or event_wire.get("payload") != feedback
    ):
        raise LearnerProjectionInvariantError("learner objective feedback Event drifted")
    feedback_event = _runtime_event(event_wire)
    decision_objective = _object(
        objective.get("final_decision"),
        "learner objective final decision",
    )
    recorded_at = _datetime(_text(projection, "recorded_at"))
    causal_floor = max(
        command.updated_at,
        current.run_row.created_at,
        feedback_event.occurred_at,
        _datetime(_text(decision_objective, "completed_at")),
        *(item.created_at for item in current.run.evidence_refs),
    )
    if recorded_at < causal_floor:
        raise LearnerProjectionInvariantError(
            "learner objective projection timestamp precedes durable authority"
        )
    await _validate_frozen_a8_receipts(
        session,
        claim=claim,
        objective=objective,
        parent=parent,
        current=current,
        validation_state=validation_state,
    )
    terminal = _object(objective.get("terminal_command"), "terminal Command objective")
    expected_links = {
        **dict(command.links),
        "run": f"/v1/runs/{current.run.run_id}",
        "world_snapshot": f"/v1/worlds/{current.run.world_id}/snapshot",
    }
    if (
        terminal.get("status")
        != (
            CommandStatus.APPLIED.value
            if current.run.task_success
            else CommandStatus.REJECTED.value
        )
        or terminal.get("result") != _terminal_result(current.result)
        or terminal.get("error") != _terminal_error(current.result)
        or terminal.get("evidence_refs")
        != [_evidence_ref_wire(item) for item in current.run.evidence_refs]
        or terminal.get("links") != expected_links
    ):
        raise LearnerProjectionInvariantError("terminal Command objective drifted")
    return feedback_event


async def _validate_frozen_a8_receipts(
    session: AsyncSession,
    *,
    claim: ClaimedLearnerProjectionJob,
    objective: Mapping[str, Any],
    parent: WorkflowJobRow,
    current: Any,
    validation_state: TerminalProjectionValidationState | None = None,
) -> None:
    outcome_receipt = await _required_step_receipt(session, claim, "OUTCOME_DERIVED")
    final_receipt = await _required_step_receipt(session, claim, "FINAL_DECISION_DERIVED")
    outcome = _object(objective.get("outcome"), "learner objective outcome")
    decision = _object(objective.get("final_decision"), "learner objective decision")
    final_data = final_receipt.receipt_json
    draft = _object(decision.get("draft"), "final decision draft")
    directive = _object(decision.get("teaching_directive"), "TeachingDirective")
    canonical_outcome = await validate_canonical_outcome_event(
        session,
        authority=current,
        outcome=outcome,
        validation_state=validation_state,
    )
    failure_count = canonical_outcome.failure_count
    expected_feedback = decision_feedback_wire(
        decision,
        current.run,
        expected_completed_at=max(
            current.run_row.created_at,
            *(item.created_at for item in current.run.evidence_refs),
        )
        .astimezone(UTC)
        .isoformat(),
    )
    expected_role = (
        "book_agent"
        if outcome.get("event_type") == "task_completed"
        else (
            "bug_agent"
            if isinstance(failure_count, int)
            and not isinstance(failure_count, bool)
            and failure_count >= 3
            else "teaching_agent"
        )
    )
    content_row = await session.scalar(
        select(ProductContentUnitRow).where(
            ProductContentUnitRow.tenant_id == claim.tenant_id,
            ProductContentUnitRow.unit_id == current.context.content_ref.unit_id,
            ProductContentUnitRow.version == current.context.content_ref.version,
            ProductContentUnitRow.content_hash == claim.content_hash,
        )
    )
    if content_row is None:
        raise LearnerProjectionInvariantError("final decision lost its Content authority")
    durable_task = _object(content_row.content_json.get("task"), "Content task")
    expected_directive = _canonical_teaching_directive(
        outcome=canonical_outcome,
        role=expected_role,
        task=durable_task,
        profile={
            "revision": claim.expected_revision,
            "competencies": {},
            "evidence_refs": [],
        },
        teaching_spec_version=current.command.versions.teaching_spec_version,
    )
    if (
        objective.get("outcome_receipt") != _step_receipt_wire(outcome_receipt)
        or objective.get("final_decision_receipt") != _step_receipt_wire(final_receipt)
        or outcome_receipt.receipt_json.get("event") != outcome
        or outcome_receipt.receipt_json.get("run_sha256")
        != run_authority_sha256(current.run_row.run_json)
        or outcome_receipt.input_sha256 != current.result.request_sha256
        or final_data.get("decision") != decision
        or final_data.get("outcome_event_id") != outcome.get("event_id")
        or final_data.get("outcome_sha256") != canonical_json_sha256(outcome)
        or final_data.get("run_id") != claim.run_id
        or final_data.get("invocation_request_sha256") != current.result.request_sha256
        or final_receipt.input_sha256 != canonical_json_sha256(outcome)
        or decision.get("source") != "provider"
        or decision.get("degraded") is not False
        or decision.get("fallback_reason") is not None
        or decision.get("evidence_refs")
        != [cast(dict[str, Any], json_value(item)) for item in current.run.evidence_refs]
        or objective.get("feedback") != expected_feedback
        or current.run_row.run_json.get("agent_feedback") != expected_feedback
        or objective.get("feedback_sha256") != canonical_json_sha256(expected_feedback)
        or draft.get("role") != expected_role
        or draft.get("skill_patch") is not None
        or directive.get("patch_eligible") is not False
        or directive.get("full_solution_eligible") is not False
        or directive != expected_directive
        or (expected_role == "book_agent" and draft.get("response_type") != "growth_summary")
    ):
        raise LearnerProjectionInvariantError("learner objective A8 authority drifted")
    providers = await load_final_provider_receipts(session, parent)
    provider_refs = [
        {
            "receipt_id": row.receipt_id,
            "step_name": row.step_name,
            "output_sha256": row.output_sha256,
        }
        for row in providers
    ]
    if final_data.get("provider_result_receipts") != provider_refs:
        raise LearnerProjectionInvariantError("final Provider receipt chain drifted")
    await validate_agent_decision_runtime_authority(
        session,
        authority=current,
        receipts=providers,
        decision=decision,
    )
    validate_provider_decision_wire(
        providers,
        decision_draft=draft,
        evidence_refs=current.run.evidence_refs,
        decision=decision,
    )


def _canonical_teaching_directive(
    *,
    outcome: GameEvent,
    role: str,
    task: Mapping[str, Any],
    profile: Mapping[str, Any],
    teaching_spec_version: str,
) -> dict[str, Any]:
    knowledge_points = task.get("knowledge_points")
    if (
        isinstance(knowledge_points, str | bytes | bytearray)
        or not isinstance(knowledge_points, Sequence)
        or any(not isinstance(item, str) for item in knowledge_points)
    ):
        raise LearnerProjectionInvariantError("teaching directive task concepts drifted")
    _validate_learner_profile_evidence_catalog(profile)
    raw_competencies = _object(profile.get("competencies"), "Learner competencies")
    competencies: list[LearnerCompetencySummary] = []
    for concept, raw in raw_competencies.items():
        competency = _competency(raw, concept)
        if competency is None:
            continue
        competencies.append(
            LearnerCompetencySummary(
                concept=competency.concept,
                evidence_stage=competency.evidence_stage,
                assistance_level=competency.assistance_level,
                next_review_at=competency.next_review_at,
                evidence_ids=competency.evidence_ids,
            )
        )
    profile_refs = profile.get("evidence_refs")
    if not isinstance(profile_refs, list) or any(
        not isinstance(item, Mapping) or not isinstance(item.get("evidence_id"), str)
        for item in profile_refs
    ):
        raise LearnerProjectionInvariantError("Learner evidence catalog drifted")
    evidence_outcome = (
        PedagogyEvidenceOutcome.SUCCESS
        if outcome.event_type == "task_completed"
        else PedagogyEvidenceOutcome.FAILED
    )
    concept = outcome.payload.get("concept")
    current_evidence = tuple(
        PedagogyEvidence(
            evidence_id=item.evidence_id,
            outcome=evidence_outcome,
            occurred_at=item.created_at,
            concept=concept if isinstance(concept, str) else None,
        )
        for item in outcome.evidence_refs
    )
    hint_policy = _object(task.get("hint_policy"), "Content hint policy")
    directive = PedagogyPolicy().decide(
        PedagogyInput(
            role=cast(Any, role),
            event_type=outcome.event_type,
            failure_count=outcome.failure_count,
            hint_requested=outcome.event_type == "hint_requested",
            teaching_spec_version=teaching_spec_version,
            task_concepts=tuple(cast(Sequence[str], knowledge_points)),
            max_hint_level=_integer(hint_policy, "max_level"),
            learner_revision=_integer(profile, "revision"),
            learner_competencies=tuple(competencies),
            learner_evidence_ids=tuple(cast(str, item["evidence_id"]) for item in profile_refs),
            current_validated_evidence=current_evidence,
            event_time=outcome.occurred_at,
        )
    )
    if directive is None or directive.pedagogy_policy_version != PEDAGOGY_POLICY_VERSION:
        raise LearnerProjectionInvariantError("final role has no canonical TeachingDirective")
    return cast(dict[str, Any], json_value(directive))


def _runtime_event(value: Mapping[str, Any]) -> RuntimeEvent:
    try:
        event = domain_event_from_data(dict(value))
        return RuntimeEvent(
            event_id=event.event_id,
            event_type=event.event_type,
            event_version=event.event_version,
            stream_id=event.stream_id,
            sequence=event.sequence,
            occurred_at=event.occurred_at,
            producer=event.producer,
            trace_id=event.trace_id,
            command_id=event.command_id,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            content_ref=event.content_ref,
            payload=event.payload,
            schema_version=event.schema_version,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise LearnerProjectionInvariantError(
            "learner objective feedback Event is not canonical"
        ) from error


async def _close_authority(
    session: AsyncSession,
    *,
    authority: _TurnAuthority,
    command: CommandRecord,
    context: OperationContext,
    outcome: GameEvent,
    decision: AgentDecision,
    result: SkillInvocationResult,
) -> tuple[RunRow, LearnerProfileRow, AgentSessionRow]:
    if (
        command.command_id != authority.command.command_id
        or command.command_type != "EXECUTE_AGENT_TURN"
        or command.terminal
        or command.status not in {CommandStatus.RUNNING_SANDBOX, CommandStatus.APPLYING_WORLD}
        or command.request_context != authority.command.request_context
        or command.versions != authority.command.versions
    ):
        raise WorkflowInvariantError("Turn Command authority changed before projection")
    if (
        result.tenant_id != authority.claim.tenant_id
        or result.run.session_id != authority.event.session_id
        or result.run.turn_id != authority.event.turn_id
        or result.run.command_id != authority.event.command_id
        or result.run.skill_ref != authority.event.skill_ref
        or result.run.request_context.actor != context.actor
        or result.run.request_context.content_ref != context.content_ref
        or result.run.world_revision_before != authority.event.expected_world_revision
    ):
        raise WorkflowInvariantError("Skill receipt differs from the accepted Turn authority")
    route = RoleRouter().route(outcome)
    if (
        not route.should_run
        or route.role not in {"teaching_agent", "bug_agent", "book_agent"}
        or decision.role != route.role
        or decision.source != "provider"
        or decision.degraded
        or decision.teaching_directive is None
        or decision.teaching_directive.patch_eligible
        or decision.teaching_directive.full_solution_eligible
        or decision.draft.skill_patch is not None
        or set(decision.evidence_refs) != set(result.run.evidence_refs)
    ):
        raise WorkflowInvariantError("final Agent decision is not closed over its Run outcome")
    if route.role == "book_agent" and decision.response_type != "growth_summary":
        raise WorkflowInvariantError("book_agent must publish one growth_summary")
    if (
        outcome.command_id != authority.event.command_id
        or outcome.session_id != authority.event.session_id
        or outcome.turn_id != authority.event.turn_id
        or outcome.student_id != authority.event.student_id
        or outcome.task_id != authority.event.task_id
        or outcome.run_id != result.run.run_id
        or outcome.skill_ref != result.run.skill_ref
        or outcome.evidence_refs != result.run.evidence_refs
        or (outcome.event_type == "task_completed") != result.run.task_success
    ):
        raise WorkflowInvariantError("final role event differs from the canonical Run")

    outcome_receipt = await session.scalar(
        select(JobStepReceiptRow).where(
            JobStepReceiptRow.tenant_id == authority.claim.tenant_id,
            JobStepReceiptRow.job_id == authority.claim.job_id,
            JobStepReceiptRow.step_name == "OUTCOME_DERIVED",
        )
    )
    expected_outcome_receipt = {
        "schema_version": "1.0.0",
        "event": cast(dict[str, Any], json_value(outcome)),
        "run_sha256": run_authority_sha256(
            await _run_json(session, result.run.run_id, authority.claim.tenant_id)
        ),
        "invocation_request_sha256": result.request_sha256,
    }
    if (
        outcome_receipt is None
        or outcome_receipt.input_sha256 != result.request_sha256
        or outcome_receipt.output_sha256 != workflow_receipt_sha256(expected_outcome_receipt)
        or outcome_receipt.receipt_json != expected_outcome_receipt
    ):
        raise WorkflowInvariantError("Run outcome receipt is missing or corrupt")
    workflow_job = await session.scalar(
        select(WorkflowJobRow).where(
            WorkflowJobRow.tenant_id == authority.claim.tenant_id,
            WorkflowJobRow.job_id == authority.claim.job_id,
        )
    )
    if workflow_job is None:
        raise WorkflowInvariantError("Turn workflow Job disappeared before projection")
    await validate_final_decision_receipt(
        session,
        job=workflow_job,
        outcome=outcome,
        decision=decision,
        result=result,
    )

    turn = await session.scalar(
        select(AgentTurnRow)
        .where(
            AgentTurnRow.tenant_id == authority.claim.tenant_id,
            AgentTurnRow.actor_id == context.actor.actor_id,
            AgentTurnRow.turn_id == authority.event.turn_id,
            AgentTurnRow.command_id == command.command_id,
        )
        .with_for_update()
    )
    session_row = await session.scalar(
        select(AgentSessionRow)
        .where(
            AgentSessionRow.tenant_id == authority.claim.tenant_id,
            AgentSessionRow.actor_id == context.actor.actor_id,
            AgentSessionRow.session_id == authority.event.session_id,
            AgentSessionRow.status == "ACTIVE",
        )
        .with_for_update()
    )
    binding = await session.scalar(
        select(CurrentSessionBindingRow).where(
            CurrentSessionBindingRow.tenant_id == authority.claim.tenant_id,
            CurrentSessionBindingRow.actor_id == context.actor.actor_id,
            CurrentSessionBindingRow.content_hash == context.content_ref.content_hash,
            CurrentSessionBindingRow.session_id == authority.event.session_id,
        )
    )
    if turn is None or session_row is None or binding is None:
        raise WorkflowInvariantError("Turn Session binding disappeared before projection")
    if result.run.world_id != binding.world_id or session_row.world_id != binding.world_id:
        raise WorkflowInvariantError("Turn Run World differs from the current Session binding")
    launch = await session.scalar(
        select(LaunchAuthorityRow).where(
            LaunchAuthorityRow.tenant_id == binding.tenant_id,
            LaunchAuthorityRow.authority_id == binding.authority_id,
            LaunchAuthorityRow.actor_id == binding.actor_id,
            LaunchAuthorityRow.content_hash == binding.content_hash,
            LaunchAuthorityRow.world_id == binding.world_id,
            LaunchAuthorityRow.learner_id == authority.learner_id,
            LaunchAuthorityRow.active.is_(True),
        )
    )
    profile = None
    if launch is not None:
        profile = await session.scalar(
            select(AgentProfileRow).where(
                AgentProfileRow.tenant_id == launch.tenant_id,
                AgentProfileRow.agent_profile_id == launch.agent_profile_id,
                AgentProfileRow.actor_id == launch.actor_id,
                AgentProfileRow.content_hash == launch.content_hash,
            )
        )
    head = await session.scalar(
        select(RegistryHeadRow).where(
            RegistryHeadRow.tenant_id == binding.tenant_id,
            RegistryHeadRow.actor_id == binding.actor_id,
            RegistryHeadRow.content_hash == binding.content_hash,
            RegistryHeadRow.world_id == binding.world_id,
            RegistryHeadRow.agent_profile_id == binding.agent_profile_id,
            RegistryHeadRow.authority_id == binding.authority_id,
        )
    )
    activation = None
    if head is not None:
        activation = await session.scalar(
            select(SkillActivationRow).where(
                SkillActivationRow.tenant_id == binding.tenant_id,
                SkillActivationRow.actor_id == binding.actor_id,
                SkillActivationRow.content_hash == binding.content_hash,
                SkillActivationRow.world_id == binding.world_id,
                SkillActivationRow.agent_profile_id == binding.agent_profile_id,
                SkillActivationRow.registry_revision == head.revision,
                SkillActivationRow.skill_id == result.run.skill_ref.skill_id,
                SkillActivationRow.skill_version_id == result.run.skill_ref.skill_version_id,
                SkillActivationRow.artifact_sha256 == result.run.skill_ref.artifact_sha256,
                SkillActivationRow.certification_id == result.run.skill_ref.certification_id,
            )
        )
    certification = await session.scalar(
        select(SkillCertificationRow).where(
            SkillCertificationRow.tenant_id == binding.tenant_id,
            SkillCertificationRow.actor_id == binding.actor_id,
            SkillCertificationRow.content_hash == binding.content_hash,
            SkillCertificationRow.skill_id == result.run.skill_ref.skill_id,
            SkillCertificationRow.skill_version_id == result.run.skill_ref.skill_version_id,
            SkillCertificationRow.artifact_sha256 == result.run.skill_ref.artifact_sha256,
            SkillCertificationRow.certification_id == result.run.skill_ref.certification_id,
        )
    )
    revoked = await session.scalar(
        select(SkillCertificationRevocationRow).where(
            SkillCertificationRevocationRow.tenant_id == binding.tenant_id,
            SkillCertificationRevocationRow.certification_id
            == result.run.skill_ref.certification_id,
        )
    )
    if (
        launch is None
        or profile is None
        or profile.profile_sha256 != canonical_json_sha256(profile.profile_json)
        or profile.profile_json.get("model_version") != command.versions.model_version
        or profile.profile_json.get("prompt_version") != command.versions.prompt_version
        or head is None
        or activation is None
        or certification is None
        or revoked is not None
    ):
        raise WorkflowInvariantError("Turn Activation closure changed before projection")

    content = await session.scalar(
        select(ProductContentUnitRow).where(
            ProductContentUnitRow.tenant_id == binding.tenant_id,
            ProductContentUnitRow.unit_id == context.content_ref.unit_id,
            ProductContentUnitRow.version == context.content_ref.version,
            ProductContentUnitRow.content_hash == context.content_ref.content_hash,
        )
    )
    if content is None or _object(content.content_json.get("task"), "Content task") != dict(
        authority.task
    ):
        raise WorkflowInvariantError("Turn Content task changed before projection")

    run_row = await session.scalar(
        select(RunRow)
        .where(
            RunRow.tenant_id == binding.tenant_id,
            RunRow.actor_id == binding.actor_id,
            RunRow.content_hash == binding.content_hash,
            RunRow.run_id == result.run.run_id,
            RunRow.session_id == authority.event.session_id,
            RunRow.turn_id == authority.event.turn_id,
            RunRow.command_id == command.command_id,
        )
        .with_for_update()
    )
    learner = await session.scalar(
        select(LearnerProfileRow)
        .where(
            LearnerProfileRow.tenant_id == binding.tenant_id,
            LearnerProfileRow.learner_id == authority.learner_id,
            LearnerProfileRow.actor_id == binding.actor_id,
            LearnerProfileRow.content_hash == binding.content_hash,
        )
        .with_for_update()
    )
    if run_row is None or learner is None:
        raise WorkflowInvariantError("Run or Learner authority disappeared before projection")
    if (
        run_row.run_json.get("agent_feedback") is not None
        or run_row.run_json.get("run_id") != result.run.run_id
        or run_row.run_json.get("evidence_refs")
        != [_evidence_ref_wire(item) for item in result.run.evidence_refs]
    ):
        raise WorkflowInvariantError("Run projection differs from the typed Skill receipt")
    return run_row, learner, session_row


def _feedback(
    authority: _TurnAuthority,
    decision: AgentDecision,
    result: SkillInvocationResult,
) -> AgentTurnFeedback:
    return AgentTurnFeedback(
        session_id=authority.event.session_id,
        turn_id=authority.event.turn_id,
        command_id=authority.event.command_id,
        run_id=result.run.run_id,
        message_key=decision.message_key,
        message=decision.message,
        source=decision.source,
        degraded=decision.degraded,
        fallback_reason=decision.fallback_reason,
        evidence_refs=result.run.evidence_refs,
        completed_at=decision.completed_at,
    )


def _feedback_wire(feedback: AgentTurnFeedback) -> dict[str, Any]:
    return {
        "session_id": feedback.session_id,
        "turn_id": feedback.turn_id,
        "command_id": feedback.command_id,
        "run_id": feedback.run_id,
        "message_key": feedback.message_key,
        "message": feedback.message,
        "source": feedback.source,
        "degraded": feedback.degraded,
        "fallback_reason": feedback.fallback_reason,
        "evidence_refs": [_evidence_ref_wire(item) for item in feedback.evidence_refs],
        "completed_at": _timestamp(feedback.completed_at),
    }


async def _append_feedback_event(
    session: AsyncSession,
    *,
    authority: _TurnAuthority,
    context: OperationContext,
    result: SkillInvocationResult,
    feedback: Mapping[str, Any],
    occurred_at: datetime,
) -> RuntimeEvent:
    causation_id = context.command_id
    receipt = result.run.world_commit
    if receipt is not None:
        world_event = await session.scalar(
            select(EventRow).where(
                EventRow.tenant_id == context.actor.tenant_id,
                EventRow.stream_id == f"world:{receipt.world_id}",
                EventRow.sequence == receipt.last_event_sequence,
            )
        )
        if world_event is None or world_event.event_json.get("command_id") != context.command_id:
            raise WorkflowInvariantError("feedback has no exact World event causation")
        causation_id = world_event.event_id
    stream_id = f"agent-session:{authority.event.session_id}"
    expected = await _stream_sequence(session, context.actor.tenant_id, stream_id)
    appended = await append_events_in_session(
        session,
        stream_id,
        expected,
        (
            UncommittedEvent(
                event_type=RuntimeEventType.AGENT_TURN_FEEDBACK_READY.value,
                event_version=1,
                producer="walnut_agent_runtime",
                trace_id=context.trace_id,
                command_id=context.command_id,
                correlation_id=context.correlation_id,
                causation_id=causation_id,
                content_ref=context.content_ref,
                payload=feedback,
            ),
        ),
        context,
        world_id=None,
        event_model=RuntimeEvent,
        occurred_at=occurred_at,
    )
    event = appended.events[0]
    if not isinstance(event, RuntimeEvent):
        raise WorkflowInvariantError("feedback append did not produce a RuntimeEvent")
    return event


async def _project_learner(
    session: AsyncSession,
    *,
    claim: ClaimedLearnerProjectionJob,
    command: CommandRecord,
    context: OperationContext,
    result: SkillInvocationResult,
    learner: LearnerProfileRow,
    task_id: str,
    concept: str,
    feedback_event: RuntimeEvent,
    recorded_at: datetime,
) -> dict[str, Any]:
    profile = dict(learner.profile_json)
    if learner.profile_sha256 != canonical_json_sha256(profile):
        raise WorkflowInvariantError("Learner profile hash is not canonical")
    previous_revision = _integer(profile, "revision")
    projected_through = _integer(profile, "projected_through_sequence")
    if previous_revision != claim.expected_revision:
        raise LearnerProjectionInvariantError("Learner revision differs from hand-off")
    learner_stream = f"learner:{claim.learner_id}"
    learner_expected = await _stream_sequence(
        session,
        context.actor.tenant_id,
        learner_stream,
    )
    learner_sequence = 1 if learner_expected == "NO_STREAM" else cast(int, learner_expected) + 1
    if learner_sequence != claim.through_sequence:
        raise LearnerProjectionInvariantError("Learner stream sequence differs from hand-off")
    if projected_through >= learner_sequence:
        raise WorkflowInvariantError("Learner projection high-watermark is not monotonic")
    model_version = _text(profile, "model_version")
    if model_version != LEARNER_PROJECTION_POLICY_VERSION:
        raise WorkflowInvariantError("Learner projection policy version is not activated")
    _validate_learner_profile_evidence_catalog(profile)
    assistance = await _closed_learner_assistance(
        session,
        tenant_id=context.actor.tenant_id,
        actor_id=context.actor.actor_id,
        session_id=result.run.session_id,
        run_id=result.run.run_id,
    )
    frozen_assistance = _object(
        claim.projection.get("assistance"), "Learner frozen assistance"
    )
    if assistance != frozen_assistance:
        raise LearnerProjectionInvariantError("Learner assistance hand-off drifted")
    used_skill_patch = assistance.get("used_skill_patch") is True
    assistance_level = 4 if used_skill_patch else 0
    competencies = _object(profile.get("competencies"), "Learner competencies")
    current = _competency(competencies.get(concept), concept)
    outcome = (
        ProjectionOutcome.SUCCESS
        if result.run.task_success
        else (
            ProjectionOutcome.FAILED
            if result.run.failure_key == "sandbox_execution_failed"
            else ProjectionOutcome.PARTIAL
        )
    )
    policy_result = LearnerProjectionPolicy().project(
        ProjectionInput(
            learner_revision=previous_revision,
            learner_projection_policy_version=LEARNER_PROJECTION_POLICY_VERSION,
            review_policy_version=REVIEW_POLICY_VERSION,
            evidence=ProjectionEvidence(
                evidence_ids=tuple(item.evidence_id for item in result.run.evidence_refs),
                concept=concept,
                outcome=outcome,
                task_relation=TaskRelation.STANDARD,
                assistance_level=assistance_level,
                used_skill_patch=used_skill_patch,
                occurred_at=feedback_event.occurred_at,
                source_sequence=learner_sequence,
            ),
            current=current,
        )
    )
    if not policy_result.applied:
        raise WorkflowInvariantError("fresh Turn Evidence was already projected")

    learner_payload: dict[str, Any] = {
        "evidence_kind": "LEARNER_OBSERVATION",
        "observation_type": ("TASK_COMPLETION" if result.run.task_success else "CODE_ATTEMPT"),
        "task_id": task_id,
        "outcome": outcome.value,
        "assistance_level": assistance_level,
    }
    payload_sha256 = canonical_json_sha256(learner_payload)
    evidence_id = _identifier(
        "evidence_learner",
        context.actor.tenant_id,
        claim.job_id,
        "LEARNER_UPDATE",
    )
    learner_reference = EvidenceRef(
        evidence_id=evidence_id,
        evidence_type=EvidenceType.LEARNER_UPDATE,
        created_at=recorded_at,
        sha256=payload_sha256,
        uri=f"/v1/evidence/{evidence_id}",
    )
    related = [_evidence_ref_wire(item) for item in result.run.evidence_refs]
    evidence_document = {
        "request_context": request_context_data(context),
        "evidence_ref": _evidence_ref_wire(learner_reference),
        "subject": {"learner_id": claim.learner_id},
        "source": {
            "source_type": "LEARNER_PROJECTOR",
            "source_id": claim.learner_id,
            "command_id": context.command_id,
            "world_id": result.run.world_id,
        },
        "occurred_at": _timestamp(feedback_event.occurred_at),
        "recorded_at": _timestamp(recorded_at),
        "integrity": {
            "payload_sha256": payload_sha256,
            "previous_evidence_sha256": None,
        },
        "payload": learner_payload,
        "related_evidence": related,
        "versions": _versions_wire(command),
    }
    session.add(
        EvidenceRow(
            evidence_id=evidence_id,
            tenant_id=context.actor.tenant_id,
            actor_id=context.actor.actor_id,
            content_hash=context.content_ref.content_hash,
            command_id=context.command_id,
            recorded_at=recorded_at,
            evidence_json=evidence_document,
        )
    )

    competency = policy_result.competency
    competencies[concept] = {
        "concept": competency.concept,
        "evidence_stage": competency.evidence_stage.value,
        "assistance_level": competency.assistance_level,
        "last_observed_at": _timestamp(competency.last_observed_at),
        "next_review_at": _timestamp(competency.next_review_at),
        "evidence_ids": list(competency.evidence_ids),
    }
    prior_refs = profile.get("evidence_refs")
    if not isinstance(prior_refs, list):
        raise WorkflowInvariantError("Learner profile evidence_refs is not an array")
    evidence_catalog = _merge_learner_evidence_catalog(prior_refs, related)
    competencies, catalog_changed = _trim_learner_competencies_to_catalog(
        competencies,
        evidence_catalog,
    )
    changed_competency_ids = sorted({concept, *catalog_changed})
    profile.update(
        {
            "revision": previous_revision + 1,
            "model_version": LEARNER_PROJECTION_POLICY_VERSION,
            "review_policy_version": REVIEW_POLICY_VERSION,
            "projected_through_sequence": learner_sequence,
            "competencies": competencies,
            "evidence_refs": evidence_catalog,
            "updated_at": _timestamp(recorded_at),
        }
    )
    learner.profile_json = profile
    learner.profile_sha256 = canonical_json_sha256(profile)
    learner.updated_at = recorded_at

    learner_update = {
        "learner_id": claim.learner_id,
        "previous_revision": previous_revision,
        "learner_revision": previous_revision + 1,
        "projected_through_sequence": learner_sequence,
        "changed_competency_ids": changed_competency_ids,
        "updated_at": _timestamp(recorded_at),
        "evidence_refs": related,
    }
    appended = await append_events_in_session(
        session,
        learner_stream,
        learner_expected,
        (
            UncommittedEvent(
                event_type=RuntimeEventType.LEARNER_MODEL_UPDATED.value,
                event_version=1,
                producer="walnut_learner",
                trace_id=context.trace_id,
                command_id=context.command_id,
                correlation_id=context.correlation_id,
                causation_id=feedback_event.event_id,
                content_ref=context.content_ref,
                payload=learner_update,
            ),
        ),
        context,
        world_id=None,
        event_model=RuntimeEvent,
        occurred_at=recorded_at,
    )
    learner_event = appended.events[0]
    if not isinstance(learner_event, RuntimeEvent):
        raise LearnerProjectionInvariantError(
            "learner projection append did not produce a RuntimeEvent"
        )
    learner_event_wire = cast(
        dict[str, Any],
        json_value(domain_event_data(learner_event)),
    )
    projection_json = {
        "learner_projection_policy_version": LEARNER_PROJECTION_POLICY_VERSION,
        "review_policy_version": REVIEW_POLICY_VERSION,
        "source_feedback_event_id": feedback_event.event_id,
        "source_evidence_ids": [item.evidence_id for item in result.run.evidence_refs],
        "learner_update": learner_update,
        "reason_codes": list(policy_result.reason_codes),
        "profile_sha256": learner.profile_sha256,
        "evidence_id": evidence_id,
        "evidence_sha256": canonical_json_sha256(evidence_document),
        "learner_event_id": learner_event.event_id,
        "learner_event_sha256": canonical_json_sha256(learner_event_wire),
    }
    return projection_json


async def _project_interaction(
    session: AsyncSession,
    *,
    claim: ClaimedLearnerProjectionJob,
    decision: Mapping[str, Any],
    context: OperationContext,
    session_id: str,
    turn_id: str,
    feedback: Mapping[str, Any],
    feedback_sha256: str,
    feedback_event: RuntimeEvent,
    committed_at: datetime,
    session_row: AgentSessionRow,
) -> dict[str, Any]:
    if session_row.session_id != session_id:
        raise WorkflowInvariantError("Product interaction Session lock drifted")
    draft = _object(decision.get("draft"), "final decision draft")
    role = _text(draft, "role")
    response_type = _text(draft, "response_type")
    projected = _object(claim.projection.get("projection"), "projection objective")
    sequence = _integer(projected, "interaction_sequence")
    if sequence < 1:
        raise LearnerProjectionInvariantError("Interaction sequence is not positive")
    interaction_high_watermark = await session.scalar(
        select(func.max(ProductInteractionRow.sequence)).where(
            ProductInteractionRow.tenant_id == context.actor.tenant_id,
            ProductInteractionRow.actor_id == context.actor.actor_id,
            ProductInteractionRow.session_id == session_id,
        )
    )
    if sequence != int(interaction_high_watermark or 0) + 1:
        raise LearnerProjectionInvariantError(
            "Interaction sequence differs from the frozen gap-free hand-off"
        )
    interaction_id = _identifier(
        "interaction",
        context.actor.tenant_id,
        claim.job_id,
    )
    if interaction_id != _text(projected, "interaction_id"):
        raise LearnerProjectionInvariantError("Interaction identity differs from hand-off")
    receipt_id = workflow_step_receipt_id(
        context.actor.tenant_id,
        claim.job_id,
        "TURN_COMPLETED",
    )
    source: dict[str, Any] = {
        "receipt_id": receipt_id,
        "source_type": "AGENT_TURN_PRODUCT_PROJECTION",
        "source_revision": 1,
        "actor": cast(dict[str, Any], json_value(context.actor)),
        "content_ref": cast(dict[str, Any], json_value(context.content_ref)),
        "interaction_id": interaction_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "sequence": sequence,
        "command_id": context.command_id,
        "feedback_event_id": feedback_event.event_id,
        "feedback_sha256": feedback_sha256,
        "role": role,
        "response_type": response_type,
        "question": draft.get("question"),
        "hint_level": draft.get("hint_level"),
        "skill_patch_sha256": None,
        "committed_at": _timestamp(committed_at),
    }
    source["source_sha256"] = canonical_json_sha256(source)
    event_wire = cast(
        dict[str, Any],
        json_value(public_domain_event_data(feedback_event)),
    )
    event_wire.pop("payload")
    event_wire["feedback_sha256"] = feedback_sha256
    interaction = {
        "request_context": request_context_data(context),
        "interaction_id": interaction_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "sequence": sequence,
        "interaction_revision": 1,
        "projection_source": source,
        "role": role,
        "response_type": response_type,
        "question": draft.get("question"),
        "hint_level": draft.get("hint_level"),
        "feedback": dict(feedback),
        "feedback_event": event_wire,
        "skill_patch": None,
        "patch_decision": None,
        "created_at": _timestamp(committed_at),
        "updated_at": _timestamp(committed_at),
        "links": {
            "self": (
                f"/product-experience/v1/sessions/{session_id}/agent-interactions/{interaction_id}"
            ),
            "session_workspace": (f"/product-experience/v1/sessions/{session_id}/workspace"),
            "skill_draft": None,
        },
    }
    existing = await session.scalar(
        select(ProductInteractionRow).where(
            ProductInteractionRow.tenant_id == context.actor.tenant_id,
            ProductInteractionRow.session_id == session_id,
            ProductInteractionRow.interaction_id == interaction_id,
        )
    )
    if existing is not None:
        raise WorkflowInvariantError("Product interaction already exists before terminal receipt")
    session.add(
        ProductInteractionRow(
            tenant_id=context.actor.tenant_id,
            actor_id=context.actor.actor_id,
            session_id=session_id,
            interaction_id=interaction_id,
            turn_id=turn_id,
            sequence=sequence,
            interaction_revision=1,
            created_at=committed_at,
            updated_at=committed_at,
            interaction_json=interaction,
        )
    )
    return interaction


def _terminal_command(
    command: CommandRecord,
    result: SkillInvocationResult,
    *,
    objective: Mapping[str, Any],
    updated_at: datetime,
) -> CommandRecord:
    terminal = _object(objective.get("terminal_command"), "terminal Command objective")
    raw_error = terminal.get("error")
    if raw_error is not None and not isinstance(raw_error, Mapping):
        raise LearnerProjectionInvariantError("terminal Command error is not an object")
    error = error_from_data(dict(raw_error) if isinstance(raw_error, Mapping) else None)
    status = CommandStatus(_text(terminal, "status"))
    stage = "COMPLETE" if status is CommandStatus.APPLIED else cast(ContractError, error).stage
    return replace(
        command,
        status=status,
        stage=stage,
        terminal=True,
        result=cast(dict[str, Any] | None, terminal.get("result")),
        error=error,
        evidence_refs=result.run.evidence_refs,
        links=_object(terminal.get("links"), "terminal Command links"),
        revision=command.revision + 1,
        updated_at=updated_at,
    )


def _validate_terminal_command_from_run(
    command: CommandRecord,
    result: SkillInvocationResult,
) -> None:
    expected_status = CommandStatus.APPLIED if result.run.task_success else CommandStatus.REJECTED
    expected_stage = (
        "COMPLETE"
        if result.run.task_success
        else (
            "SANDBOX" if result.run.failure_key == "sandbox_execution_failed" else "WORLD_VALIDATE"
        )
    )
    expected_links = {
        **dict(command.links),
        "run": f"/v1/runs/{result.run.run_id}",
        "world_snapshot": f"/v1/worlds/{result.run.world_id}/snapshot",
    }
    if (
        not command.terminal
        or command.status is not expected_status
        or command.stage != expected_stage
        or command.result != _terminal_result(result)
        or error_data(command.error) != _terminal_error(result)
        or command.evidence_refs != result.run.evidence_refs
        or dict(command.links) != expected_links
    ):
        raise LearnerProjectionInvariantError(
            "terminal Command does not derive exactly from its Run"
        )


def _projection_commit_authority(
    *,
    learner: LearnerProfileRow,
    learner_result: Mapping[str, Any],
    interaction: Mapping[str, Any],
    workspace: ProductWorkspaceRow,
    command: CommandRecord,
) -> dict[str, Any]:
    profile = dict(learner.profile_json)
    workspace_wire = dict(workspace.workspace_json)
    command_wire = cast(dict[str, Any], json_value(command_record_data(command)))
    return {
        "schema_version": "1.0.0",
        "learner": {
            "profile_sha256": canonical_json_sha256(profile),
            "profile": profile,
            "projection_sha256": canonical_json_sha256(dict(learner_result)),
            "projection": dict(learner_result),
        },
        "interaction": {
            "interaction_sha256": canonical_json_sha256(dict(interaction)),
            "interaction": dict(interaction),
        },
        "workspace": {
            "workspace_revision": workspace.workspace_revision,
            "workspace_sha256": canonical_json_sha256(workspace_wire),
            "workspace": workspace_wire,
        },
        "command": {
            "record_sha256": canonical_json_sha256(command_wire),
            "record": command_wire,
        },
    }


def _terminal_closure(
    *,
    learner: LearnerProfileRow,
    learner_result: Mapping[str, Any],
    interaction: Mapping[str, Any],
    workspace: ProductWorkspaceRow,
    receipt: JobStepReceiptRow,
    projection_receipt: JobStepReceiptRow,
    command: CommandRecord,
) -> dict[str, Any]:
    profile = dict(learner.profile_json)
    source = _object(interaction.get("projection_source"), "Interaction source")
    learner_update = _object(learner_result.get("learner_update"), "learner update")
    return {
        "schema_version": "1.0.0",
        "learner": {
            "learner_id": learner.learner_id,
            "revision": _integer(profile, "revision"),
            "projected_through_sequence": _integer(profile, "projected_through_sequence"),
            "profile_sha256": learner.profile_sha256,
            "evidence_id": _text(learner_result, "evidence_id"),
            "evidence_sha256": _text(learner_result, "evidence_sha256"),
            "event_id": _text(learner_result, "learner_event_id"),
            "event_sha256": _text(learner_result, "learner_event_sha256"),
            "event_payload_sha256": canonical_json_sha256(learner_update),
        },
        "interaction": {
            "interaction_id": _text(interaction, "interaction_id"),
            "sequence": _integer(interaction, "sequence"),
            "interaction_sha256": canonical_json_sha256(interaction),
            "source_sha256": _text(source, "source_sha256"),
        },
        "workspace": {
            "workspace_id": workspace.workspace_id,
            "workspace_revision": workspace.workspace_revision,
            "workspace_sha256": canonical_json_sha256(workspace.workspace_json),
            "last_interaction_sequence": workspace.workspace_json.get("last_interaction_sequence"),
        },
        "terminal_receipt": _step_receipt_wire(receipt),
        "projection_receipt": _step_receipt_wire(projection_receipt),
        "command": {
            "command_id": command.command_id,
            "status": command.status.value,
            "revision": command.revision,
            "record_sha256": canonical_json_sha256(command_record_data(command)),
        },
        "parent_workflow": {"status": "SUCCEEDED", "phase": "COMPLETE"},
    }


def _validate_terminal_result_closure(
    *,
    stored: Mapping[str, Any],
    claim: ClaimedLearnerProjectionJob,
    learner: LearnerProfileRow,
    learner_result: Mapping[str, Any],
    interaction: Mapping[str, Any],
    workspace: ProductWorkspaceRow,
    receipt: JobStepReceiptRow,
    projection_receipt: JobStepReceiptRow,
    command: CommandRecord,
) -> None:
    learner_wire = _object(stored.get("learner"), "terminal Learner closure")
    interaction_wire = _object(
        stored.get("interaction"),
        "terminal Interaction closure",
    )
    workspace_wire = _object(stored.get("workspace"), "terminal Workspace closure")
    command_wire = _object(stored.get("command"), "terminal Command closure")
    parent_wire = _object(stored.get("parent_workflow"), "terminal parent closure")
    commit = projection_receipt.receipt_json
    commit_learner = _object(commit.get("learner"), "committed Learner authority")
    commit_interaction = _object(commit.get("interaction"), "committed Interaction authority")
    commit_workspace = _object(commit.get("workspace"), "committed Workspace authority")
    commit_command = _object(commit.get("command"), "committed Command authority")
    committed_profile = _object(commit_learner.get("profile"), "committed Learner profile")
    committed_projection = _object(commit_learner.get("projection"), "committed Learner projection")
    committed_interaction = _object(commit_interaction.get("interaction"), "committed Interaction")
    committed_workspace = _object(commit_workspace.get("workspace"), "committed Workspace")
    committed_command = _object(commit_command.get("record"), "committed Command")
    profile = dict(learner.profile_json)
    current_revision = _integer(profile, "revision")
    current_through = _integer(profile, "projected_through_sequence")
    _validate_learner_profile_evidence_catalog(profile)
    _validate_learner_profile_evidence_catalog(committed_profile)
    learner_update = _object(learner_result.get("learner_update"), "Learner update")
    terminal_objective = _object(
        claim.projection.get("terminal_command"),
        "terminal Command objective",
    )
    source_refs = terminal_objective.get("evidence_refs")
    if not isinstance(source_refs, list):
        raise LearnerProjectionInvariantError("terminal source Evidence catalog is not an array")
    canonical_source_refs = _merge_learner_evidence_catalog([], source_refs)
    if canonical_source_refs != source_refs:
        raise LearnerProjectionInvariantError("terminal source Evidence catalog is not canonical")
    source_ids = [cast(str, item["evidence_id"]) for item in canonical_source_refs]
    committed_catalog = {
        cast(str, item["evidence_id"]): item
        for item in cast(list[dict[str, Any]], committed_profile["evidence_refs"])
    }
    stored_workspace_revision = _integer(workspace_wire, "workspace_revision")
    stored_workspace_sequence = _integer(workspace_wire, "last_interaction_sequence")
    current_workspace_sequence = _integer(
        workspace.workspace_json,
        "last_interaction_sequence",
    )
    source = _object(interaction.get("projection_source"), "Interaction source")
    projected = _object(claim.projection.get("projection"), "projection objective")
    expected_learner = {
        "learner_id": claim.learner_id,
        "revision": claim.expected_revision + 1,
        "projected_through_sequence": claim.through_sequence,
        "profile_sha256": learner_wire.get("profile_sha256"),
        "evidence_id": learner_result.get("evidence_id"),
        "evidence_sha256": learner_result.get("evidence_sha256"),
        "event_id": learner_result.get("learner_event_id"),
        "event_sha256": learner_result.get("learner_event_sha256"),
        "event_payload_sha256": canonical_json_sha256(
            _object(learner_result.get("learner_update"), "Learner update")
        ),
    }
    if (
        stored.get("schema_version") != "1.0.0"
        or set(commit) != {"schema_version", "learner", "interaction", "workspace", "command"}
        or commit.get("schema_version") != "1.0.0"
        or projection_receipt.receipt_id
        != workflow_step_receipt_id(
            claim.tenant_id,
            claim.job_id,
            "LEARNER_PROJECTION_COMMITTED",
        )
        or projection_receipt.step_name != "LEARNER_PROJECTION_COMMITTED"
        or projection_receipt.input_sha256 != claim.request_sha256
        or projection_receipt.output_sha256 != workflow_receipt_sha256(commit)
        or commit_learner.get("profile_sha256") != canonical_json_sha256(committed_profile)
        or commit_learner.get("projection_sha256") != canonical_json_sha256(committed_projection)
        or committed_projection != learner_result
        or commit_interaction.get("interaction_sha256")
        != canonical_json_sha256(committed_interaction)
        or committed_interaction != interaction
        or commit_workspace.get("workspace_revision")
        != _integer(committed_workspace, "workspace_revision")
        or commit_workspace.get("workspace_sha256") != canonical_json_sha256(committed_workspace)
        or commit_command.get("record_sha256") != canonical_json_sha256(committed_command)
        or committed_command != cast(dict[str, Any], json_value(command_record_data(command)))
        or learner_wire != expected_learner
        or not isinstance(learner_wire.get("profile_sha256"), str)
        or len(cast(str, learner_wire["profile_sha256"])) != 64
        or current_revision < claim.expected_revision + 1
        or current_through < claim.through_sequence
        or learner_update.get("evidence_refs") != canonical_source_refs
        or learner_result.get("source_evidence_ids") != source_ids
        or any(
            committed_catalog.get(cast(str, item["evidence_id"])) != item
            for item in canonical_source_refs
        )
        or learner_wire.get("profile_sha256") != commit_learner.get("profile_sha256")
        or (
            current_revision == claim.expected_revision + 1
            and learner.profile_json != committed_profile
        )
        or interaction_wire
        != {
            "interaction_id": interaction.get("interaction_id"),
            "sequence": interaction.get("sequence"),
            "interaction_sha256": canonical_json_sha256(interaction),
            "source_sha256": source.get("source_sha256"),
        }
        or interaction.get("sequence") != _integer(projected, "interaction_sequence")
        or stored_workspace_revision > workspace.workspace_revision
        or stored_workspace_sequence > current_workspace_sequence
        or (
            stored_workspace_revision == workspace.workspace_revision
            and workspace_wire.get("workspace_sha256")
            != canonical_json_sha256(workspace.workspace_json)
        )
        or workspace_wire.get("workspace_id") != workspace.workspace_id
        or stored.get("terminal_receipt") != _step_receipt_wire(receipt)
        or stored.get("projection_receipt") != _step_receipt_wire(projection_receipt)
        or workspace_wire.get("workspace_revision") != commit_workspace.get("workspace_revision")
        or workspace_wire.get("workspace_sha256") != commit_workspace.get("workspace_sha256")
        or workspace_wire.get("last_interaction_sequence")
        != committed_workspace.get("last_interaction_sequence")
        or command_wire
        != {
            "command_id": command.command_id,
            "status": command.status.value,
            "revision": command.revision,
            "record_sha256": canonical_json_sha256(command_record_data(command)),
        }
        or parent_wire != {"status": "SUCCEEDED", "phase": "COMPLETE"}
    ):
        raise LearnerProjectionInvariantError(
            "learner terminal result does not close its committed projections"
        )


async def _terminal_learner_result(
    session: AsyncSession,
    *,
    claim: ClaimedLearnerProjectionJob,
    learner: LearnerProfileRow,
    command: CommandRecord,
    result: SkillInvocationResult,
) -> dict[str, Any]:
    profile = dict(learner.profile_json)
    objective = dict(claim.projection)
    task = _object(objective.get("task"), "terminal learner task")
    projection = _object(objective.get("projection"), "terminal projection objective")
    feedback_event = _runtime_event(
        _object(
            objective.get("source_feedback_event"),
            "terminal feedback Event",
        )
    )
    recorded_at = _datetime(_text(projection, "recorded_at"))
    context = _operation_context(command)
    evidence_id = _identifier(
        "evidence_learner",
        claim.tenant_id,
        claim.job_id,
        "LEARNER_UPDATE",
    )
    evidence = await session.scalar(
        select(EvidenceRow).where(
            EvidenceRow.tenant_id == claim.tenant_id,
            EvidenceRow.actor_id == claim.actor_id,
            EvidenceRow.content_hash == claim.content_hash,
            EvidenceRow.command_id == claim.command_id,
            EvidenceRow.evidence_id == evidence_id,
        )
    )
    event = await session.scalar(
        select(EventRow).where(
            EventRow.tenant_id == claim.tenant_id,
            EventRow.stream_id == f"learner:{claim.learner_id}",
            EventRow.sequence == claim.through_sequence,
        )
    )
    projection_receipt = await session.scalar(
        select(JobStepReceiptRow).where(
            JobStepReceiptRow.tenant_id == claim.tenant_id,
            JobStepReceiptRow.job_id == claim.job_id,
            JobStepReceiptRow.step_name == "LEARNER_PROJECTION_COMMITTED",
        )
    )
    if evidence is None or event is None or projection_receipt is None:
        raise LearnerProjectionInvariantError("terminal Learner event or Evidence is missing")
    event_wire = dict(event.event_json)
    event_payload = _object(event_wire.get("payload"), "Learner update event payload")
    projection_outcome = (
        ProjectionOutcome.SUCCESS
        if result.run.task_success
        else (
            ProjectionOutcome.FAILED
            if result.run.failure_key == "sandbox_execution_failed"
            else ProjectionOutcome.PARTIAL
        )
    )
    assistance = await _closed_learner_assistance(
        session,
        tenant_id=claim.tenant_id,
        actor_id=claim.actor_id,
        session_id=claim.session_id,
        run_id=result.run.run_id,
    )
    if assistance != _object(
        objective.get("assistance"), "terminal frozen assistance"
    ):
        raise LearnerProjectionInvariantError("terminal Run provenance drifted")
    used_skill_patch = assistance.get("used_skill_patch") is True
    learner_payload = {
        "evidence_kind": "LEARNER_OBSERVATION",
        "observation_type": ("TASK_COMPLETION" if result.run.task_success else "CODE_ATTEMPT"),
        "task_id": _text(task, "task_id"),
        "outcome": projection_outcome.value,
        "assistance_level": 4 if used_skill_patch else 0,
    }
    payload_sha256 = canonical_json_sha256(learner_payload)
    learner_reference = EvidenceRef(
        evidence_id=evidence_id,
        evidence_type=EvidenceType.LEARNER_UPDATE,
        created_at=recorded_at,
        sha256=payload_sha256,
        uri=f"/v1/evidence/{evidence_id}",
    )
    reference_wire = _evidence_ref_wire(learner_reference)
    expected_evidence = {
        "request_context": request_context_data(context),
        "evidence_ref": reference_wire,
        "subject": {"learner_id": claim.learner_id},
        "source": {
            "source_type": "LEARNER_PROJECTOR",
            "source_id": claim.learner_id,
            "command_id": claim.command_id,
            "world_id": result.run.world_id,
        },
        "occurred_at": _timestamp(feedback_event.occurred_at),
        "recorded_at": _timestamp(recorded_at),
        "integrity": {
            "payload_sha256": payload_sha256,
            "previous_evidence_sha256": None,
        },
        "payload": learner_payload,
        "related_evidence": [_evidence_ref_wire(item) for item in result.run.evidence_refs],
        "versions": _versions_wire(command),
    }
    source_refs = [_evidence_ref_wire(item) for item in result.run.evidence_refs]
    changed_competency_ids = event_payload.get("changed_competency_ids")
    expected_update = {
        "learner_id": claim.learner_id,
        "previous_revision": claim.expected_revision,
        "learner_revision": claim.expected_revision + 1,
        "projected_through_sequence": claim.through_sequence,
        "changed_competency_ids": changed_competency_ids,
        "updated_at": _timestamp(recorded_at),
        "evidence_refs": source_refs,
    }
    evidence_ref = _object(
        evidence.evidence_json.get("evidence_ref"),
        "Learner Evidence reference",
    )
    committed_learner = _object(
        projection_receipt.receipt_json.get("learner"),
        "committed Learner authority",
    )
    committed_projection = _object(
        committed_learner.get("projection"),
        "committed Learner projection",
    )
    committed_profile = _object(
        committed_learner.get("profile"),
        "committed Learner profile",
    )
    _validate_learner_profile_evidence_catalog(profile)
    _validate_learner_profile_evidence_catalog(committed_profile)
    committed_catalog = {
        cast(str, item["evidence_id"]): item
        for item in cast(list[dict[str, Any]], committed_profile["evidence_refs"])
    }
    if (
        learner.profile_sha256 != canonical_json_sha256(profile)
        or _integer(profile, "revision") < claim.expected_revision + 1
        or _integer(profile, "projected_through_sequence") < claim.through_sequence
        or not isinstance(changed_competency_ids, list)
        or changed_competency_ids != sorted(set(changed_competency_ids))
        or _text(task, "concept") not in changed_competency_ids
        or evidence_ref != reference_wire
        or evidence.evidence_json != expected_evidence
        or event.event_json.get("event_type") != RuntimeEventType.LEARNER_MODEL_UPDATED.value
        or event.event_json.get("event_version") != 1
        or event.event_json.get("stream_id") != f"learner:{claim.learner_id}"
        or event.event_json.get("sequence") != claim.through_sequence
        or event.event_json.get("occurred_at") != cast(str, json_value(recorded_at))
        or event.event_json.get("producer") != "walnut_learner"
        or event.event_json.get("trace_id") != context.trace_id
        or event.event_json.get("command_id") != claim.command_id
        or event.event_json.get("correlation_id") != context.correlation_id
        or event.event_json.get("causation_id") != claim.source_event_id
        or event.event_json.get("content_ref")
        != cast(dict[str, Any], json_value(context.content_ref))
        or event.event_json.get("schema_version") != "1.0.0"
        or event_payload != expected_update
        or committed_projection.get("learner_update") != event_payload
        or committed_projection.get("source_evidence_ids")
        != [item.evidence_id for item in result.run.evidence_refs]
        or any(
            committed_catalog.get(cast(str, item["evidence_id"])) != item for item in source_refs
        )
        or committed_projection.get("evidence_id") != evidence_id
        or committed_projection.get("evidence_sha256")
        != canonical_json_sha256(evidence.evidence_json)
        or committed_projection.get("learner_event_id") != event.event_id
        or committed_projection.get("learner_event_sha256") != canonical_json_sha256(event_wire)
    ):
        raise LearnerProjectionInvariantError("terminal Learner projection drifted")
    return committed_projection


def _terminal_result(result: SkillInvocationResult) -> dict[str, Any] | None:
    if not result.run.task_success:
        return None
    receipt = result.run.world_commit
    if receipt is None:
        raise WorkflowInvariantError("successful Run has no World commit receipt")
    return {
        "result_type": "WORLD_COMMIT",
        "world_id": receipt.world_id,
        "previous_revision": receipt.previous_revision,
        "world_revision": receipt.world_revision,
        "first_event_sequence": receipt.first_event_sequence,
        "last_event_sequence": receipt.last_event_sequence,
    }


def _terminal_error(result: SkillInvocationResult) -> dict[str, Any] | None:
    if result.run.task_success:
        return None
    sandbox = result.run.failure_key == "sandbox_execution_failed"
    failure = ContractError(
        code="SANDBOX_RUNTIME_ERROR" if sandbox else "WORLD_RULE_REJECTED",
        category=ErrorCategory.SANDBOX if sandbox else ErrorCategory.WORLD_RULE,
        retryable=False,
        user_message_key="sandbox.runtime_error" if sandbox else "world.rule_rejected",
        stage="SANDBOX" if sandbox else "WORLD_VALIDATE",
        message=(
            "The activated Skill did not complete in its Sandbox."
            if sandbox
            else "The staged actions did not satisfy the activated World rules."
        ),
        details={
            "reason_code": _reason_code(result.run.failure_key),
            "evidence_ids": tuple(item.evidence_id for item in result.run.evidence_refs),
        },
    )
    return cast(dict[str, Any], error_data(failure))


async def _required_step_receipt(
    session: AsyncSession,
    claim: ClaimedWorkflowJob | ClaimedLearnerProjectionJob,
    step_name: str,
) -> JobStepReceiptRow:
    row = await session.scalar(
        select(JobStepReceiptRow).where(
            JobStepReceiptRow.tenant_id == claim.tenant_id,
            JobStepReceiptRow.job_id == claim.job_id,
            JobStepReceiptRow.step_name == step_name,
        )
    )
    if (
        row is None
        or row.fencing_token < 1
        or row.output_sha256 != workflow_receipt_sha256(row.receipt_json)
    ):
        raise WorkflowInvariantError(f"{step_name} receipt is missing or corrupt")
    return row


def _step_receipt_wire(receipt: JobStepReceiptRow) -> dict[str, Any]:
    return {
        "receipt_id": receipt.receipt_id,
        "step_name": receipt.step_name,
        "fencing_token": receipt.fencing_token,
        "input_sha256": receipt.input_sha256,
        "output_sha256": receipt.output_sha256,
        "receipt_json": dict(receipt.receipt_json),
        "completed_at": _timestamp(receipt.completed_at),
    }


async def _stream_sequence(
    session: AsyncSession,
    tenant_id: str,
    stream_id: str,
) -> int | str:
    row = await session.scalar(
        select(WorldStreamRow)
        .where(
            WorldStreamRow.tenant_id == tenant_id,
            WorldStreamRow.stream_id == stream_id,
        )
        .with_for_update()
    )
    return "NO_STREAM" if row is None else row.last_sequence


async def _command(
    session: AsyncSession,
    command_id: str,
    tenant_id: str,
) -> CommandRecord:
    row = await session.scalar(
        select(CommandRow)
        .where(
            CommandRow.tenant_id == tenant_id,
            CommandRow.command_id == command_id,
        )
        .with_for_update()
    )
    if row is None:
        raise WorkflowInvariantError("Turn Command disappeared before projection")
    return command_record_from_data(row.record_json)


async def _run_json(
    session: AsyncSession,
    run_id: str,
    tenant_id: str,
) -> dict[str, Any]:
    row = await session.scalar(
        select(RunRow).where(
            RunRow.tenant_id == tenant_id,
            RunRow.run_id == run_id,
        )
    )
    if row is None:
        raise WorkflowInvariantError("Run outcome receipt has no Run row")
    return dict(row.run_json)


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


def _competency(value: object, concept: str) -> CompetencyProjection | None:
    if value is None:
        return None
    wire = _object(value, f"Learner competency {concept}")
    if _text(wire, "concept") != concept:
        raise WorkflowInvariantError("Learner competency concept drifted")
    raw_ids = wire.get("evidence_ids")
    if (
        isinstance(raw_ids, str | bytes | bytearray)
        or not isinstance(raw_ids, Sequence)
        or any(not isinstance(item, str) for item in raw_ids)
    ):
        raise WorkflowInvariantError("Learner competency evidence_ids is invalid")
    try:
        return CompetencyProjection(
            concept=concept,
            evidence_stage=EvidenceStage(_text(wire, "evidence_stage")),
            assistance_level=_integer(wire, "assistance_level"),
            last_observed_at=_datetime(_text(wire, "last_observed_at")),
            next_review_at=_datetime(_text(wire, "next_review_at")),
            evidence_ids=tuple(cast(Sequence[str], raw_ids)),
        )
    except (TypeError, ValueError) as error:
        raise WorkflowInvariantError("Learner competency is not canonical") from error


def _versions_wire(command: CommandRecord) -> dict[str, Any]:
    value = cast(dict[str, Any], json_value(command.versions))
    return {key: item for key, item in value.items() if item is not None}


def _evidence_ref_wire(reference: EvidenceRef) -> dict[str, Any]:
    value: dict[str, Any] = {
        "evidence_id": reference.evidence_id,
        "evidence_type": reference.evidence_type.value,
        "created_at": _timestamp(reference.created_at),
    }
    if reference.sha256 is not None:
        value["sha256"] = reference.sha256
    if reference.uri is not None:
        value["uri"] = reference.uri
    return value


def _canonical_evidence_catalog_entry(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LearnerProjectionInvariantError("Learner Evidence catalog entry is not an object")
    wire = dict(value)
    required = {"evidence_id", "evidence_type", "created_at"}
    if not required.issubset(wire) or not set(wire).issubset(required | {"sha256", "uri"}):
        raise LearnerProjectionInvariantError("Learner Evidence catalog entry fields drifted")
    evidence_id = wire.get("evidence_id")
    evidence_type = wire.get("evidence_type")
    created_at = wire.get("created_at")
    sha256 = wire.get("sha256")
    uri = wire.get("uri")
    if (
        not isinstance(evidence_id, str)
        or not isinstance(evidence_type, str)
        or not isinstance(created_at, str)
        or (sha256 is not None and not isinstance(sha256, str))
        or (uri is not None and not isinstance(uri, str))
    ):
        raise LearnerProjectionInvariantError("Learner Evidence catalog metadata is invalid")
    try:
        reference = EvidenceRef(
            evidence_id=evidence_id,
            evidence_type=EvidenceType(evidence_type),
            created_at=_datetime(created_at),
            sha256=sha256,
            uri=uri,
        )
    except (TypeError, ValueError) as error:
        raise LearnerProjectionInvariantError(
            "Learner Evidence catalog metadata is invalid"
        ) from error
    if reference.evidence_type is EvidenceType.LEARNER_UPDATE:
        raise LearnerProjectionInvariantError(
            "derived Learner Evidence cannot support a competency catalog"
        )
    canonical = _evidence_ref_wire(reference)
    if canonical != wire:
        raise LearnerProjectionInvariantError("Learner Evidence catalog metadata is not canonical")
    return canonical


def _merge_learner_evidence_catalog(
    prior_refs: object,
    incoming_refs: object,
) -> list[dict[str, Any]]:
    if not isinstance(prior_refs, list) or not isinstance(incoming_refs, list):
        raise LearnerProjectionInvariantError("Learner Evidence catalog is not an array")
    ordered: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for raw in (*prior_refs, *incoming_refs):
        item = _canonical_evidence_catalog_entry(raw)
        evidence_id = cast(str, item["evidence_id"])
        existing = by_id.get(evidence_id)
        if existing is not None:
            if existing != item:
                raise LearnerProjectionInvariantError(
                    "Learner Evidence ID has conflicting immutable metadata"
                )
            continue
        by_id[evidence_id] = item
        ordered.append(item)
    return ordered[-64:]


def _trim_learner_competencies_to_catalog(
    competencies: Mapping[str, Any],
    catalog: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    retained_ids = {cast(str, item["evidence_id"]) for item in catalog}
    trimmed: dict[str, Any] = {}
    changed: list[str] = []
    for concept, raw in competencies.items():
        competency = _competency(raw, concept)
        if competency is None:
            continue
        evidence_ids = tuple(
            evidence_id for evidence_id in competency.evidence_ids if evidence_id in retained_ids
        )
        if not evidence_ids:
            changed.append(concept)
            continue
        value = _object(raw, f"Learner competency {concept}")
        if evidence_ids != competency.evidence_ids:
            value["evidence_ids"] = list(evidence_ids)
            changed.append(concept)
        trimmed[concept] = value
    return trimmed, tuple(sorted(changed))


def _validate_learner_profile_evidence_catalog(profile: Mapping[str, Any]) -> None:
    profile_refs = profile.get("evidence_refs")
    catalog = _merge_learner_evidence_catalog([], profile_refs)
    if catalog != profile_refs:
        raise LearnerProjectionInvariantError(
            "Learner Evidence catalog is duplicated or exceeds its global bound"
        )
    competencies = _object(profile.get("competencies"), "Learner competencies")
    retained_ids = {cast(str, item["evidence_id"]) for item in catalog}
    for concept, raw in competencies.items():
        competency = _competency(raw, concept)
        if competency is None or not set(competency.evidence_ids).issubset(retained_ids):
            raise LearnerProjectionInvariantError(
                "Learner competency Evidence is absent from the profile catalog"
            )


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


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise WorkflowInvariantError("projection timestamp must include a timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _identifier(prefix: str, *parts: str) -> str:
    framed = "\x00".join((prefix, *parts)).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(framed).hexdigest()[:24]}"


def _reason_code(value: str | None) -> str:
    if value == "sandbox_execution_failed":
        return "SANDBOX_EXECUTION_FAILED"
    return "TASK_INCOMPLETE"


async def _database_now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WorkflowInvariantError("PostgreSQL returned an invalid clock timestamp")
    return value


__all__ = [
    "finish_turn_projection",
    "project_learner_handoff",
    "validate_learner_handoff_terminal",
    "validate_terminal_learner_row_in_session",
]
