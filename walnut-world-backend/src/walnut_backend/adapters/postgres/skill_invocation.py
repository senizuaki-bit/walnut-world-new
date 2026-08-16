"""Fenced, receipt-backed Skill invocation owned by the Walnut database."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    CommandRecord,
    CommandStatus,
    CommandTransition,
    ContractError,
    ErrorCategory,
    EvidenceRef,
    EvidenceType,
    Failure,
    FrozenJsonObject,
    OperationContext,
    RequestContext,
    SandboxLimits,
    SandboxRunRequest,
    SandboxRunResult,
    SkillRef,
    Success,
    UncommittedEvent,
    VersionSet,
    WorldAtomicCommit,
    WorldCommand,
    WorldCommitReceipt,
    WorldSnapshot,
    canonical_json_sha256,
)
from yaya_agent_runtime import (
    AgentToolExecutionError,
    RunResultSnapshot,
    SkillInvocationRequest,
    SkillInvocationResult,
    side_effect_execution_id,
    world_commit_receipt_sha256,
)
from yaya_agent_sandbox import RecoverableSandboxPort

from walnut_backend.domain.world.engine import WorldEngine, WorldTransition
from walnut_backend.domain.world.rules import WorldRules
from walnut_backend.domain.world.state import WorldRuleViolation

from .activation_authority import (
    ActivationAuthorityNotFound,
    load_current_activation_authority,
)
from .command_store import PostgresCommandStore
from .models import (
    AgentSessionRow,
    AgentTurnRow,
    BuildPolicyRow,
    CommandRow,
    CurrentSessionBindingRow,
    EvidenceRow,
    JobStepReceiptRow,
    LaunchAuthorityRow,
    ProductWorkspaceRow,
    RunRow,
    SkillActivationProvenanceRow,
    SkillActivationRow,
    SkillArtifactRow,
    SkillBuildProvenanceRow,
    SkillCertificationRevocationRow,
    SkillCertificationRow,
    SkillRunProvenanceRow,
    WorldSnapshotRow,
    command_record_from_data,
    error_data,
    json_value,
    request_context_data,
    request_context_from_data,
    world_snapshot_from_data,
)
from .product_workspaces import refresh_workspace_in_session
from .skill_provenance import (
    activation_provenance_sha256,
    active_build_matches_current_patch_origin,
    run_provenance_sha256,
    validate_build_provenance,
)
from .workflow_jobs import (
    ClaimedWorkflowJob,
    PostgresWorkflowJobStore,
    WorkflowFenceLost,
    WorkflowInvariantError,
)
from .world import PostgresWorldUnitOfWork, world_commit_identifier


@dataclass(frozen=True, slots=True)
class _InvocationAuthority:
    command: CommandRecord
    world: WorldSnapshot
    activation: SkillActivationRow
    artifact: SkillArtifactRow
    policy: BuildPolicyRow


class PostgresFencedSkillInvocation:
    """A claim-scoped ``SkillInvocationPort`` with one immutable Run receipt."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        commands: PostgresCommandStore,
        jobs: PostgresWorkflowJobStore,
        claim: ClaimedWorkflowJob,
        sandbox: RecoverableSandboxPort,
        limits: SandboxLimits,
        versions: VersionSet,
        world_uow: PostgresWorldUnitOfWork,
        world_engine: WorldEngine,
        rules_by_version: Mapping[str, WorldRules],
        lease_seconds: int,
        skill_patch_enabled: bool = False,
    ) -> None:
        if claim.operation != "EXECUTE_AGENT_TURN" or claim.subject_type != "AGENT_TURN":
            raise ValueError("Skill invocation requires one claimed Agent Turn")
        if not 30 <= lease_seconds <= 3600:
            raise ValueError("Skill invocation lease must be between 30 and 3600 seconds")
        if not isinstance(skill_patch_enabled, bool):
            raise TypeError("skill_patch_enabled must be a boolean")
        self._sessions = session_factory
        self._commands = commands
        self._jobs = jobs
        self._claim = claim
        self._sandbox = sandbox
        self._limits = limits
        self._versions = versions
        self._world_uow = world_uow
        self._world_engine = world_engine
        self._rules = dict(rules_by_version)
        self._skill_patch_enabled = skill_patch_enabled
        self._lease_seconds = lease_seconds

    async def get_result(
        self, invocation_id: str, context: OperationContext
    ) -> SkillInvocationResult | None:
        async with self._sessions() as session:
            row = await _step(session, self._claim, "SKILL_INVOKED")
        if row is None:
            return None
        result = _invocation_result_from_data(_object(row.receipt_json, "invocation receipt"))
        _validate_result_identity(result, invocation_id, context, self._claim)
        return result

    async def invoke(
        self, request: SkillInvocationRequest, context: OperationContext
    ) -> SkillInvocationResult:
        _validate_request_identity(request, context, self._claim)
        async with self._sessions() as session:
            completed = await _step(session, self._claim, "SKILL_INVOKED")
            dispatched = await _step(session, self._claim, "SANDBOX_DISPATCHED")
        if completed is not None:
            result = _invocation_result_from_data(
                _object(completed.receipt_json, "invocation receipt")
            )
            _validate_result_request(result, request, context)
            return result
        run_id = _identifier("run", request.invocation_id)
        if dispatched is not None:
            _validate_dispatch_receipt(dispatched, request, run_id)
            async with self._sessions() as session:
                authority = await _load_authority(
                    session,
                    request,
                    context,
                    self._claim,
                    for_update=False,
                )
                await _require_int2_build_provenance(
                    session,
                    authority,
                    request,
                    required=True,
                )
            sandbox_result = await self._sandbox.reconcile(
                _sandbox_request(request, authority.world, run_id, self._limits),
                context,
            )
            if sandbox_result is None:
                raise AgentToolExecutionError(
                    "UNKNOWN_COMMIT_STATE",
                    "The dispatched Sandbox outcome is not terminal or observable yet.",
                    {
                        "runtime_warning": "SIDE_EFFECT_COMMIT_UNKNOWN",
                        "dispatch_receipt_id": dispatched.receipt_id,
                        "retryable": True,
                    },
                )
            return await self._publish(request, context, authority, sandbox_result)

        authority = await self._prepare_dispatch(request, context, run_id)
        sandbox_result = await self._sandbox.run(
            _sandbox_request(request, authority.world, run_id, self._limits),
            context,
        )
        return await self._publish(request, context, authority, sandbox_result)

    async def _prepare_dispatch(
        self,
        request: SkillInvocationRequest,
        context: OperationContext,
        run_id: str,
    ) -> _InvocationAuthority:
        async with self._sessions() as session, session.begin():
            await self._jobs.start_step_in_session(
                session,
                self._claim,
                phase="SANDBOX",
                lease_seconds=self._lease_seconds,
            )
            authority = await _load_authority(
                session, request, context, self._claim, for_update=True
            )
            await _require_int2_build_provenance(
                session,
                authority,
                request,
                required=True,
            )
            command = await _advance_to_sandbox(
                session,
                authority.command,
                context,
                self._commands,
                run_id=run_id,
            )
            await self._jobs.record_step_in_session(
                session,
                self._claim,
                step_name="SANDBOX_DISPATCHED",
                input_sha256=request.request_sha256,
                output={
                    "schema_version": "1.0.0",
                    "invocation_id": request.invocation_id,
                    "run_id": run_id,
                    "request_sha256": request.request_sha256,
                    "arguments": json_value(request.arguments),
                    "skill": _skill_ref_wire(request.skill_ref),
                    "world_id": request.world_id,
                    "expected_world_revision": request.expected_world_revision,
                },
            )
            return replace(authority, command=command)

    async def _publish(
        self,
        request: SkillInvocationRequest,
        context: OperationContext,
        initial: _InvocationAuthority,
        sandbox_result: Success[SandboxRunResult] | Failure,
    ) -> SkillInvocationResult:
        async with self._sessions() as session, session.begin():
            await self._jobs.start_step_in_session(
                session,
                self._claim,
                phase="WORLD_COMMIT",
                lease_seconds=self._lease_seconds,
            )
            completed = await _step(session, self._claim, "SKILL_INVOKED")
            if completed is not None:
                replay = _invocation_result_from_data(
                    _object(completed.receipt_json, "invocation receipt")
                )
                _validate_result_request(replay, request, context)
                return replay
            current = await _load_authority(session, request, context, self._claim, for_update=True)
            if (
                current.activation.activation_sha256 != initial.activation.activation_sha256
                or current.artifact.artifact_sha256 != initial.artifact.artifact_sha256
                or current.world.revision != initial.world.revision
                or current.world.last_event_sequence != initial.world.last_event_sequence
                or current.world.state_hash != initial.world.state_hash
            ):
                raise AgentToolExecutionError(
                    "TOOL_WORLD_REVISION_CONFLICT",
                    "World or exact active Skill authority changed during Sandbox execution.",
                    {"commit_state": "ROLLED_BACK"},
                )
            return await self._materialize(session, request, context, current, sandbox_result)
    async def _materialize(
        self,
        session: AsyncSession,
        request: SkillInvocationRequest,
        context: OperationContext,
        authority: _InvocationAuthority,
        sandbox_result: Success[SandboxRunResult] | Failure,
    ) -> SkillInvocationResult:
        database_now = await _database_now(session)
        causal_times = [
            database_now,
            authority.command.updated_at,
            context.requested_at,
        ]
        if isinstance(sandbox_result, Success):
            causal_times.extend(
                (
                    sandbox_result.value.started_at,
                    sandbox_result.value.finished_at,
                )
            )
        # PostgreSQL, the host process, and Docker can have small clock skew.
        # Publish every durable Run/Command/Evidence/World timestamp from one
        # causal floor so a recovered Sandbox result can never move the
        # Command clock backwards.
        now = max(causal_times)
        run_id = _identifier("run", request.invocation_id)
        sandbox_value: SandboxRunResult | None = None
        sandbox_failure: ContractError | None = None
        world_failure: ContractError | None = None
        transition: WorldTransition | None = None
        if isinstance(sandbox_result, Success):
            candidate = sandbox_result.value
            if (
                candidate.run_id != run_id
                or candidate.stdout_ref is not None
                or candidate.stderr_ref is not None
                or candidate.evidence_refs
                or len(candidate.action_intents) > self._limits.max_intents
            ):
                sandbox_failure = _sandbox_protocol_error()
            else:
                sandbox_value = candidate
                rules = self._rules.get(authority.world.world_rules_version)
                if rules is None:
                    raise WorkflowInvariantError("World rules are not activated")
                try:
                    transition = self._world_engine.apply(
                        authority.world.state, candidate.action_intents, rules
                    )
                except WorldRuleViolation as error:
                    world_failure = _world_rejected(error.code)
        else:
            sandbox_failure = sandbox_result.error

        task_success = bool(transition is not None and transition.success)
        if transition is not None and not transition.success:
            world_failure = _world_rejected("TASK_INCOMPLETE")
        world_commit: WorldCommitReceipt | None = None
        world_reference: EvidenceRef | None = None
        if task_success:
            if transition is None or sandbox_value is None:
                raise AssertionError("successful transition requires Sandbox output")
            expected = WorldCommitReceipt(
                world_id=authority.world.world_id,
                previous_revision=authority.world.revision,
                world_revision=authority.world.revision + 1,
                first_event_sequence=authority.world.last_event_sequence + 1,
                last_event_sequence=authority.world.last_event_sequence + 1,
                committed_at=now,
                state_hash=transition.state_hash,
            )
            world_reference = EvidenceRef(
                evidence_id=_identifier("evidence_world", request.invocation_id),
                evidence_type=EvidenceType.WORLD_COMMIT,
                created_at=now,
                sha256=world_commit_receipt_sha256(expected),
            )
            world_event = UncommittedEvent(
                event_type="world.committed",
                event_version=1,
                producer="walnut_world_engine",
                trace_id=context.trace_id,
                command_id=context.command_id,
                correlation_id=context.correlation_id,
                causation_id=context.command_id,
                content_ref=context.content_ref,
                payload=cast(
                    FrozenJsonObject,
                    {
                        "commit_id": world_commit_identifier(
                            request.tenant_id,
                            f"world:{request.world_id}",
                            run_id,
                            authority.world.revision,
                        ),
                        "run_id": run_id,
                        "world_id": request.world_id,
                        "previous_world_revision": authority.world.revision,
                        "world_revision": authority.world.revision + 1,
                        "state_hash": transition.state_hash,
                        "applied_intent_ids": transition.applied_intent_ids,
                        "committed_at": _iso(now),
                        "evidence_refs": (_evidence_ref_wire(world_reference),),
                    },
                ),
            )
            committed = await self._world_uow.commit_in_session(
                session,
                WorldAtomicCommit(
                    stream_id=f"world:{request.world_id}",
                    expected_stream_sequence=(
                        "NO_STREAM"
                        if authority.world.last_event_sequence == 0
                        else authority.world.last_event_sequence
                    ),
                    command=WorldCommand(
                        run_id=run_id,
                        world_id=request.world_id,
                        expected_world_revision=authority.world.revision,
                        world_rules_version=authority.world.world_rules_version,
                        skill_ref=request.skill_ref,
                        intents=sandbox_value.action_intents,
                    ),
                    events=(world_event,),
                    outbox_messages=(),
                ),
                context,
            )
            if isinstance(committed, Failure):
                raise AgentToolExecutionError(
                    "TOOL_WORLD_REVISION_CONFLICT",
                    committed.error.message or "World commit failed.",
                    {"commit_state": "ROLLED_BACK", "code": committed.error.code},
                )
            world_commit = committed.value.world
            if world_commit != expected:
                raise WorkflowInvariantError("World receipt differs from staged transition")
            workspace_id = await session.scalar(
                select(ProductWorkspaceRow.workspace_id).where(
                    ProductWorkspaceRow.tenant_id == request.tenant_id,
                    ProductWorkspaceRow.actor_id == context.actor.actor_id,
                    ProductWorkspaceRow.session_id == request.session_id,
                )
            )
            if workspace_id is not None:
                # Keep the public Product checkpoint atomic with the
                # authoritative World commit.  Book context is built before
                # learner/Interaction projection, so a later refresh is too
                # late to close prior terminal history against the new head.
                # Internal UoW fixtures may omit the whole Product Session;
                # a real Session is required to have a strict workspace.
                await refresh_workspace_in_session(
                    session,
                    tenant_id=request.tenant_id,
                    actor_id=context.actor.actor_id,
                    session_id=request.session_id,
                    updated_at=world_commit.committed_at,
                )
            else:
                binding_id = await session.scalar(
                    select(CurrentSessionBindingRow.binding_id).where(
                        CurrentSessionBindingRow.tenant_id == request.tenant_id,
                        CurrentSessionBindingRow.actor_id == context.actor.actor_id,
                        CurrentSessionBindingRow.session_id == request.session_id,
                    )
                )
                if binding_id is not None:
                    raise WorkflowInvariantError(
                        "bound Session lost its Product workspace during World commit"
                    )
        return await self._persist_run(
            session=session,
            request=request,
            context=context,
            authority=authority,
            run_id=run_id,
            now=now,
            sandbox=sandbox_value,
            sandbox_failure=sandbox_failure,
            world_failure=world_failure,
            transition=transition,
            world_commit=world_commit,
            world_reference=world_reference,
        )

    async def _persist_run(
        self,
        *,
        session: AsyncSession,
        request: SkillInvocationRequest,
        context: OperationContext,
        authority: _InvocationAuthority,
        run_id: str,
        now: datetime,
        sandbox: SandboxRunResult | None,
        sandbox_failure: ContractError | None,
        world_failure: ContractError | None,
        transition: WorldTransition | None,
        world_commit: WorldCommitReceipt | None,
        world_reference: EvidenceRef | None,
    ) -> SkillInvocationResult:
        if sandbox is not None:
            sandbox_status = "SUCCEEDED"
            world_status = "COMMITTED" if world_commit is not None else "REJECTED"
            occurred_at = sandbox.finished_at
            intent_count = len(sandbox.action_intents)
        else:
            reason = (
                "" if sandbox_failure is None else str(sandbox_failure.details.get("reason", ""))
            )
            sandbox_status = "TIMED_OUT" if reason == "WALL_TIMEOUT" else "FAILED"
            world_status = "NOT_ATTEMPTED"
            occurred_at = now
            intent_count = 0
        run_payload: dict[str, object] = {
            "evidence_kind": "SKILL_RUN",
            "run_id": run_id,
            "sandbox_status": sandbox_status,
            "world_status": world_status,
            "intent_count": intent_count,
        }
        run_reference = EvidenceRef(
            evidence_id=_identifier("evidence_run", request.invocation_id),
            evidence_type=EvidenceType.SANDBOX_LOG,
            created_at=occurred_at,
            sha256=canonical_json_sha256(run_payload),
        )
        references = [run_reference]
        evidence_documents = [
            _evidence_document(
                context=context,
                reference=run_reference,
                source_type="SKILL_RUN",
                source_id=run_id,
                world_id=request.world_id,
                occurred_at=occurred_at,
                recorded_at=now,
                payload=run_payload,
                versions=self._versions_for(authority),
            )
        ]
        revision_after = authority.world.revision
        if world_commit is not None:
            if world_reference is None:
                raise AssertionError("World commit requires Evidence")
            revision_after = world_commit.world_revision
            world_payload: dict[str, object] = {
                "evidence_kind": "WORLD_COMMIT",
                "world_id": world_commit.world_id,
                "previous_revision": world_commit.previous_revision,
                "world_revision": world_commit.world_revision,
                "first_event_sequence": world_commit.first_event_sequence,
                "last_event_sequence": world_commit.last_event_sequence,
                "state_hash": world_commit.state_hash,
            }
            references.append(world_reference)
            evidence_documents.append(
                _evidence_document(
                    context=context,
                    reference=world_reference,
                    source_type="WORLD",
                    source_id=request.world_id,
                    world_id=request.world_id,
                    occurred_at=world_commit.committed_at,
                    recorded_at=now,
                    payload=world_payload,
                    versions=self._versions_for(authority),
                )
            )

        task_success = world_commit is not None
        if transition is None:
            world_difference: Mapping[str, object] = {
                "score": 0,
                "intent_count": 0,
                "applied_intent_ids": (),
            }
            failure_key = "sandbox_execution_failed"
        else:
            world_difference = {
                "score": transition.score,
                "intent_count": len(transition.applied_intent_ids),
                "applied_intent_ids": transition.applied_intent_ids,
            }
            failure_key = None if task_success else "task_incomplete"
        failed_actions: tuple[FrozenJsonObject, ...] = ()
        if not task_success:
            failed_actions = (cast(FrozenJsonObject, {"reason": failure_key or "task_incomplete"}),)
        run = RunResultSnapshot(
            run_id=run_id,
            session_id=request.session_id,
            turn_id=request.turn_id,
            command_id=request.command_id,
            world_id=request.world_id,
            skill_ref=request.skill_ref,
            task_success=task_success,
            world_revision_before=authority.world.revision,
            world_revision_after=revision_after,
            world_difference=cast(FrozenJsonObject, world_difference),
            failed_actions=failed_actions,
            failure_key=failure_key,
            evidence_refs=tuple(references),
            world_commit=world_commit,
            request_context=RequestContext(
                request_id=context.request_id,
                correlation_id=context.correlation_id,
                trace_id=context.trace_id,
                requested_at=context.requested_at,
                actor=context.actor,
                content_ref=context.content_ref,
                schema_version=context.schema_version,
            ),
        )
        result = SkillInvocationResult(
            invocation_id=request.invocation_id,
            tenant_id=request.tenant_id,
            request_sha256=request.request_sha256,
            arguments=request.arguments,
            run=run,
        )
        versions = self._versions_for(authority)
        # A controlled Sandbox can report a host timestamp slightly behind the
        # PostgreSQL command clock.  The Run is caused by the accepted Command,
        # so its durable creation time may never precede that authority.  Keep
        # the original Sandbox and Evidence timestamps in their own fields.
        run_created_at = (
            max(sandbox.started_at, authority.command.accepted_at)
            if sandbox is not None
            else now
        )
        run_wire = _run_wire(
            request=request,
            context=context,
            run=run,
            sandbox=sandbox,
            sandbox_failure=sandbox_failure,
            world_failure=world_failure,
            evidence_refs=references,
            versions=versions,
            limits=self._limits,
            created_at=run_created_at,
            now=now,
        )
        for reference, document in zip(references, evidence_documents, strict=True):
            session.add(
                EvidenceRow(
                    evidence_id=reference.evidence_id,
                    tenant_id=request.tenant_id,
                    actor_id=context.actor.actor_id,
                    content_hash=context.content_ref.content_hash,
                    command_id=request.command_id,
                    recorded_at=now,
                    evidence_json=document,
                )
            )
        session.add(
            RunRow(
                run_id=run_id,
                tenant_id=request.tenant_id,
                actor_id=context.actor.actor_id,
                content_hash=context.content_ref.content_hash,
                session_id=request.session_id,
                turn_id=request.turn_id,
                command_id=request.command_id,
                created_at=run_created_at,
                run_json=run_wire,
            )
        )
        build_provenance = await _require_int2_build_provenance(
            session,
            authority,
            request,
            required=True,
        )
        if build_provenance is None:
            raise WorkflowInvariantError("Skill Run Build provenance disappeared")
        activation_provenance = await session.scalar(
            select(SkillActivationProvenanceRow).where(
                SkillActivationProvenanceRow.activation_id
                == authority.activation.activation_id,
                SkillActivationProvenanceRow.tenant_id == request.tenant_id,
                SkillActivationProvenanceRow.actor_id == context.actor.actor_id,
            )
        )
        if (
            activation_provenance is None
            or activation_provenance.authority_sha256
            != activation_provenance_sha256(activation_provenance)
            or activation_provenance.build_id != build_provenance.build_id
            or activation_provenance.build_authority_sha256
            != build_provenance.authority_sha256
        ):
            raise WorkflowInvariantError("Skill Run Activation provenance disappeared")
        run_provenance = SkillRunProvenanceRow(
                    run_id=run_id,
                    build_id=build_provenance.build_id,
                    provenance_kind=_run_provenance_kind(
                        build_provenance.provenance_kind
                    ),
                    build_authority_sha256=build_provenance.authority_sha256,
                    tenant_id=request.tenant_id,
                    actor_id=context.actor.actor_id,
                    session_id=request.session_id,
                    activation_id=authority.activation.activation_id,
                    activation_sha256=authority.activation.activation_sha256,
                    activation_authority_sha256=activation_provenance.authority_sha256,
                    registry_revision=authority.activation.registry_revision,
                    certification_id=authority.activation.certification_id,
                    certification_sha256=activation_provenance.certification_sha256,
                    certification_authority_sha256=(
                        activation_provenance.certification_authority_sha256
                    ),
                    artifact_sha256=authority.activation.artifact_sha256,
                    artifact_authority_sha256=(
                        activation_provenance.artifact_authority_sha256
                    ),
                    draft_revision_row_id=build_provenance.draft_revision_row_id,
                    draft_sha256=build_provenance.draft_sha256,
                    assistance_authority=build_provenance.assistance_authority,
                    authority_sha256="0" * 64,
                    created_at=run_created_at,
                )
        run_provenance.authority_sha256 = run_provenance_sha256(run_provenance)
        session.add(run_provenance)
        if task_success:
            command = await _command(session, self._claim, for_update=True)
            if command.status is CommandStatus.RUNNING_SANDBOX:
                applying = replace(
                    command,
                    status=CommandStatus.APPLYING_WORLD,
                    stage="WORLD_COMMIT",
                    revision=command.revision + 1,
                    updated_at=now,
                )
                changed = await self._commands.transition_in_session(
                    session, CommandTransition(command, applying), context
                )
                if isinstance(changed, Failure):
                    raise WorkflowFenceLost("Turn Command World CAS was lost")
        receipt_data = _invocation_result_data(result)
        await self._jobs.record_step_in_session(
            session,
            self._claim,
            step_name="SKILL_INVOKED",
            input_sha256=request.request_sha256,
            output=receipt_data,
        )
        return result

    def _versions_for(self, authority: _InvocationAuthority) -> VersionSet:
        return replace(
            self._versions,
            policy_version=authority.policy.build_policy_id,
            world_rules_version=authority.world.world_rules_version,
            skill_version=authority.activation.skill_version_id,
            artifact_sha256=authority.activation.artifact_sha256,
            compiler_version=authority.policy.compiler_version,
            sandbox_image_digest=authority.policy.sandbox_image_digest,
            test_suite_version=authority.policy.test_suite_version,
        )


