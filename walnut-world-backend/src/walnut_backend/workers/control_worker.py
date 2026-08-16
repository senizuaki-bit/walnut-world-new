"""Durable Session binding and full-scope Skill activation workflows."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal, cast

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_build import SourceBundleValidationError, validate_source_bundle
from yaya_agent_contracts import (
    CommandRecord,
    CommandStatus,
    CommandTransition,
    ContractError,
    ErrorCategory,
    Failure,
    OperationContext,
    canonical_json_sha256,
)

from walnut_backend.adapters.postgres.certification_authority import (
    validate_certification_authority,
)
from walnut_backend.adapters.postgres.command_store import PostgresCommandStore
from walnut_backend.adapters.postgres.models import (
    AgentSessionRow,
    BuildPolicyRow,
    CommandRow,
    CurrentSessionBindingRow,
    JobStepReceiptRow,
    LaunchAuthorityRow,
    ProductContentUnitRow,
    ProductDraftRow,
    ProductWorkspaceRow,
    RegistryEntryRow,
    RegistryHeadRow,
    SkillActivationProvenanceRow,
    SkillActivationRow,
    SkillCertificationProvenanceRow,
    SkillCertificationRevocationRow,
    SkillCertificationRow,
    WorkflowJobRow,
    WorldSnapshotRow,
    command_record_from_data,
    error_data,
    request_context_data,
)
from walnut_backend.adapters.postgres.product_drafts import (
    append_draft_revision_in_session,
    draft_resource,
)
from walnut_backend.adapters.postgres.product_workspaces import initial_workspace_resource
from walnut_backend.adapters.postgres.skill_provenance import (
    activation_provenance_sha256,
    activation_receipt_authority_sha256,
    activation_workflow_job_sha256,
)
from walnut_backend.adapters.postgres.workflow_jobs import (
    ClaimedWorkflowJob,
    PostgresWorkflowJobStore,
    WorkflowInvariantError,
)


class _TerminalControlError(RuntimeError):
    def __init__(
        self,
        error: ContractError,
        status: Literal["REJECTED", "FAILED"] = "REJECTED",
    ) -> None:
        super().__init__(error.code)
        self.error = error
        self.status = status


class ControlWorkflowHandler:
    """Materialize control-plane resources under the workflow fence.

    These operations have no external side effect.  Their receipt, resource,
    registry CAS, Command transition, and terminal Job state therefore commit
    in one PostgreSQL transaction.
    """

    operations = frozenset({"CREATE_AGENT_SESSION", "ACTIVATE_SKILL_VERSION"})

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        command_store: PostgresCommandStore,
        workflow_jobs: PostgresWorkflowJobStore,
        *,
        lease_seconds: int = 30,
    ) -> None:
        self._sessions = session_factory
        self._commands = command_store
        self._jobs = workflow_jobs
        self._lease_seconds = lease_seconds

    async def execute(self, claim: ClaimedWorkflowJob) -> None:
        if claim.operation not in self.operations:
            raise ValueError(f"unsupported control operation {claim.operation}")
        try:
            if claim.operation == "CREATE_AGENT_SESSION":
                await self._bind_session(claim)
            else:
                await self._activate_skill(claim)
        except _TerminalControlError as error:
            await self._finish_error(claim, error)

    async def _bind_session(self, claim: ClaimedWorkflowJob) -> None:
        async with self._sessions() as session, session.begin():
            owned = await self._jobs.start_step_in_session(
                session,
                claim,
                phase="VALIDATE",
                lease_seconds=self._lease_seconds,
            )
            command = await _command(session, owned)
            workflow = await _workflow_job(session, owned)
            context = _operation_context(command)
            resource = await session.scalar(
                select(AgentSessionRow)
                .where(
                    AgentSessionRow.session_id == owned.subject_id,
                    AgentSessionRow.tenant_id == owned.tenant_id,
                    AgentSessionRow.actor_id == command.request_context.actor.actor_id,
                    AgentSessionRow.command_id == owned.command_id,
                )
                .with_for_update()
            )
            if resource is None:
                raise _TerminalControlError(
                    _invariant("Session resource is missing from its accepted workflow"),
                    "FAILED",
                )
            job = _object(owned.job, "job")
            request = _object(job.get("request"), "job.request")
            if job.get("session_id") != resource.session_id:
                raise _TerminalControlError(
                    _invariant("Session workflow identity drifted"), "FAILED"
                )
            content = _object(request.get("content"), "session content")
            if (
                content.get("content_hash")
                != command.request_context.content_ref.content_hash
                or content.get("unit_id") != command.request_context.content_ref.unit_id
                or content.get("version") != command.request_context.content_ref.version
            ):
                raise _TerminalControlError(
                    _mismatch("Session content differs from authenticated authority")
                )
            authority = await session.scalar(
                select(LaunchAuthorityRow).where(
                    LaunchAuthorityRow.tenant_id == owned.tenant_id,
                    LaunchAuthorityRow.actor_id
                    == command.request_context.actor.actor_id,
                    LaunchAuthorityRow.content_hash
                    == command.request_context.content_ref.content_hash,
                    LaunchAuthorityRow.world_id == request.get("world_id"),
                    LaunchAuthorityRow.learner_id == request.get("learner_id"),
                    LaunchAuthorityRow.agent_profile_id
                    == request.get("agent_profile_id"),
                    LaunchAuthorityRow.channel == request.get("channel"),
                    LaunchAuthorityRow.active.is_(True),
                )
            )
            if authority is None:
                raise _TerminalControlError(
                    _mismatch("Session request is outside the active launch authority")
                )
            snapshot = await session.scalar(
                select(WorldSnapshotRow).where(
                    WorldSnapshotRow.tenant_id == owned.tenant_id,
                    WorldSnapshotRow.world_id == authority.world_id,
                    WorldSnapshotRow.actor_id == authority.actor_id,
                    WorldSnapshotRow.content_hash == authority.content_hash,
                )
            )
            if snapshot is None:
                raise _TerminalControlError(
                    _invariant("launch authority World snapshot is missing"), "FAILED"
                )
            published = await session.scalar(
                select(ProductContentUnitRow).where(
                    ProductContentUnitRow.tenant_id == authority.tenant_id,
                    ProductContentUnitRow.unit_id == authority.content_unit_id,
                    ProductContentUnitRow.version == authority.content_version,
                    ProductContentUnitRow.content_hash == authority.content_hash,
                )
            )
            policy = await session.scalar(
                select(BuildPolicyRow).where(
                    BuildPolicyRow.tenant_id == authority.tenant_id,
                    BuildPolicyRow.build_policy_id == authority.build_policy_id,
                    BuildPolicyRow.actor_id == authority.actor_id,
                    BuildPolicyRow.content_hash == authority.content_hash,
                    BuildPolicyRow.active.is_(True),
                )
            )
            if published is None or policy is None or "LEARNER" not in published.audiences:
                raise _TerminalControlError(
                    _invariant("Session starter authorities are missing"), "FAILED"
                )
            task = _object(published.content_json.get("task"), "published task")
            starter = _object(task.get("starter_skill"), "published starter_skill")
            source_bundle = _object(starter.get("source_bundle"), "starter source_bundle")
            try:
                validate_source_bundle(source_bundle)
            except SourceBundleValidationError as error:
                raise _TerminalControlError(
                    _invariant(f"published starter source is invalid: {error.code}"),
                    "FAILED",
                ) from error
            task_capabilities = task.get("allowed_capabilities")
            if (
                not isinstance(task_capabilities, list)
                or any(not isinstance(item, str) for item in task_capabilities)
                or not set(task_capabilities) <= set(policy.allowed_capabilities)
                or starter.get("compiler_profile") != policy.compiler_profile
                or starter.get("test_suite_version") != policy.test_suite_version
            ):
                raise _TerminalControlError(
                    _invariant("published starter differs from active BuildPolicy"), "FAILED"
                )
            supplied_revision = request.get("expected_world_revision")
            if supplied_revision is not None and supplied_revision != snapshot.revision:
                raise _TerminalControlError(
                    _conflict("expected_world_revision is stale")
                )
            durable = dict(resource.session_json)
            if (
                durable.get("world_id") != authority.world_id
                or durable.get("learner_id") != authority.learner_id
                or durable.get("agent_profile_id") != authority.agent_profile_id
                or durable.get("channel") != authority.channel
                or durable.get("content") != content
                or durable.get("status") != "ACTIVE"
            ):
                raise _TerminalControlError(
                    _invariant("Session resource differs from launch authority"), "FAILED"
                )
            existing = await session.scalar(
                select(CurrentSessionBindingRow)
                .where(
                    CurrentSessionBindingRow.tenant_id == owned.tenant_id,
                    CurrentSessionBindingRow.authority_id == authority.authority_id,
                )
                .with_for_update()
            )
            now = max(
                await _database_now(session),
                command.updated_at,
                workflow.updated_at,
            )
            binding_id = _identifier(
                "binding", owned.tenant_id, authority.authority_id, resource.session_id
            )
            if existing is None:
                session.add(
                    CurrentSessionBindingRow(
                        binding_id=binding_id,
                        tenant_id=owned.tenant_id,
                        authority_id=authority.authority_id,
                        session_id=resource.session_id,
                        actor_id=authority.actor_id,
                        content_hash=authority.content_hash,
                        world_id=authority.world_id,
                        learner_id=authority.learner_id,
                        agent_profile_id=authority.agent_profile_id,
                        bound_at=now,
                    )
                )
            elif (
                existing.binding_id != binding_id
                or existing.session_id != resource.session_id
                or existing.actor_id != authority.actor_id
                or existing.content_hash != authority.content_hash
                or existing.world_id != authority.world_id
                or existing.learner_id != authority.learner_id
                or existing.agent_profile_id != authority.agent_profile_id
            ):
                raise _TerminalControlError(
                    _conflict("launch authority already has another current Session")
                )
            existing_draft = await session.scalar(
                select(ProductDraftRow).where(
                    ProductDraftRow.tenant_id == owned.tenant_id,
                    ProductDraftRow.session_id == resource.session_id,
                )
            )
            existing_workspace = await session.scalar(
                select(ProductWorkspaceRow).where(
                    ProductWorkspaceRow.tenant_id == owned.tenant_id,
                    ProductWorkspaceRow.session_id == resource.session_id,
                )
            )
            if existing_draft is not None or existing_workspace is not None:
                raise _TerminalControlError(
                    _invariant("Session starter projections already exist before receipt"),
                    "FAILED",
                )
            skill_id = _text(starter, "skill_id")
            draft_id = _identifier(
                "draft", owned.tenant_id, resource.session_id, skill_id
            )
            draft = draft_resource(
                {
                    "session_id": resource.session_id,
                    "draft_id": draft_id,
                    "skill_id": skill_id,
                    "content_ref": content,
                    "display_name": _text(starter, "display_name"),
                    "source_bundle": source_bundle,
                },
                _object(durable.get("request_context"), "Session request_context"),
                1,
                now,
                now,
                None,
            )
            session.add(
                ProductDraftRow(
                    tenant_id=owned.tenant_id,
                    actor_id=authority.actor_id,
                    session_id=resource.session_id,
                    draft_id=draft_id,
                    skill_id=skill_id,
                    revision=1,
                    draft_sha256=draft["draft_sha256"],
                    created_at=now,
                    updated_at=now,
                    draft_json=draft,
                )
            )
            append_draft_revision_in_session(
                session,
                tenant_id=owned.tenant_id,
                actor_id=authority.actor_id,
                draft=draft,
                source_kind="STUDENT",
                patch_id=None,
                created_at=now,
            )
            workspace = initial_workspace_resource(
                tenant_id=owned.tenant_id,
                session_resource=durable,
                world_revision=snapshot.revision,
                last_event_sequence=snapshot.last_event_sequence,
                state_hash=snapshot.state_hash,
                draft_resource=draft,
                task_id=_text(task, "task_id"),
                created_at=now,
            )
            session.add(
                ProductWorkspaceRow(
                    workspace_id=workspace["workspace_id"],
                    tenant_id=owned.tenant_id,
                    actor_id=authority.actor_id,
                    session_id=resource.session_id,
                    workspace_revision=1,
                    updated_at=now,
                    workspace_json=workspace,
                )
            )
            receipt = await self._jobs.record_step_in_session(
                session,
                owned,
                step_name="SESSION_BOUND",
                input_sha256=owned.request_sha256,
                output={
                    "binding_id": binding_id,
                    "authority_id": authority.authority_id,
                    "session_id": resource.session_id,
                    "world_id": authority.world_id,
                    "world_revision": snapshot.revision,
                    "draft_id": draft_id,
                    "draft_sha256": draft["draft_sha256"],
                    "workspace_id": workspace["workspace_id"],
                    "workspace_revision": workspace["workspace_revision"],
                },
            )
            await self._apply_command(
                session,
                command,
                context,
                stage="VALIDATE",
                causal_floor=max(now, receipt.completed_at),
                result={
                    "result_type": "RESOURCE_CREATED",
                    "resource_type": "AGENT_SESSION",
                    "resource_id": resource.session_id,
                    "resource_url": f"/v1/agent-sessions/{resource.session_id}",
                },
            )
            await self._jobs.finish_in_session(session, owned, status="SUCCEEDED")

    async def _activate_skill(self, claim: ClaimedWorkflowJob) -> None:
        async with self._sessions() as session, session.begin():
            owned = await self._jobs.start_step_in_session(
                session,
                claim,
                phase="REGISTRY",
                lease_seconds=self._lease_seconds,
            )
            command = await _command(session, owned)
            workflow = await _workflow_job(session, owned)
            context = _operation_context(command)
            job = _object(owned.job, "job")
            scope = _object(job.get("activation_scope"), "activation_scope")
            skill = _object(job.get("skill"), "skill")
            expected_revision = job.get("expected_registry_revision")
            if isinstance(expected_revision, bool) or not isinstance(
                expected_revision, int
            ):
                raise _TerminalControlError(
                    _invariant("Activation workflow revision is invalid"), "FAILED"
                )
            authority = await session.scalar(
                select(LaunchAuthorityRow).where(
                    LaunchAuthorityRow.tenant_id == owned.tenant_id,
                    LaunchAuthorityRow.authority_id == job.get("authority_id"),
                    LaunchAuthorityRow.actor_id
                    == command.request_context.actor.actor_id,
                    LaunchAuthorityRow.content_hash
                    == command.request_context.content_ref.content_hash,
                    LaunchAuthorityRow.world_id == scope.get("world_id"),
                    LaunchAuthorityRow.agent_profile_id
                    == scope.get("agent_profile_id"),
                    LaunchAuthorityRow.active.is_(True),
                )
            )
            if authority is None:
                raise _TerminalControlError(
                    _mismatch("Activation launch authority is no longer active")
                )
            head = await session.scalar(
                select(RegistryHeadRow)
                .where(
                    RegistryHeadRow.tenant_id == owned.tenant_id,
                    RegistryHeadRow.actor_id == authority.actor_id,
                    RegistryHeadRow.content_hash == authority.content_hash,
                    RegistryHeadRow.world_id == authority.world_id,
                    RegistryHeadRow.agent_profile_id == authority.agent_profile_id,
                    RegistryHeadRow.authority_id == authority.authority_id,
                )
                .with_for_update()
            )
            if head is None:
                raise _TerminalControlError(
                    _invariant("server-owned registry head disappeared"), "FAILED"
                )
            if head.revision != expected_revision:
                raise _TerminalControlError(
                    _mismatch("registry revision changed before activation commit")
                )
            certification = await session.scalar(
                select(SkillCertificationRow)
                .where(
                    SkillCertificationRow.tenant_id == owned.tenant_id,
                    SkillCertificationRow.certification_id
                    == skill.get("certification_id"),
                    SkillCertificationRow.skill_id == skill.get("skill_id"),
                    SkillCertificationRow.skill_version_id
                    == skill.get("skill_version_id"),
                    SkillCertificationRow.artifact_sha256
                    == skill.get("artifact_sha256"),
                    SkillCertificationRow.actor_id == authority.actor_id,
                    SkillCertificationRow.content_hash == authority.content_hash,
                )
                .with_for_update()
            )
            revoked = (
                await session.scalar(
                    select(
                        exists().where(
                            SkillCertificationRevocationRow.tenant_id
                            == owned.tenant_id,
                            SkillCertificationRevocationRow.certification_id
                            == skill.get("certification_id"),
                        )
                    )
                )
            )
            if certification is None or revoked is True:
                raise _TerminalControlError(_not_certified())
            build_provenance_digest = job.get("build_provenance_sha256")
            certification_digest = job.get("certification_sha256")
            artifact_digest = job.get("artifact_authority_sha256")
            closed_certification = await validate_certification_authority(
                session,
                certification,
                expected_certification_sha256=(
                    certification_digest if isinstance(certification_digest, str) else ""
                ),
                expected_artifact_authority_sha256=(
                    artifact_digest if isinstance(artifact_digest, str) else ""
                ),
                expected_build_provenance_sha256=(
                    build_provenance_digest
                    if isinstance(build_provenance_digest, str)
                    else ""
                ),
                for_update=True,
            )
            if closed_certification is None:
                raise _TerminalControlError(
                    _invariant("Activation Certification provenance is missing or corrupt"),
                    "FAILED",
                )
            artifact, build_provenance = closed_certification
            certification_provenance = await session.scalar(
                select(SkillCertificationProvenanceRow).where(
                    SkillCertificationProvenanceRow.certification_id
                    == certification.certification_id,
                    SkillCertificationProvenanceRow.tenant_id == owned.tenant_id,
                    SkillCertificationProvenanceRow.actor_id == authority.actor_id,
                    SkillCertificationProvenanceRow.build_id == certification.build_id,
                )
            )
            if certification_provenance is None:
                raise _TerminalControlError(
                    _invariant("Activation Certification seal disappeared"), "FAILED"
                )
            now = max(
                await _database_now(session),
                command.updated_at,
                workflow.updated_at,
            )
            next_revision = expected_revision + 1
            activation_id = cast(str, job.get("activation_id"))
            wire = {
                "request_context": request_context_data(command.request_context),
                "activation_id": activation_id,
                "skill_id": certification.skill_id,
                "skill_version_id": certification.skill_version_id,
                "certification_id": certification.certification_id,
                "artifact_sha256": certification.artifact_sha256,
                "activation_scope": {
                    "world_id": authority.world_id,
                    "agent_profile_id": authority.agent_profile_id,
                },
                "previous_registry_revision": expected_revision,
                "registry_revision": next_revision,
                "activated_at": _iso(now),
            }
            entry = {
                "authority_id": authority.authority_id,
                "activation_id": activation_id,
                "actor_id": authority.actor_id,
                "content_hash": authority.content_hash,
                "world_id": authority.world_id,
                "agent_profile_id": authority.agent_profile_id,
                "skill_id": certification.skill_id,
                "skill_version_id": certification.skill_version_id,
                "certification_id": certification.certification_id,
                "artifact_sha256": certification.artifact_sha256,
                "previous_revision": expected_revision,
                "revision": next_revision,
                "activated_at": _iso(now),
            }
            session.add(
                RegistryEntryRow(
                    tenant_id=owned.tenant_id,
                    actor_id=authority.actor_id,
                    content_hash=authority.content_hash,
                    world_id=authority.world_id,
                    agent_profile_id=authority.agent_profile_id,
                    revision=next_revision,
                    skill_id=certification.skill_id,
                    skill_version_id=certification.skill_version_id,
                    certification_id=certification.certification_id,
                    artifact_sha256=certification.artifact_sha256,
                    previous_revision=expected_revision,
                    entry_sha256=canonical_json_sha256(entry),
                    entry_json=entry,
                    activated_at=now,
                )
            )
            session.add(
                SkillActivationRow(
                    activation_id=activation_id,
                    tenant_id=owned.tenant_id,
                    actor_id=authority.actor_id,
                    content_hash=authority.content_hash,
                    world_id=authority.world_id,
                    agent_profile_id=authority.agent_profile_id,
                    skill_id=certification.skill_id,
                    skill_version_id=certification.skill_version_id,
                    certification_id=certification.certification_id,
                    artifact_sha256=certification.artifact_sha256,
                    previous_registry_revision=expected_revision,
                    registry_revision=next_revision,
                    activation_sha256=canonical_json_sha256(wire),
                    activation_json=wire,
                    activated_at=now,
                )
            )
            # The provenance row has an explicit FK to this immutable
            # Activation but no ORM relationship; establish the parent before
            # adding the seal so SQLAlchemy cannot choose an unsafe insert order.
            await session.flush()
            head.revision = next_revision
            head.updated_at = now
            await session.flush()
            receipt = await self._jobs.record_step_in_session(
                session,
                owned,
                step_name="REGISTRY_ACTIVATED",
                input_sha256=owned.request_sha256,
                output={
                    "activation_id": activation_id,
                    "previous_registry_revision": expected_revision,
                    "registry_revision": next_revision,
                    "entry_sha256": canonical_json_sha256(entry),
                    "activation_sha256": canonical_json_sha256(wire),
                    "certification_sha256": certification.certification_sha256,
                    "artifact_authority_sha256": cast(str, artifact_digest),
                    "build_provenance_sha256": build_provenance.authority_sha256,
                },
            )
            await self._apply_command(
                session,
                command,
                context,
                stage="REGISTRY",
                causal_floor=max(now, receipt.completed_at),
                result={
                    "result_type": "RESOURCE_CREATED",
                    "resource_type": "SKILL_ACTIVATION",
                    "resource_id": activation_id,
                    "resource_url": f"/v1/skill-activations/{activation_id}",
                },
            )
            await self._jobs.finish_in_session(session, owned, status="SUCCEEDED")
            terminal_job = await session.scalar(
                select(WorkflowJobRow).where(
                    WorkflowJobRow.tenant_id == owned.tenant_id,
                    WorkflowJobRow.job_id == owned.job_id,
                )
            )
            receipt_row = await session.scalar(
                select(JobStepReceiptRow).where(
                    JobStepReceiptRow.tenant_id == owned.tenant_id,
                    JobStepReceiptRow.receipt_id == receipt.receipt_id,
                )
            )
            if (
                terminal_job is None
                or terminal_job.status != "SUCCEEDED"
                or receipt_row is None
            ):
                raise WorkflowInvariantError(
                    "Activation terminal workflow authority did not materialize"
                )
            activation_provenance = SkillActivationProvenanceRow(
                activation_id=activation_id,
                tenant_id=owned.tenant_id,
                actor_id=authority.actor_id,
                build_id=certification.build_id,
                build_authority_sha256=build_provenance.authority_sha256,
                certification_id=certification.certification_id,
                certification_sha256=certification.certification_sha256,
                certification_authority_sha256=(
                    certification_provenance.authority_sha256
                ),
                artifact_sha256=certification.artifact_sha256,
                artifact_authority_sha256=cast(str, artifact_digest),
                registry_revision=next_revision,
                activation_sha256=canonical_json_sha256(wire),
                launch_authority_id=authority.authority_id,
                entry_sha256=canonical_json_sha256(entry),
                workflow_job_id=terminal_job.job_id,
                workflow_request_sha256=terminal_job.request_sha256,
                workflow_job_sha256=activation_workflow_job_sha256(terminal_job),
                activation_receipt_id=receipt_row.receipt_id,
                activation_receipt_sha256=activation_receipt_authority_sha256(
                    receipt_row
                ),
                authority_sha256="0" * 64,
                created_at=terminal_job.updated_at,
            )
            activation_provenance.authority_sha256 = activation_provenance_sha256(
                activation_provenance
            )
            session.add(activation_provenance)

    async def _apply_command(
        self,
        session: AsyncSession,
        command: CommandRecord,
        context: OperationContext,
        *,
        stage: Literal["VALIDATE", "REGISTRY"],
        causal_floor: datetime,
        result: Mapping[str, Any],
    ) -> None:
        now = max(await _database_now(session), command.updated_at, causal_floor)
        current = command
        if current.status is CommandStatus.ACCEPTED:
            validating = replace(
                current,
                status=CommandStatus.VALIDATING,
                stage=stage,
                revision=current.revision + 1,
                updated_at=now,
            )
            transitioned = await self._commands.transition_in_session(
                session, CommandTransition(current, validating), context
            )
            if isinstance(transitioned, Failure):
                raise WorkflowInvariantError("Command validating CAS was lost")
            current = transitioned.value
        applied = replace(
            current,
            status=CommandStatus.APPLIED,
            stage="COMPLETE",
            terminal=True,
            result=dict(result),
            error=None,
            revision=current.revision + 1,
            updated_at=now,
        )
        transitioned = await self._commands.transition_in_session(
            session, CommandTransition(current, applied), context
        )
        if isinstance(transitioned, Failure):
            raise WorkflowInvariantError("Command applied CAS was lost")

    async def _finish_error(
        self, claim: ClaimedWorkflowJob, failure: _TerminalControlError
    ) -> None:
        async with self._sessions() as session, session.begin():
            command = await _command(session, claim)
            context = _operation_context(command)
            receipt = await self._jobs.record_step_in_session(
                session,
                claim,
                step_name="CONTROL_REJECTED",
                input_sha256=claim.request_sha256,
                output={"error": error_data(failure.error)},
            )
            status = (
                CommandStatus.REJECTED
                if failure.status == "REJECTED"
                else CommandStatus.FAILED
            )
            now = max(
                await _database_now(session),
                command.updated_at,
                receipt.completed_at,
            )
            if claim.operation == "CREATE_AGENT_SESSION":
                resource = await session.scalar(
                    select(AgentSessionRow)
                    .where(
                        AgentSessionRow.tenant_id == claim.tenant_id,
                        AgentSessionRow.session_id == claim.subject_id,
                        AgentSessionRow.command_id == claim.command_id,
                    )
                    .with_for_update()
                )
                if resource is not None:
                    failed_session = dict(resource.session_json)
                    failed_session["status"] = "FAILED"
                    failed_session["updated_at"] = _iso(now)
                    resource.status = "FAILED"
                    resource.updated_at = now
                    resource.session_json = failed_session
            terminal = replace(
                command,
                status=status,
                stage=failure.error.stage,
                terminal=True,
                result=None,
                error=failure.error,
                revision=command.revision + 1,
                updated_at=now,
            )
            transitioned = await self._commands.transition_in_session(
                session, CommandTransition(command, terminal), context
            )
            if isinstance(transitioned, Failure):
                raise WorkflowInvariantError("Command rejection CAS was lost")
            await self._jobs.finish_in_session(
                session,
                claim,
                status="FAILED",
                phase=failure.error.stage,
                error=cast(dict[str, Any], error_data(failure.error)),
            )


async def _command(
    session: AsyncSession, claim: ClaimedWorkflowJob
) -> CommandRecord:
    row = await session.scalar(
        select(CommandRow)
        .where(
            CommandRow.command_id == claim.command_id,
            CommandRow.tenant_id == claim.tenant_id,
        )
        .with_for_update()
    )
    if row is None:
        raise WorkflowInvariantError("workflow Command disappeared")
    command = command_record_from_data(row.record_json)
    if (
        command.command_id != claim.command_id
        or command.command_type != claim.operation
        or command.terminal
    ):
        raise WorkflowInvariantError("workflow Command identity or state drifted")
    return command


async def _workflow_job(
    session: AsyncSession, claim: ClaimedWorkflowJob
) -> WorkflowJobRow:
    row = await session.scalar(
        select(WorkflowJobRow)
        .where(
            WorkflowJobRow.tenant_id == claim.tenant_id,
            WorkflowJobRow.job_id == claim.job_id,
            WorkflowJobRow.command_id == claim.command_id,
            WorkflowJobRow.fencing_token == claim.fencing_token,
            WorkflowJobRow.lease_owner == claim.lease_owner,
            WorkflowJobRow.status == "RUNNING",
        )
        .with_for_update()
    )
    if row is None:
        raise WorkflowInvariantError("owned workflow Job disappeared after step start")
    return row


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


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _TerminalControlError(
            _invariant(f"{label} is not a durable object"), "FAILED"
        )
    return dict(value)


def _text(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise _TerminalControlError(
            _invariant(f"{key} is not durable text"), "FAILED"
        )
    return item


async def _database_now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WorkflowInvariantError("PostgreSQL returned an invalid timestamp")
    return value.astimezone(UTC)


def _identifier(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x00".join((prefix, *parts)).encode()).hexdigest()
    return f"{prefix}_{digest[:24]}"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _error(
    code: str,
    category: ErrorCategory,
    stage: str,
    message: str,
    key: str,
    *,
    retryable: bool = False,
) -> ContractError:
    return ContractError(code, category, retryable, key, stage, message)


def _mismatch(message: str) -> ContractError:
    return _error(
        "CONTENT_VERSION_MISMATCH",
        ErrorCategory.VALIDATION,
        "REGISTRY",
        message,
        "content.version_mismatch",
    )


def _conflict(message: str) -> ContractError:
    return _error(
        "WORLD_REVISION_CONFLICT",
        ErrorCategory.CONCURRENCY,
        "WORLD_VALIDATE",
        message,
        "world.changed_retry",
        retryable=True,
    )


def _not_certified() -> ContractError:
    return _error(
        "SKILL_NOT_CERTIFIED",
        ErrorCategory.SKILL,
        "REGISTRY",
        "certification is missing or revoked",
        "skill.not_certified",
    )


def _invariant(message: str) -> ContractError:
    return _error(
        "INVARIANT_VIOLATION",
        ErrorCategory.INVARIANT,
        "VALIDATE",
        message,
        "system.invariant_violation",
    )


__all__ = ["ControlWorkflowHandler"]
