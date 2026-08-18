"""Provider-backed Agent Turn workflow with fenced Sandbox/World closure."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    ActorType,
    CommandRecord,
    CommandStatus,
    CommandTransition,
    Failure,
    LlmPort,
    OperationContext,
    SandboxLimits,
    SkillRef,
    VersionSet,
    canonical_json_sha256,
)
from yaya_agent_runtime import (
    AgentDecision,
    ContextBuilder,
    GameEvent,
    PackagedRoleConfigProvider,
    PromptBuilder,
    RecoverableLlmPort,
    RoleRouter,
    RuntimeBoundaryError,
    SharedAgentRuntime,
    SkillInvocationResult,
    build_default_tool_registry,
    side_effect_execution_id,
)
from yaya_agent_runtime.evidence import collect_decision_evidence
from yaya_agent_runtime.tool_registry import ToolRegistry
from yaya_agent_sandbox import RecoverableSandboxPort

from walnut_backend.adapters.postgres.activation_authority import (
    load_current_activation_authority,
)
from walnut_backend.adapters.postgres.agent_runtime import (
    PostgresAgentRuntimeReads,
    PostgresAgentTrace,
)
from walnut_backend.adapters.postgres.command_store import PostgresCommandStore
from walnut_backend.adapters.postgres.durable_llm import PostgresDurableLlm
from walnut_backend.adapters.postgres.models import (
    AgentProfileRow,
    AgentSessionRow,
    AgentTurnRow,
    CommandRow,
    CurrentSessionBindingRow,
    EvidenceRow,
    LaunchAuthorityRow,
    ProductContentUnitRow,
    ProductDraftRevisionRow,
    ProductDraftRow,
    ProductInteractionRow,
    ProductSkillPatchProposalRow,
    ProductSkillPatchRequestRow,
    RunRow,
    SkillBuildProvenanceRow,
    SkillBuildRow,
    SkillRunProvenanceRow,
    command_record_from_data,
    json_value,
)
from walnut_backend.adapters.postgres.product_interactions import (
    _run_interactions_have_authority,
)
from walnut_backend.adapters.postgres.run_outcomes import (
    PostgresRunOutcomeAuthority,
    evidence_ref_wire,
)
from walnut_backend.adapters.postgres.session_binding_authority import (
    current_session_binding_matches,
    current_session_binding_observed_at,
)
from walnut_backend.adapters.postgres.skill_invocation import (
    PostgresFencedSkillInvocation,
)
from walnut_backend.adapters.postgres.skill_provenance import validate_run_provenance
from walnut_backend.adapters.postgres.workflow_jobs import (
    ClaimedWorkflowJob,
    PostgresWorkflowJobStore,
    WorkflowBoundaryError,
    WorkflowInvariantError,
)
from walnut_backend.adapters.postgres.world import (
    PostgresWorld,
    PostgresWorldUnitOfWork,
)
from walnut_backend.domain.world.engine import WorldEngine
from walnut_backend.domain.world.rules import WorldRules


@dataclass(frozen=True, slots=True)
class _TurnAuthority:
    claim: ClaimedWorkflowJob
    command: CommandRecord
    context: OperationContext
    event: GameEvent
    task: Mapping[str, Any]
    learner_id: str


class _NoSkillInvocation:
    """A SkillInvocationPort that cannot execute: a hint never runs the Skill.

    ``invoke_skill`` is authorized for xiaohutao alone, so a teaching role can
    never reach this port through the registry.  Refusing here makes that a
    structural property of the hint runtime rather than an incidental one.
    """

    async def invoke(self, request: Any, context: OperationContext) -> Any:
        del request, context
        raise WorkflowInvariantError("a hint Turn cannot invoke the Skill")

    async def get_result(self, invocation_id: str, context: OperationContext) -> None:
        del invocation_id, context
        raise WorkflowInvariantError("a hint Turn has no Skill invocation")


def _final_runtime_boundary(error: RuntimeBoundaryError) -> WorkflowBoundaryError:
    """Map a redacted Agent substage into the durable workflow namespace."""

    return WorkflowBoundaryError(f"FINAL_RUNTIME_{error.stage.value}")


class TurnWorkflowHandler:
    """Execute one exact Agent Turn using only public Agent runtime libraries."""

    operations = frozenset({"EXECUTE_AGENT_TURN"})

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        commands: PostgresCommandStore,
        jobs: PostgresWorkflowJobStore,
        provider: RecoverableLlmPort,
        sandbox: RecoverableSandboxPort,
        limits: SandboxLimits,
        versions: VersionSet,
        rules_by_version: Mapping[str, WorldRules],
        provider_name: str,
        model_version: str,
        prompt_version: str,
        sandbox_image_digest: str,
        skill_patch_enabled: bool = False,
        lease_seconds: int = 180,
    ) -> None:
        if not 30 <= lease_seconds <= 3600:
            raise ValueError("Turn lease must be between 30 and 3600 seconds")
        if not isinstance(provider, RecoverableLlmPort):
            raise TypeError("production Turn provider must implement RecoverableLlmPort")
        if not isinstance(skill_patch_enabled, bool):
            raise TypeError("skill_patch_enabled must be a boolean")
        if any(
            not isinstance(value, str) or not value
            for value in (provider_name, model_version, prompt_version)
        ):
            raise ValueError("Turn provider, model and prompt versions must be configured")
        if (
            len(sandbox_image_digest) != 71
            or not sandbox_image_digest.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in sandbox_image_digest[7:])
        ):
            raise ValueError("Turn Sandbox image digest must be exact sha256 authority")
        self._sessions = session_factory
        self._commands = commands
        self._jobs = jobs
        self._provider = provider
        self._sandbox = sandbox
        self._limits = limits
        self._versions = versions
        self._rules = dict(rules_by_version)
        self._provider_name = provider_name
        self._model_version = model_version
        self._prompt_version = prompt_version
        self._sandbox_image_digest = sandbox_image_digest
        self._skill_patch_enabled = skill_patch_enabled
        self._lease_seconds = lease_seconds
        self._engine = WorldEngine()
        self._world = PostgresWorld(session_factory)
        self._world_uow = PostgresWorldUnitOfWork(
            session_factory,
            self._rules,
            world_engine=self._engine,
        )
        self._reads = PostgresAgentRuntimeReads(session_factory)
        self._trace = PostgresAgentTrace(session_factory)
        self._outcomes = PostgresRunOutcomeAuthority(
            session_factory,
            jobs,
            lease_seconds=lease_seconds,
        )
        self._role_configs = PackagedRoleConfigProvider.load()
        self._contexts = ContextBuilder(
            tasks=self._reads,
            sessions=self._reads,
            skills=self._reads,
            runs=self._reads,
            counterexamples=self._reads,
            learners=self._reads,
            messages=self._reads,
            worlds=self._world,
            drafts=self._reads,
            interactions=self._reads,
            role_configs=self._role_configs,
            teaching_spec_version=versions.teaching_spec_version,
        )

    async def execute(self, claim: ClaimedWorkflowJob) -> None:
        authority = await self._prepare(claim)
        if authority.event.event_type == "skill_patch_requested":
            await self._execute_skill_patch(authority)
            return
        if authority.event.event_type == "hint_requested":
            await self._execute_hint(authority)
            return
        invocation = PostgresFencedSkillInvocation(
            session_factory=self._sessions,
            commands=self._commands,
            jobs=self._jobs,
            claim=claim,
            sandbox=self._sandbox,
            limits=self._limits,
            versions=authority.command.versions,
            world_uow=self._world_uow,
            world_engine=self._engine,
            rules_by_version=self._rules,
            lease_seconds=self._lease_seconds,
            skill_patch_enabled=self._skill_patch_enabled,
        )
        invocation_id = side_effect_execution_id(
            authority.event.command_id, authority.event.turn_id
        )
        root_llm = PostgresDurableLlm(
            session_factory=self._sessions,
            jobs=self._jobs,
            claim=claim,
            provider=self._provider,
            provider_name=self._provider_name,
            model_version=self._model_version,
            lease_seconds=self._lease_seconds,
            receipt_namespace="ROOT",
            ordinal_base=0,
        )
        root_runtime = self._runtime(
            claim,
            invocation,
            root_llm,
            authority.command.versions,
            clock_at=authority.event.occurred_at,
        )
        route = RoleRouter().route(authority.event)
        if not route.should_run or route.role != "xiaohutao":
            raise WorkflowInvariantError("run_skill_requested did not route to xiaohutao")
        result = await invocation.get_result(invocation_id, authority.context)
        recovered_root = result is not None
        if result is None:
            turn_context = await self._contexts.build(
                authority.event,
                "xiaohutao",
                authority.context,
            )
            root_decision = await root_runtime.run(
                "xiaohutao",
                turn_context,
                authority.context,
            )
            result = await invocation.get_result(invocation_id, authority.context)
        else:
            recovery = await self._contexts.build_skill_recovery(
                authority.event,
                authority.context,
            )
            root_decision = await root_runtime.recover_skill_invocation(
                recovery,
                result,
                authority.context,
            )
        if result is None:
            raise WorkflowInvariantError("provider decision completed without a Skill Run")
        if root_decision.role != "xiaohutao" or set(root_decision.evidence_refs) != set(
            result.run.evidence_refs
        ):
            raise WorkflowInvariantError("xiaohutao decision is not closed over its Skill Run")
        receipt_recovery = (
            root_decision.fallback_reason == "SIDE_EFFECT_RECEIPT_RECOVERED"
            and root_decision.source == "provider_fallback"
            and root_decision.degraded
        )
        if recovered_root and not receipt_recovery:
            raise WorkflowInvariantError("existing Skill Run did not use receipt recovery")
        if not receipt_recovery and (root_decision.source != "provider" or root_decision.degraded):
            raise WorkflowInvariantError(
                "initial production Turn must retain a non-degraded provider decision"
            )
        outcome = await self._outcomes.derive(
            claim,
            root_event=authority.event,
            context=authority.context,
        )
        final_route = RoleRouter().route(outcome)
        if not final_route.should_run or final_route.role not in {
            "teaching_agent",
            "bug_agent",
            "book_agent",
        }:
            raise WorkflowInvariantError("Run outcome did not route to one final A8 role")
        try:
            final_context = await self._contexts.build(
                outcome,
                final_route.role,
                authority.context,
            )
        except ValueError as error:
            raise WorkflowBoundaryError("FINAL_CONTEXT_BUILD") from error
        final_llm = PostgresDurableLlm(
            session_factory=self._sessions,
            jobs=self._jobs,
            claim=claim,
            provider=self._provider,
            provider_name=self._provider_name,
            model_version=self._model_version,
            lease_seconds=self._lease_seconds,
            receipt_namespace="FINAL",
            ordinal_base=100,
        )
        final_runtime = self._runtime(
            claim,
            invocation,
            final_llm,
            authority.command.versions,
            clock_at=outcome.occurred_at,
        )
        try:
            decision = await final_runtime.run(
                final_route.role,
                final_context,
                authority.context,
            )
        except RuntimeBoundaryError as error:
            raise _final_runtime_boundary(error) from error
        except ValueError as error:
            raise WorkflowBoundaryError("FINAL_RUNTIME_PRE_DISPATCH") from error
        if (
            decision.role != final_route.role
            or decision.source != "provider"
            or decision.degraded
            or set(decision.evidence_refs) != set(result.run.evidence_refs)
        ):
            raise WorkflowInvariantError(
                "final production role must retain one non-degraded Provider decision"
            )
        await self._outcomes.record_final_decision(
            claim,
            outcome=outcome,
            decision=decision,
            result=result,
            context=authority.context,
        )
        await self._finish(authority, outcome, decision, result)

    async def _execute_hint(self, authority: _TurnAuthority) -> None:
        """Dispatch one teaching role for a hint with no Run and no World change."""

        route = RoleRouter().route(authority.event)
        if not route.should_run or route.role not in {"teaching_agent", "bug_agent"}:
            raise WorkflowInvariantError("hint_requested did not route to one teaching role")
        try:
            turn_context = await self._contexts.build(
                authority.event,
                route.role,
                authority.context,
            )
        except ValueError as error:
            raise WorkflowBoundaryError("HINT_CONTEXT_BUILD") from error
        llm = PostgresDurableLlm(
            session_factory=self._sessions,
            jobs=self._jobs,
            claim=authority.claim,
            provider=self._provider,
            provider_name=self._provider_name,
            model_version=self._model_version,
            lease_seconds=self._lease_seconds,
            receipt_namespace="HINT",
            ordinal_base=300,
        )
        runtime = self._hint_runtime(
            llm,
            authority.command.versions,
            clock_at=authority.event.occurred_at,
        )
        try:
            decision = await runtime.run(
                route.role,
                turn_context,
                authority.context,
            )
        except RuntimeBoundaryError as error:
            raise WorkflowBoundaryError(f"HINT_RUNTIME_{error.stage.value}") from error
        except ValueError as error:
            raise WorkflowBoundaryError("HINT_RUNTIME_PRE_DISPATCH") from error
        directive = decision.teaching_directive
        hint_mismatches: list[str] = []
        if decision.role != route.role:
            hint_mismatches.append("ROLE")
        if decision.response_type not in {"question", "hint"}:
            hint_mismatches.append("RESPONSE_TYPE")
        if decision.draft.skill_patch is not None:
            hint_mismatches.append("PATCH_BODY")
        if decision.source != "provider":
            hint_mismatches.append("SOURCE")
        if decision.degraded:
            hint_mismatches.append("DEGRADED")
        if any(item.name == "invoke_skill" for item in decision.tool_calls):
            hint_mismatches.append("TOOLS")
        if decision.evidence_refs != collect_decision_evidence(turn_context):
            hint_mismatches.append("DECISION_EVIDENCE")
        if decision.evidence_refs:
            # A hint observes prior Evidence through its prompt; it never owns
            # any, because it produces no Run and no World commit.
            hint_mismatches.append("OWNED_EVIDENCE")
        if (
            directive is None
            or directive.patch_eligible
            or directive.full_solution_eligible
            or directive != turn_context.teaching_directive
        ):
            hint_mismatches.append("TEACHING_DIRECTIVE")
        if hint_mismatches:
            raise WorkflowInvariantError(
                "hint decision closure mismatch: " + ",".join(hint_mismatches)
            )
        from walnut_backend.workers.turn_projection import finish_hint_interaction

        await finish_hint_interaction(
            session_factory=self._sessions,
            commands=self._commands,
            jobs=self._jobs,
            authority=authority,
            decision=decision,
            lease_seconds=self._lease_seconds,
        )

    async def _execute_skill_patch(self, authority: _TurnAuthority) -> None:
        """Dispatch only teaching_agent for one pre-authorized UI request."""

        route = RoleRouter().route(authority.event)
        if not route.should_run or route.role != "teaching_agent":
            raise WorkflowInvariantError(
                "skill_patch_requested did not route to teaching_agent"
            )
        try:
            turn_context = await self._contexts.build(
                authority.event,
                "teaching_agent",
                authority.context,
            )
        except ValueError as error:
            raise WorkflowBoundaryError("PATCH_CONTEXT_BUILD") from error
        llm = PostgresDurableLlm(
            session_factory=self._sessions,
            jobs=self._jobs,
            claim=authority.claim,
            provider=self._provider,
            provider_name=self._provider_name,
            model_version=self._model_version,
            lease_seconds=self._lease_seconds,
            receipt_namespace="PATCH",
            ordinal_base=200,
        )
        runtime = self._patch_runtime(
            llm,
            authority.command.versions,
            clock_at=authority.event.occurred_at,
        )
        try:
            decision = await runtime.run(
                "teaching_agent",
                turn_context,
                authority.context,
            )
        except RuntimeBoundaryError as error:
            raise WorkflowBoundaryError(f"PATCH_RUNTIME_{error.stage.value}") from error
        except ValueError as error:
            raise WorkflowBoundaryError("PATCH_RUNTIME_PRE_DISPATCH") from error
        proposal_mismatches: list[str] = []
        if decision.role != "teaching_agent":
            proposal_mismatches.append("ROLE")
        if decision.response_type != "skill_patch":
            proposal_mismatches.append("RESPONSE_TYPE")
        proposal = decision.draft.skill_patch
        if proposal is None:
            proposal_mismatches.append("PATCH_BODY")
        if decision.source != "provider":
            proposal_mismatches.append("SOURCE")
        if decision.degraded:
            proposal_mismatches.append("DEGRADED")
        if decision.tool_calls:
            proposal_mismatches.append("TOOLS")
        if decision.evidence_refs != collect_decision_evidence(turn_context):
            proposal_mismatches.append("DECISION_EVIDENCE")
        if (
            proposal is not None
            and proposal.failed.evidence_refs != authority.event.evidence_refs
        ):
            proposal_mismatches.append("FAILURE_EVIDENCE")
        if proposal_mismatches:
            raise WorkflowInvariantError(
                "Skill Patch proposal closure mismatch: "
                + ",".join(proposal_mismatches)
            )
        from walnut_backend.workers.turn_projection import finish_skill_patch_proposal

        await finish_skill_patch_proposal(
            session_factory=self._sessions,
            commands=self._commands,
            jobs=self._jobs,
            authority=authority,
            decision=decision,
            lease_seconds=self._lease_seconds,
        )

    def _runtime(
        self,
        claim: ClaimedWorkflowJob,
        invocation: PostgresFencedSkillInvocation,
        llm: LlmPort,
        versions: VersionSet,
        *,
        clock_at: datetime,
    ) -> SharedAgentRuntime:
        del claim
        tools = build_default_tool_registry(self._trace, invocation)
        return SharedAgentRuntime(
            llm=llm,
            role_configs=self._role_configs,
            tools=tools,
            prompts=PromptBuilder(),
            trace=self._trace,
            versions=versions,
            clock=lambda: clock_at,
        )

    def _hint_runtime(
        self,
        llm: LlmPort,
        versions: VersionSet,
        *,
        clock_at: datetime,
    ) -> SharedAgentRuntime:
        """A teaching runtime whose only tools are the role's read-only reads."""

        return SharedAgentRuntime(
            llm=llm,
            role_configs=self._role_configs,
            tools=build_default_tool_registry(self._trace, _NoSkillInvocation()),
            prompts=PromptBuilder(),
            trace=self._trace,
            versions=versions,
            clock=lambda: clock_at,
        )

    def _patch_runtime(
        self,
        llm: LlmPort,
        versions: VersionSet,
        *,
        clock_at: datetime,
    ) -> SharedAgentRuntime:
        """A teaching-only runtime with no executable tools by construction."""

        return SharedAgentRuntime(
            llm=llm,
            role_configs=self._role_configs,
            tools=ToolRegistry(self._trace),
            prompts=PromptBuilder(),
            trace=self._trace,
            versions=versions,
            clock=lambda: clock_at,
        )

    async def _prepare(self, claim: ClaimedWorkflowJob) -> _TurnAuthority:
        if claim.operation not in self.operations or claim.subject_type != "AGENT_TURN":
            raise ValueError("unsupported Turn workflow")
        async with self._sessions() as session, session.begin():
            owned = await self._jobs.start_step_in_session(
                session,
                claim,
                phase="AGENT_CONTEXT",
                lease_seconds=self._lease_seconds,
            )
            command = await _command(session, owned, for_update=True)
            context = _operation_context(command)
            turn = await session.scalar(
                select(AgentTurnRow)
                .where(
                    AgentTurnRow.tenant_id == owned.tenant_id,
                    AgentTurnRow.actor_id == context.actor.actor_id,
                    AgentTurnRow.command_id == owned.command_id,
                    AgentTurnRow.turn_id == owned.subject_id,
                )
                .with_for_update()
            )
            if turn is None:
                raise WorkflowInvariantError("Agent Turn resource disappeared")
            owner = await session.scalar(
                select(AgentSessionRow)
                .where(
                    AgentSessionRow.tenant_id == owned.tenant_id,
                    AgentSessionRow.actor_id == context.actor.actor_id,
                    AgentSessionRow.session_id == turn.session_id,
                    AgentSessionRow.status == "ACTIVE",
                )
                .with_for_update()
            )
            binding = await session.scalar(
                select(CurrentSessionBindingRow).where(
                    CurrentSessionBindingRow.tenant_id == owned.tenant_id,
                    CurrentSessionBindingRow.actor_id == context.actor.actor_id,
                    CurrentSessionBindingRow.content_hash == context.content_ref.content_hash,
                    CurrentSessionBindingRow.session_id == turn.session_id,
                )
            )
            if owner is None or binding is None or owner.world_id != binding.world_id:
                raise WorkflowInvariantError("Turn Session authority is missing")
            launch = await session.scalar(
                select(LaunchAuthorityRow).where(
                    LaunchAuthorityRow.tenant_id == owned.tenant_id,
                    LaunchAuthorityRow.authority_id == binding.authority_id,
                    LaunchAuthorityRow.actor_id == binding.actor_id,
                    LaunchAuthorityRow.content_hash == binding.content_hash,
                    LaunchAuthorityRow.world_id == binding.world_id,
                    LaunchAuthorityRow.active.is_(True),
                )
            )
            binding_observed_at = await current_session_binding_observed_at(session)
            if (
                launch is None
                or binding_observed_at is None
                or not current_session_binding_matches(
                    binding,
                    owner=owner,
                    authority=launch,
                    observed_at=binding_observed_at,
                )
            ):
                raise WorkflowInvariantError("Turn Session binding authority is corrupt")
            profile = await session.scalar(
                select(AgentProfileRow).where(
                    AgentProfileRow.tenant_id == owned.tenant_id,
                    AgentProfileRow.agent_profile_id == launch.agent_profile_id,
                    AgentProfileRow.actor_id == launch.actor_id,
                    AgentProfileRow.content_hash == launch.content_hash,
                )
            )
            if profile is None or profile.profile_sha256 != canonical_json_sha256(
                profile.profile_json
            ):
                raise WorkflowInvariantError("Turn Agent profile authority is corrupt")
            expected_runtime = (
                profile.profile_json.get("provider"),
                profile.profile_json.get("model_version"),
                profile.profile_json.get("prompt_version"),
                command.versions.model_version,
                command.versions.prompt_version,
                command.versions.sandbox_image_digest,
                command.versions.teaching_spec_version,
            )
            configured_runtime = (
                self._provider_name,
                self._model_version,
                self._prompt_version,
                self._model_version,
                self._prompt_version,
                self._sandbox_image_digest,
                self._versions.teaching_spec_version,
            )
            if expected_runtime != configured_runtime:
                raise WorkflowInvariantError(
                    "Turn provider/model/prompt/Sandbox/TeachingSpec authority drifted"
                )
            if command.versions.world_rules_version not in self._rules:
                raise WorkflowInvariantError("Turn WorldRules version is not configured")
            request = _object(turn.request_json, "Turn request")
            # A hint asks the teaching roles to explain the student's current
            # situation.  It never invokes the Skill and never touches the
            # World, so it is the one Turn that carries no binding.  The
            # teaching roles still need the exact source they teach about, so
            # the server adopts its own Registry head rather than trusting a
            # client tuple.  A declared binding always means "execute this
            # Skill" and is validated against that same head.
            requested_input = _object(request.get("input"), "Turn input")
            raw_bindings = request.get("skill_bindings")
            if not isinstance(raw_bindings, list):
                raise WorkflowInvariantError("Turn skill_bindings must be an array")
            is_hint_request = (
                not raw_bindings and requested_input.get("type") == "MESSAGE"
            )
            requested_ref: SkillRef | None = None
            if not is_hint_request:
                if len(raw_bindings) != 1 or not isinstance(raw_bindings[0], Mapping):
                    raise WorkflowInvariantError("Turn has no unique Skill binding")
                requested_ref = SkillRef(**dict(raw_bindings[0]))
            active = await load_current_activation_authority(
                session,
                tenant_id=owned.tenant_id,
                actor_id=binding.actor_id,
                content_hash=binding.content_hash,
                world_id=binding.world_id,
                agent_profile_id=binding.agent_profile_id,
                authority_id=binding.authority_id,
                skill_ref=requested_ref,
            )
            skill_ref: SkillRef = active.skill_ref
            if (
                command.versions.skill_version != skill_ref.skill_version_id
                or command.versions.artifact_sha256 != skill_ref.artifact_sha256
            ):
                raise WorkflowInvariantError("Turn Activation authority drifted after acceptance")
            content = await session.scalar(
                select(ProductContentUnitRow).where(
                    ProductContentUnitRow.tenant_id == owned.tenant_id,
                    ProductContentUnitRow.unit_id == context.content_ref.unit_id,
                    ProductContentUnitRow.version == context.content_ref.version,
                    ProductContentUnitRow.content_hash == context.content_ref.content_hash,
                )
            )
            if content is None:
                raise WorkflowInvariantError("Turn ContentUnit disappeared")
            task = _object(content.content_json.get("task"), "Content task")
            task_id = _text(task, "task_id")
            expected_revision = _integer(request, "expected_world_revision")
            turn_input = _object(request.get("input"), "Turn input")
            is_patch_request = (
                turn_input.get("type") == "UI_ACTION"
                and turn_input.get("action_id") == "request_ai_patch"
            )
            if is_patch_request:
                if not self._skill_patch_enabled:
                    raise WorkflowInvariantError("Skill Patch capability is disabled")
                if context.actor.actor_type is not ActorType.STUDENT:
                    raise WorkflowInvariantError("Skill Patch requires a student actor")
                if set(turn_input) != {"type", "action_id", "selection_id"}:
                    raise WorkflowInvariantError("Skill Patch UI action is not exact")
                selected_id = _text(turn_input, "selection_id")
                selected = await self._reads.get_current_failed_interaction(
                    turn.session_id,
                    selected_id,
                    context,
                )
                compile_result = await self._reads.get_compile_result(
                    selected.build_id,
                    context,
                )
                run_result = await self._reads.get_run(selected.run_id, context)
                draft_authority = compile_result.draft_authority
                if draft_authority is None:
                    raise WorkflowInvariantError(
                        "selected failed Build has no immutable Draft provenance"
                    )
                current_draft = await self._reads.get_current_draft(
                    turn.session_id,
                    draft_authority.draft_id,
                    context,
                )
                # The outer transaction already owns the Session row, which is
                # the Interaction/Turn append serialization point.  Lock the
                # mutable Draft head as the other eligibility cursor, then
                # re-close every typed read before creating the durable
                # pre-Provider reservation.  Provider dispatch starts only
                # after this transaction commits.
                selected_row = await session.scalar(
                    select(ProductInteractionRow)
                    .where(
                        ProductInteractionRow.tenant_id == owned.tenant_id,
                        ProductInteractionRow.actor_id == context.actor.actor_id,
                        ProductInteractionRow.session_id == turn.session_id,
                        ProductInteractionRow.interaction_id == selected.interaction_id,
                        ProductInteractionRow.interaction_revision
                        == selected.interaction_revision,
                        ProductInteractionRow.sequence
                        == selected.interaction_sequence,
                    )
                    .with_for_update()
                )
                selected_hwm = await session.scalar(
                    select(func.max(ProductInteractionRow.sequence)).where(
                        ProductInteractionRow.tenant_id == owned.tenant_id,
                        ProductInteractionRow.actor_id == context.actor.actor_id,
                        ProductInteractionRow.session_id == turn.session_id,
                    )
                )
                current_draft_row = await session.scalar(
                    select(ProductDraftRow)
                    .where(
                        ProductDraftRow.tenant_id == owned.tenant_id,
                        ProductDraftRow.actor_id == context.actor.actor_id,
                        ProductDraftRow.session_id == turn.session_id,
                        ProductDraftRow.draft_id == draft_authority.draft_id,
                    )
                    .with_for_update()
                )
                immutable_draft = await session.scalar(
                    select(ProductDraftRevisionRow).where(
                        ProductDraftRevisionRow.tenant_id == owned.tenant_id,
                        ProductDraftRevisionRow.actor_id == context.actor.actor_id,
                        ProductDraftRevisionRow.session_id == turn.session_id,
                        ProductDraftRevisionRow.draft_id == draft_authority.draft_id,
                        ProductDraftRevisionRow.skill_id == draft_authority.skill_id,
                        ProductDraftRevisionRow.revision
                        == draft_authority.draft_revision,
                        ProductDraftRevisionRow.draft_sha256
                        == draft_authority.draft_sha256,
                    )
                )
                failed_build = await session.scalar(
                    select(SkillBuildRow).where(
                        SkillBuildRow.tenant_id == owned.tenant_id,
                        SkillBuildRow.actor_id == context.actor.actor_id,
                        SkillBuildRow.build_id == selected.build_id,
                    )
                )
                failed_build_provenance = await session.scalar(
                    select(SkillBuildProvenanceRow).where(
                        SkillBuildProvenanceRow.build_id == selected.build_id,
                        SkillBuildProvenanceRow.tenant_id == owned.tenant_id,
                        SkillBuildProvenanceRow.actor_id == context.actor.actor_id,
                        SkillBuildProvenanceRow.session_id == turn.session_id,
                        SkillBuildProvenanceRow.draft_revision_row_id
                        == (
                            immutable_draft.draft_revision_row_id
                            if immutable_draft is not None
                            else -1
                        ),
                    )
                )
                failed_run = await session.scalar(
                    select(RunRow).where(
                        RunRow.tenant_id == owned.tenant_id,
                        RunRow.actor_id == context.actor.actor_id,
                        RunRow.session_id == turn.session_id,
                        RunRow.run_id == selected.run_id,
                    )
                )
                failed_run_provenance = await session.scalar(
                    select(SkillRunProvenanceRow).where(
                        SkillRunProvenanceRow.run_id == selected.run_id,
                        SkillRunProvenanceRow.build_id == selected.build_id,
                        SkillRunProvenanceRow.tenant_id == owned.tenant_id,
                        SkillRunProvenanceRow.actor_id == context.actor.actor_id,
                        SkillRunProvenanceRow.session_id == turn.session_id,
                    )
                )
                evidence_rows = list(
                    (
                        await session.scalars(
                            select(EvidenceRow).where(
                                EvidenceRow.tenant_id == owned.tenant_id,
                                EvidenceRow.actor_id == context.actor.actor_id,
                                EvidenceRow.evidence_id.in_(
                                    [item.evidence_id for item in selected.evidence_refs]
                                ),
                            )
                        )
                    ).all()
                )
                validated_failed_build = (
                    await validate_run_provenance(
                        session,
                        failed_run_provenance,
                        require_immutable=True,
                    )
                    if failed_run_provenance is not None
                    else None
                )
                selected_value = (
                    selected_row.interaction_json if selected_row is not None else {}
                )
                hint_policy = _object(task.get("hint_policy"), "Content hint policy")
                if (
                    selected_row is None
                    or selected_hwm != selected.interaction_sequence
                    or selected_row.sequence
                    != selected.same_failure_suffix_end_sequence
                    or current_draft_row is None
                    or immutable_draft is None
                    or failed_build is None
                    or failed_build_provenance is None
                    or failed_run is None
                    or failed_run_provenance is None
                    or validated_failed_build is None
                    or validated_failed_build.build_id
                    != failed_build_provenance.build_id
                    or validated_failed_build.authority_sha256
                    != failed_build_provenance.authority_sha256
                    or selected_value.get("role")
                    not in {"teaching_agent", "bug_agent"}
                    or selected_value.get("response_type")
                    not in {"question", "hint", "message"}
                    or (
                        selected_value.get("hint_level") is not None
                        and (
                            isinstance(selected_value.get("hint_level"), bool)
                            or not isinstance(selected_value.get("hint_level"), int)
                            or not 0 <= cast(int, selected_value.get("hint_level")) <= 3
                        )
                    )
                    or current_draft_row.draft_json != immutable_draft.draft_json
                    or current_draft_row.revision != immutable_draft.revision
                    or current_draft_row.draft_sha256
                    != immutable_draft.draft_sha256
                    or failed_build_provenance.draft_revision_row_id
                    != immutable_draft.draft_revision_row_id
                    or failed_build_provenance.draft_sha256
                    != immutable_draft.draft_sha256
                    or failed_run_provenance.draft_revision_row_id
                    != immutable_draft.draft_revision_row_id
                    or failed_run_provenance.draft_sha256
                    != immutable_draft.draft_sha256
                    or failed_run.command_id != selected.command_id
                    or len(evidence_rows) != len(selected.evidence_refs)
                    or any(
                        evidence.evidence_json.get("evidence_ref")
                        != evidence_ref_wire(reference)
                        for evidence, reference in zip(
                            sorted(evidence_rows, key=lambda item: item.evidence_id),
                            sorted(selected.evidence_refs, key=lambda item: item.evidence_id),
                            strict=True,
                        )
                    )
                    or not await _run_interactions_have_authority(
                        session, [selected_row], owner
                    )
                    or selected.failure_count < 4
                    or _integer(hint_policy, "max_level") != 4
                    or selected.session_id != turn.session_id
                    or selected.task_id != task_id
                    or selected.skill_ref != skill_ref
                    or selected.interaction_sequence
                    != selected.same_failure_suffix_end_sequence
                    or compile_result.skill_ref != skill_ref
                    or not compile_result.succeeded
                    or compile_result.draft_authority != current_draft.authority
                    or run_result.run_id != selected.run_id
                    or run_result.build_id != selected.build_id
                    or run_result.task_success
                    or run_result.failure_key != selected.failure_key
                    or run_result.evidence_refs != selected.evidence_refs
                    or run_result.world_commit is not None
                    or run_result.world_revision_after != expected_revision
                ):
                    raise WorkflowInvariantError(
                        "Skill Patch selected failure or current Draft authority drifted"
                    )
                payload = {
                    "source_event_type": "UI_ACTION",
                    "action_id": "request_ai_patch",
                    "requested_interaction_id": selected.interaction_id,
                    "feature_enabled": True,
                    "capability_enabled": True,
                    "effective_hint_level": 4,
                    "draft_authority": {
                        "draft_id": draft_authority.draft_id,
                        "session_id": draft_authority.session_id,
                        "skill_id": draft_authority.skill_id,
                        "draft_revision": draft_authority.draft_revision,
                        "draft_sha256": draft_authority.draft_sha256,
                        "source_bundle_sha256": draft_authority.source_bundle_sha256,
                        "entrypoint": draft_authority.entrypoint,
                        "entrypoint_sha256": draft_authority.entrypoint_sha256,
                    },
                }
                event = GameEvent(
                    event_id=_identifier("gameevent", command.command_id),
                    event_type="skill_patch_requested",
                    student_id=context.actor.actor_id,
                    task_id=task_id,
                    session_id=turn.session_id,
                    turn_id=turn.turn_id,
                    command_id=command.command_id,
                    occurred_at=turn.created_at,
                    expected_world_revision=expected_revision,
                    skill_ref=skill_ref,
                    build_id=selected.build_id,
                    run_id=selected.run_id,
                    failure_count=selected.failure_count,
                    failure_key=selected.failure_key,
                    evidence_refs=selected.evidence_refs,
                    payload=payload,
                )
                if turn.turn_sequence != owner.session_json.get("last_turn_sequence"):
                    raise WorkflowInvariantError(
                        "Skill Patch request was superseded by a later Turn"
                    )
                existing_request = await session.scalar(
                    select(ProductSkillPatchRequestRow).where(
                        ProductSkillPatchRequestRow.tenant_id == owned.tenant_id,
                        ProductSkillPatchRequestRow.requested_interaction_id
                        == selected.interaction_id,
                    ).with_for_update()
                )
                existing_proposal = await session.scalar(
                    select(ProductSkillPatchProposalRow.patch_id).where(
                        ProductSkillPatchProposalRow.tenant_id == owned.tenant_id,
                        ProductSkillPatchProposalRow.requested_interaction_id
                        == selected.interaction_id,
                    )
                )
                request_authority_sha256 = canonical_json_sha256(
                    cast(dict[str, Any], json_value(event))
                )
                request_id = _identifier(
                    "patchrequest", f"{owned.tenant_id}:{command.command_id}"
                )
                if existing_proposal is not None:
                    raise WorkflowInvariantError(
                        "selected failure already has a Skill Patch proposal"
                    )
                if existing_request is None:
                    session.add(
                        ProductSkillPatchRequestRow(
                            request_id=request_id,
                            tenant_id=owned.tenant_id,
                            actor_id=context.actor.actor_id,
                            session_id=turn.session_id,
                            turn_id=turn.turn_id,
                            command_id=command.command_id,
                            requested_interaction_id=selected.interaction_id,
                            authority_sha256=request_authority_sha256,
                            status="PENDING",
                            proposal_id=None,
                            created_at=turn.created_at,
                            updated_at=turn.created_at,
                        )
                    )
                elif (
                    existing_request.request_id != request_id
                    or existing_request.actor_id != context.actor.actor_id
                    or existing_request.session_id != turn.session_id
                    or existing_request.turn_id != turn.turn_id
                    or existing_request.command_id != command.command_id
                    or existing_request.authority_sha256 != request_authority_sha256
                    or existing_request.status != "PENDING"
                    or existing_request.proposal_id is not None
                ):
                    raise WorkflowInvariantError(
                        "selected failure is reserved by another Patch request"
                    )
                if command.status is CommandStatus.ACCEPTED:
                    validating = replace(
                        command,
                        status=CommandStatus.VALIDATING,
                        stage="POLICY",
                        revision=command.revision + 1,
                        updated_at=max(command.updated_at, turn.created_at),
                    )
                    transitioned = await self._commands.transition_in_session(
                        session,
                        CommandTransition(command, validating),
                        context,
                    )
                    if isinstance(transitioned, Failure):
                        raise WorkflowInvariantError(
                            "Skill Patch Command validation CAS was lost"
                        )
                    command = validating
                elif (
                    command.status is not CommandStatus.VALIDATING
                    or command.stage != "POLICY"
                ):
                    raise WorkflowInvariantError(
                        "Skill Patch Command is outside its policy stage"
                    )
                return _TurnAuthority(
                    claim=owned,
                    command=command,
                    context=context,
                    event=event,
                    task=task,
                    learner_id=launch.learner_id,
                )
            event = GameEvent(
                event_id=_identifier("gameevent", command.command_id),
                # hint_requested routes to the teaching roles without a Skill Run;
                # run_skill_requested routes to xiaohutao and executes the Skill.
                event_type="hint_requested" if is_hint_request else "run_skill_requested",
                student_id=context.actor.actor_id,
                task_id=task_id,
                session_id=turn.session_id,
                turn_id=turn.turn_id,
                command_id=command.command_id,
                occurred_at=turn.created_at,
                expected_world_revision=expected_revision,
                skill_ref=skill_ref,
                payload={"input": turn_input},
            )
            if is_hint_request:
                # A hint never enters SANDBOX or WORLD, so its only pre-Provider
                # stage is POLICY.  The Skill Run path leaves the Command in
                # ACCEPTED and lets the fenced invocation own every transition.
                if turn.turn_sequence != owner.session_json.get("last_turn_sequence"):
                    raise WorkflowInvariantError("hint request was superseded by a later Turn")
                if command.status is CommandStatus.ACCEPTED:
                    validating = replace(
                        command,
                        status=CommandStatus.VALIDATING,
                        stage="POLICY",
                        revision=command.revision + 1,
                        updated_at=max(command.updated_at, turn.created_at),
                    )
                    transitioned = await self._commands.transition_in_session(
                        session,
                        CommandTransition(command, validating),
                        context,
                    )
                    if isinstance(transitioned, Failure):
                        raise WorkflowInvariantError("hint Command validation CAS was lost")
                    command = validating
                elif (
                    command.status is not CommandStatus.VALIDATING
                    or command.stage != "POLICY"
                ):
                    raise WorkflowInvariantError("hint Command is outside its policy stage")
            return _TurnAuthority(
                claim=owned,
                command=command,
                context=context,
                event=event,
                task=task,
                learner_id=launch.learner_id,
            )

    async def _finish(
        self,
        authority: _TurnAuthority,
        outcome: GameEvent,
        decision: AgentDecision,
        result: SkillInvocationResult,
    ) -> None:
        from walnut_backend.workers.turn_projection import finish_turn_projection

        await finish_turn_projection(
            session_factory=self._sessions,
            commands=self._commands,
            jobs=self._jobs,
            authority=authority,
            outcome=outcome,
            decision=decision,
            result=result,
            lease_seconds=self._lease_seconds,
        )


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
    value = command_record_from_data(row.record_json)
    if value.command_type != claim.operation or value.terminal:
        raise WorkflowInvariantError("Turn Command is not executable")
    return value


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


def _identifier(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


__all__ = ["TurnWorkflowHandler", "_TurnAuthority"]