def _run_provenance_kind(build_provenance_kind: str) -> str:
    """Distinguish migrated v0.4 Runs from new Runs on a sealed v0.4 Build."""

    if build_provenance_kind == "LEGACY_V04":
        return "LEGACY_V04_ACTIVE"
    if build_provenance_kind == "IMMUTABLE_DRAFT":
        return build_provenance_kind
    raise WorkflowInvariantError("Skill Build provenance kind is unsupported")


async def _require_int2_build_provenance(
    session: AsyncSession,
    authority: _InvocationAuthority,
    request: SkillInvocationRequest,
    *,
    required: bool,
) -> SkillBuildProvenanceRow | None:
    provenance = await session.scalar(
        select(SkillBuildProvenanceRow).where(
            SkillBuildProvenanceRow.build_id == authority.artifact.build_id,
            SkillBuildProvenanceRow.tenant_id == request.tenant_id,
            SkillBuildProvenanceRow.actor_id == authority.artifact.actor_id,
            SkillBuildProvenanceRow.skill_id == request.skill_ref.skill_id,
            SkillBuildProvenanceRow.source_bundle_sha256
            == authority.artifact.source_sha256,
        )
    )
    if provenance is None or not await validate_build_provenance(session, provenance):
        if not required:
            return None
        raise WorkflowInvariantError(
            "active Skill Build has no exact Draft or legacy provenance"
        )
    if (
        provenance.provenance_kind == "IMMUTABLE_DRAFT"
        and provenance.session_id != request.session_id
    ):
        raise WorkflowInvariantError("active Skill Build Draft belongs to another Session")
    if not await active_build_matches_current_patch_origin(
        session,
        provenance,
        tenant_id=request.tenant_id,
        actor_id=authority.artifact.actor_id,
        session_id=request.session_id,
        skill_id=request.skill_ref.skill_id,
    ):
        raise WorkflowInvariantError(
            "active Skill Build predates the current accepted Patch origin"
        )
    return provenance


async def _load_authority(
    session: AsyncSession,
    request: SkillInvocationRequest,
    context: OperationContext,
    claim: ClaimedWorkflowJob,
    *,
    for_update: bool,
) -> _InvocationAuthority:
    command = await _command(session, claim, for_update=for_update)
    if command.command_type != "EXECUTE_AGENT_TURN" or command.terminal:
        raise WorkflowInvariantError("Turn Command is not executable")
    if (
        command.request_context.actor != context.actor
        or command.request_context.content_ref != context.content_ref
    ):
        raise WorkflowInvariantError("Turn Command authority differs from runtime context")
    if (
        command.versions.skill_version != request.skill_ref.skill_version_id
        or command.versions.artifact_sha256 != request.skill_ref.artifact_sha256
    ):
        raise WorkflowInvariantError("Turn Command is not pinned to the requested Skill")

    turn = await _scalar(
        session,
        select(AgentTurnRow).where(
            AgentTurnRow.tenant_id == request.tenant_id,
            AgentTurnRow.actor_id == context.actor.actor_id,
            AgentTurnRow.session_id == request.session_id,
            AgentTurnRow.turn_id == request.turn_id,
            AgentTurnRow.command_id == request.command_id,
        ),
        for_update=for_update,
    )
    if turn is None or claim.subject_id != request.turn_id:
        raise AgentToolExecutionError(
            "TOOL_INVOCATION_IDENTITY_MISMATCH", "Accepted Turn authority is missing."
        )
    turn_request = _object(turn.request_json, "Turn request")
    bindings = turn_request.get("skill_bindings")
    if not isinstance(bindings, list) or len(bindings) != 1 or not isinstance(bindings[0], Mapping):
        raise WorkflowInvariantError("Turn has no unique Skill binding")
    if dict(bindings[0]) != _skill_ref_wire(request.skill_ref):
        raise WorkflowInvariantError("Turn Skill binding differs from invocation")
    if turn_request.get("expected_world_revision") != request.expected_world_revision:
        raise WorkflowInvariantError("Turn World revision differs from invocation")

    owner = await _scalar(
        session,
        select(AgentSessionRow).where(
            AgentSessionRow.tenant_id == request.tenant_id,
            AgentSessionRow.actor_id == context.actor.actor_id,
            AgentSessionRow.session_id == request.session_id,
            AgentSessionRow.world_id == request.world_id,
            AgentSessionRow.status == "ACTIVE",
        ),
        for_update=for_update,
    )
    if owner is None:
        raise AgentToolExecutionError(
            "TOOL_INVOCATION_IDENTITY_MISMATCH", "Active Session authority is missing."
        )
    binding = await _scalar(
        session,
        select(CurrentSessionBindingRow).where(
            CurrentSessionBindingRow.tenant_id == request.tenant_id,
            CurrentSessionBindingRow.actor_id == context.actor.actor_id,
            CurrentSessionBindingRow.content_hash == context.content_ref.content_hash,
            CurrentSessionBindingRow.session_id == request.session_id,
            CurrentSessionBindingRow.world_id == request.world_id,
        ),
        for_update=for_update,
    )
    if binding is None:
        raise AgentToolExecutionError(
            "TOOL_SKILL_BINDING_MISMATCH", "Current Session binding is missing."
        )
    launch = await _scalar(
        session,
        select(LaunchAuthorityRow).where(
            LaunchAuthorityRow.tenant_id == request.tenant_id,
            LaunchAuthorityRow.authority_id == binding.authority_id,
            LaunchAuthorityRow.actor_id == binding.actor_id,
            LaunchAuthorityRow.content_hash == binding.content_hash,
            LaunchAuthorityRow.world_id == binding.world_id,
            LaunchAuthorityRow.agent_profile_id == binding.agent_profile_id,
            LaunchAuthorityRow.active.is_(True),
        ),
        for_update=for_update,
    )
    if launch is None:
        raise AgentToolExecutionError(
            "TOOL_SKILL_BINDING_MISMATCH", "Launch authority is no longer active."
        )
    try:
        active = await load_current_activation_authority(
            session,
            tenant_id=request.tenant_id,
            actor_id=binding.actor_id,
            content_hash=binding.content_hash,
            world_id=binding.world_id,
            agent_profile_id=binding.agent_profile_id,
            authority_id=binding.authority_id,
            skill_ref=request.skill_ref,
            for_update=for_update,
        )
    except ActivationAuthorityNotFound as error:
        raise AgentToolExecutionError(
            "TOOL_SKILL_BINDING_MISMATCH", "Registry has no exact active Skill."
        ) from error
    activation = active.activation
    revoked = await session.scalar(
        select(
            exists().where(
                SkillCertificationRevocationRow.tenant_id == request.tenant_id,
                SkillCertificationRevocationRow.certification_id
                == request.skill_ref.certification_id,
            )
        )
    )
    if revoked is True:
        raise AgentToolExecutionError(
            "TOOL_SKILL_BINDING_MISMATCH", "Exact certification is inactive or revoked."
        )
    certification = await _scalar(
        session,
        select(SkillCertificationRow).where(
            SkillCertificationRow.tenant_id == request.tenant_id,
            SkillCertificationRow.actor_id == context.actor.actor_id,
            SkillCertificationRow.content_hash == context.content_ref.content_hash,
            SkillCertificationRow.skill_id == request.skill_ref.skill_id,
            SkillCertificationRow.skill_version_id == request.skill_ref.skill_version_id,
            SkillCertificationRow.certification_id == request.skill_ref.certification_id,
            SkillCertificationRow.artifact_sha256 == request.skill_ref.artifact_sha256,
        ),
        for_update=for_update,
    )
    if certification is None:
        raise WorkflowInvariantError("Exact Skill certification disappeared")
    artifact = await _scalar(
        session,
        select(SkillArtifactRow).where(
            SkillArtifactRow.tenant_id == request.tenant_id,
            SkillArtifactRow.actor_id == context.actor.actor_id,
            SkillArtifactRow.content_hash == context.content_ref.content_hash,
            SkillArtifactRow.build_id == certification.build_id,
            SkillArtifactRow.artifact_sha256 == request.skill_ref.artifact_sha256,
        ),
        for_update=for_update,
    )
    if artifact is None:
        raise WorkflowInvariantError("Certified Skill Artifact disappeared")
    policy = await _scalar(
        session,
        select(BuildPolicyRow).where(
            BuildPolicyRow.tenant_id == request.tenant_id,
            BuildPolicyRow.build_policy_id == launch.build_policy_id,
            BuildPolicyRow.actor_id == context.actor.actor_id,
            BuildPolicyRow.content_hash == context.content_ref.content_hash,
            BuildPolicyRow.active.is_(True),
        ),
        for_update=for_update,
    )
    if policy is None:
        raise WorkflowInvariantError("Pinned Build policy disappeared")
    world_row = await _scalar(
        session,
        select(WorldSnapshotRow).where(
            WorldSnapshotRow.tenant_id == request.tenant_id,
            WorldSnapshotRow.actor_id == context.actor.actor_id,
            WorldSnapshotRow.content_hash == context.content_ref.content_hash,
            WorldSnapshotRow.world_id == request.world_id,
        ),
        for_update=for_update,
    )
    if world_row is None:
        raise AgentToolExecutionError("TOOL_WORLD_NOT_FOUND", "World was not found.")
    world = world_snapshot_from_data(world_row.snapshot_json)
    if (
        world.revision != request.expected_world_revision
        or world_row.revision != world.revision
        or world_row.last_event_sequence != world.last_event_sequence
        or world_row.state_hash != world.state_hash
        or world.request_context.actor != context.actor
        or world.request_context.content_ref != context.content_ref
        or command.versions.world_rules_version != world.world_rules_version
    ):
        raise AgentToolExecutionError(
            "TOOL_WORLD_REVISION_CONFLICT", "World authority differs from the accepted Turn."
        )
    return _InvocationAuthority(command, world, activation, artifact, policy)


async def _scalar(session: AsyncSession, statement: Any, *, for_update: bool) -> Any | None:
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def _command(
    session: AsyncSession, claim: ClaimedWorkflowJob, *, for_update: bool
) -> CommandRecord:
    statement = select(CommandRow).where(
        CommandRow.tenant_id == claim.tenant_id,
        CommandRow.command_id == claim.command_id,
    )
    if for_update:
        statement = statement.with_for_update()
    row = await session.scalar(statement)
    if row is None:
        raise WorkflowInvariantError("Turn Command disappeared")
    return command_record_from_data(row.record_json)


async def _advance_to_sandbox(
    session: AsyncSession,
    command: CommandRecord,
    context: OperationContext,
    commands: PostgresCommandStore,
    *,
    run_id: str,
) -> CommandRecord:
    now = await _database_now(session)
    current = command
    if current.status is CommandStatus.ACCEPTED:
        validating = replace(
            current,
            status=CommandStatus.VALIDATING,
            stage="REGISTRY",
            revision=current.revision + 1,
            updated_at=now,
        )
        changed = await commands.transition_in_session(
            session, CommandTransition(current, validating), context
        )
        if isinstance(changed, Failure):
            raise WorkflowFenceLost("Turn Command validation CAS was lost")
        current = changed.value
    if current.status is CommandStatus.VALIDATING:
        running = replace(
            current,
            status=CommandStatus.RUNNING_SANDBOX,
            stage="SANDBOX",
            links=cast(
                FrozenJsonObject,
                {**dict(current.links), "run": f"/v1/runs/{run_id}"},
            ),
            revision=current.revision + 1,
            updated_at=now,
        )
        changed = await commands.transition_in_session(
            session, CommandTransition(current, running), context
        )
        if isinstance(changed, Failure):
            raise WorkflowFenceLost("Turn Command Sandbox CAS was lost")
        current = changed.value
    if current.status not in {CommandStatus.RUNNING_SANDBOX, CommandStatus.APPLYING_WORLD}:
        raise WorkflowInvariantError("Turn Command is outside the recoverable execution state")
    if current.links.get("run") != f"/v1/runs/{run_id}":
        raise WorkflowInvariantError("Turn Command Run link differs from its invocation")
    return current


async def _step(
    session: AsyncSession, claim: ClaimedWorkflowJob, name: str
) -> JobStepReceiptRow | None:
    return await session.scalar(
        select(JobStepReceiptRow).where(
            JobStepReceiptRow.tenant_id == claim.tenant_id,
            JobStepReceiptRow.job_id == claim.job_id,
            JobStepReceiptRow.step_name == name,
        )
    )


def _sandbox_request(
    request: SkillInvocationRequest,
    world: WorldSnapshot,
    run_id: str,
    limits: SandboxLimits,
) -> SandboxRunRequest:
    return SandboxRunRequest(
        run_id=run_id,
        skill_ref=request.skill_ref,
        world_id=request.world_id,
        world_snapshot=world,
        input=cast(FrozenJsonObject, request.arguments),
        deterministic_seed=request.invocation_id,
        limits=limits,
    )


def _validate_dispatch_receipt(
    receipt: JobStepReceiptRow,
    request: SkillInvocationRequest,
    run_id: str,
) -> None:
    expected = {
        "schema_version": "1.0.0",
        "invocation_id": request.invocation_id,
        "run_id": run_id,
        "request_sha256": request.request_sha256,
        "arguments": json_value(request.arguments),
        "skill": _skill_ref_wire(request.skill_ref),
        "world_id": request.world_id,
        "expected_world_revision": request.expected_world_revision,
    }
    if receipt.input_sha256 != request.request_sha256 or receipt.receipt_json != expected:
        raise WorkflowInvariantError("Sandbox dispatch receipt differs from invocation bytes")


def _validate_request_identity(
    request: SkillInvocationRequest,
    context: OperationContext,
    claim: ClaimedWorkflowJob,
) -> None:
    if (
        request.tenant_id != claim.tenant_id
        or request.tenant_id != context.actor.tenant_id
        or request.command_id != claim.command_id
        or request.command_id != context.command_id
        or request.turn_id != claim.subject_id
        or request.invocation_id != side_effect_execution_id(request.command_id, request.turn_id)
    ):
        raise AgentToolExecutionError(
            "TOOL_INVOCATION_IDENTITY_MISMATCH",
            "Invocation differs from the fenced Agent Turn.",
        )


def _validate_result_identity(
    result: SkillInvocationResult,
    invocation_id: str,
    context: OperationContext,
    claim: ClaimedWorkflowJob,
) -> None:
    if (
        result.invocation_id != invocation_id
        or result.tenant_id != claim.tenant_id
        or result.run.command_id != claim.command_id
        or result.run.turn_id != claim.subject_id
        or result.run.request_context.actor != context.actor
        or result.run.request_context.content_ref != context.content_ref
    ):
        raise WorkflowInvariantError("Invocation receipt authority drifted")


def _validate_result_request(
    result: SkillInvocationResult,
    request: SkillInvocationRequest,
    context: OperationContext,
) -> None:
    if (
        result.invocation_id != request.invocation_id
        or result.tenant_id != request.tenant_id
        or result.request_sha256 != request.request_sha256
        or result.arguments != request.arguments
        or result.run.session_id != request.session_id
        or result.run.turn_id != request.turn_id
        or result.run.command_id != request.command_id
        or result.run.world_id != request.world_id
        or result.run.world_revision_before != request.expected_world_revision
        or result.run.skill_ref != request.skill_ref
        or result.run.request_context.actor != context.actor
        or result.run.request_context.content_ref != context.content_ref
    ):
        raise AgentToolExecutionError(
            "TOOL_IDEMPOTENCY_KEY_REUSED",
            "Invocation receipt differs from the request.",
        )


def _invocation_result_data(result: SkillInvocationResult) -> dict[str, Any]:
    run = result.run
    return {
        "schema_version": "1.0.0",
        "invocation_id": result.invocation_id,
        "tenant_id": result.tenant_id,
        "request_sha256": result.request_sha256,
        "arguments": json_value(result.arguments),
        "run": {
            "run_id": run.run_id,
            "session_id": run.session_id,
            "turn_id": run.turn_id,
            "command_id": run.command_id,
            "world_id": run.world_id,
            "skill_ref": _skill_ref_wire(run.skill_ref),
            "task_success": run.task_success,
            "world_revision_before": run.world_revision_before,
            "world_revision_after": run.world_revision_after,
            "world_difference": json_value(run.world_difference),
            "failed_actions": json_value(run.failed_actions),
            "failure_key": run.failure_key,
            "evidence_refs": [_evidence_ref_wire(item) for item in run.evidence_refs],
            "world_commit": _world_receipt_wire(run.world_commit),
            "request_context": request_context_data(run.request_context),
        },
    }


def _invocation_result_from_data(value: Mapping[str, Any]) -> SkillInvocationResult:
    if value.get("schema_version") != "1.0.0":
        raise WorkflowInvariantError("Invocation receipt schema is unsupported")
    run_data = _object(value.get("run"), "invocation run")
    world_data = run_data.get("world_commit")
    world_commit = None
    if world_data is not None:
        world = _object(world_data, "World receipt")
        world_commit = WorldCommitReceipt(
            world_id=_text(world, "world_id"),
            previous_revision=_integer(world, "previous_revision"),
            world_revision=_integer(world, "world_revision"),
            first_event_sequence=_integer(world, "first_event_sequence"),
            last_event_sequence=_integer(world, "last_event_sequence"),
            committed_at=_datetime(_text(world, "committed_at")),
            state_hash=_text(world, "state_hash"),
        )
    run = RunResultSnapshot(
        run_id=_text(run_data, "run_id"),
        session_id=_text(run_data, "session_id"),
        turn_id=_text(run_data, "turn_id"),
        command_id=_text(run_data, "command_id"),
        world_id=_text(run_data, "world_id"),
        skill_ref=SkillRef(**_object(run_data.get("skill_ref"), "SkillRef")),
        task_success=_boolean(run_data, "task_success"),
        world_revision_before=_integer(run_data, "world_revision_before"),
        world_revision_after=_integer(run_data, "world_revision_after"),
        world_difference=_object(run_data.get("world_difference"), "world_difference"),
        failed_actions=tuple(cast(Sequence[Mapping[str, Any]], run_data.get("failed_actions", []))),
        failure_key=cast(str | None, run_data.get("failure_key")),
        evidence_refs=tuple(
            _evidence_ref_from_wire(_object(item, "EvidenceRef"))
            for item in cast(Sequence[object], run_data.get("evidence_refs", []))
        ),
        world_commit=world_commit,
        request_context=request_context_from_data(
            _object(run_data.get("request_context"), "request_context")
        ),
    )
    return SkillInvocationResult(
        invocation_id=_text(value, "invocation_id"),
        tenant_id=_text(value, "tenant_id"),
        request_sha256=_text(value, "request_sha256"),
        arguments=_object(value.get("arguments"), "arguments"),
        run=run,
    )


def invocation_result_from_receipt(value: Mapping[str, Any]) -> SkillInvocationResult:
    """Decode one immutable ``SKILL_INVOKED`` receipt for sibling adapters.

    Keeping this decoder with the writer prevents the Run outcome/context
    readers from growing a second, subtly different receipt codec.
    """

    return _invocation_result_from_data(value)


def invocation_result_receipt_data(result: SkillInvocationResult) -> dict[str, Any]:
    """Encode one exact writer-format ``SKILL_INVOKED`` receipt for sibling validators."""

    return _invocation_result_data(result)


def _run_wire(
    *,
    request: SkillInvocationRequest,
    context: OperationContext,
    run: RunResultSnapshot,
    sandbox: SandboxRunResult | None,
    sandbox_failure: ContractError | None,
    world_failure: ContractError | None,
    evidence_refs: Sequence[EvidenceRef],
    versions: VersionSet,
    limits: SandboxLimits,
    created_at: datetime,
    now: datetime,
) -> dict[str, Any]:
    if sandbox is None:
        if sandbox_failure is None:
            raise AssertionError("missing Sandbox result and failure")
        reason = str(sandbox_failure.details.get("reason", ""))
        sandbox_status = "TIMED_OUT" if reason == "WALL_TIMEOUT" else "FAILED"
        sandbox_wire: dict[str, Any] = {
            "invocation_id": request.invocation_id,
            "status": sandbox_status,
            "started_at": None,
            "finished_at": _iso(now),
            "limits": _limits_wire(limits),
            "usage": None,
            "action_intents": [],
            "failure": _error_wire(sandbox_failure),
        }
        status = "FAILED"
        world_wire = {"status": "NOT_ATTEMPTED", "receipt": None, "failure": None}
    else:
        sandbox_wire = {
            "invocation_id": request.invocation_id,
            "status": "SUCCEEDED",
            "started_at": _iso(sandbox.started_at),
            "finished_at": _iso(sandbox.finished_at),
            "limits": _limits_wire(limits),
            "usage": {
                "cpu_ms": sandbox.usage.cpu_ms,
                "wall_ms": sandbox.usage.wall_ms,
                "peak_memory_bytes": sandbox.usage.peak_memory_bytes,
            },
            "action_intents": [_action_intent_wire(item) for item in sandbox.action_intents],
            "failure": None,
        }
        if run.task_success:
            if run.world_commit is None:
                raise AssertionError("successful Run requires World receipt")
            status = "SUCCEEDED"
            world_wire = {
                "status": "COMMITTED",
                "receipt": _world_receipt_wire(run.world_commit),
                "failure": None,
            }
        else:
            status = "REJECTED"
            world_wire = {
                "status": "REJECTED",
                "receipt": None,
                "failure": _error_wire(world_failure or _world_rejected("TASK_INCOMPLETE")),
            }
    return {
        "request_context": request_context_data(context),
        "run_id": run.run_id,
        "session_id": run.session_id,
        "turn_id": run.turn_id,
        "command_id": run.command_id,
        "status": status,
        "terminal": True,
        "skill": _skill_ref_wire(run.skill_ref),
        "sandbox": sandbox_wire,
        "world_application": world_wire,
        "agent_feedback": None,
        "created_at": _iso(created_at),
        "updated_at": _iso(now),
        "evidence_refs": [_evidence_ref_wire(item) for item in evidence_refs],
        "versions": _versions_wire(versions),
    }


def _evidence_document(
    *,
    context: OperationContext,
    reference: EvidenceRef,
    source_type: str,
    source_id: str,
    world_id: str,
    occurred_at: datetime,
    recorded_at: datetime,
    payload: Mapping[str, object],
    versions: VersionSet,
) -> dict[str, Any]:
    payload_hash = canonical_json_sha256(payload)
    if reference.sha256 != payload_hash:
        raise WorkflowInvariantError("EvidenceRef hash differs from canonical payload")
    return {
        "request_context": request_context_data(context),
        "evidence_ref": _evidence_ref_wire(reference),
        "subject": {"learner_id": context.actor.actor_id},
        "source": {
            "source_type": source_type,
            "source_id": source_id,
            "command_id": context.command_id,
            "world_id": world_id,
        },
        "occurred_at": _iso(occurred_at),
        "recorded_at": _iso(recorded_at),
        "integrity": {
            "payload_sha256": payload_hash,
            "previous_evidence_sha256": None,
        },
        "payload": dict(payload),
        "related_evidence": [],
        "versions": _versions_wire(versions),
    }


def _skill_ref_wire(reference: SkillRef) -> dict[str, str]:
    return {
        "skill_id": reference.skill_id,
        "skill_version_id": reference.skill_version_id,
        "artifact_sha256": reference.artifact_sha256,
        "certification_id": reference.certification_id,
    }


def _action_intent_wire(intent: object) -> dict[str, object]:
    value = json_value(intent)
    action_type = getattr(intent, "action_type", None)
    if not isinstance(value, dict) or not isinstance(action_type, str):
        raise WorkflowInvariantError("ActionIntent did not serialize as an object")
    return {**value, "action_type": action_type}


def _evidence_ref_wire(reference: EvidenceRef) -> dict[str, object]:
    value: dict[str, object] = {
        "evidence_id": reference.evidence_id,
        "evidence_type": reference.evidence_type.value,
        "created_at": _iso(reference.created_at),
    }
    if reference.sha256 is not None:
        value["sha256"] = reference.sha256
    if reference.uri is not None:
        value["uri"] = reference.uri
    return value


def _evidence_ref_from_wire(value: Mapping[str, Any]) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=_text(value, "evidence_id"),
        evidence_type=EvidenceType(_text(value, "evidence_type")),
        created_at=_datetime(_text(value, "created_at")),
        sha256=cast(str | None, value.get("sha256")),
        uri=cast(str | None, value.get("uri")),
    )


def _world_receipt_wire(receipt: WorldCommitReceipt | None) -> dict[str, object] | None:
    if receipt is None:
        return None
    return {
        "world_id": receipt.world_id,
        "previous_revision": receipt.previous_revision,
        "world_revision": receipt.world_revision,
        "first_event_sequence": receipt.first_event_sequence,
        "last_event_sequence": receipt.last_event_sequence,
        "state_hash": receipt.state_hash,
        "committed_at": _iso(receipt.committed_at),
    }


def _versions_wire(versions: VersionSet) -> dict[str, object]:
    value = json_value(versions)
    if not isinstance(value, dict):
        raise WorkflowInvariantError("VersionSet did not serialize as an object")
    return {key: item for key, item in value.items() if item is not None}


def _limits_wire(limits: SandboxLimits) -> dict[str, int]:
    return {
        "cpu_ms": limits.cpu_ms,
        "wall_ms": limits.wall_ms,
        "memory_bytes": limits.memory_bytes,
        "max_intents": limits.max_intents,
    }


def _error_wire(error: ContractError) -> dict[str, object]:
    value = error_data(error)
    if value is None:
        raise AssertionError("ContractError cannot serialize to null")
    return value


def _world_rejected(reason: str) -> ContractError:
    return ContractError(
        code="WORLD_RULE_REJECTED",
        category=ErrorCategory.WORLD_RULE,
        retryable=False,
        user_message_key="world.rule_rejected",
        stage="WORLD_VALIDATE",
        message="The staged actions did not satisfy the activated World rules.",
        details={"reason": reason},
    )


def _sandbox_protocol_error() -> ContractError:
    return ContractError(
        code="SANDBOX_RUNTIME_ERROR",
        category=ErrorCategory.SANDBOX,
        retryable=False,
        user_message_key="sandbox.runtime_error",
        stage="SANDBOX",
        message="Sandbox result violated its authority boundary.",
        details={"reason": "SANDBOX_PROTOCOL_MISMATCH"},
    )


def _identifier(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise WorkflowInvariantError("timestamp is not timezone-aware")
    return parsed.astimezone(UTC)


async def _database_now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WorkflowInvariantError("PostgreSQL returned an invalid timestamp")
    return value.astimezone(UTC)


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


def _boolean(value: Mapping[str, Any], key: str) -> bool:
    item = value.get(key)
    if not isinstance(item, bool):
        raise WorkflowInvariantError(f"{key} must be a boolean")
    return item


__all__ = [
    "PostgresFencedSkillInvocation",
    "invocation_result_from_receipt",
    "invocation_result_receipt_data",
]
