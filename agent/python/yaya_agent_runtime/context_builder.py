"""Role-minimal, identity-anchored context construction."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import cast

from yaya_agent_contracts import (
    ActorType,
    Failure,
    OperationContext,
    RequestContext,
    Success,
    WorldPort,
    WorldSnapshot,
)

from .domain import (
    CompileResultSnapshot,
    CounterexampleSnapshot,
    DraftSnapshot,
    FailedInteractionSnapshot,
    GameEvent,
    LearnerProfileSnapshot,
    MessageSnapshot,
    RoleId,
    RunResultSnapshot,
    SessionSnapshot,
    SkillPatchAuthority,
    SkillPatchFailureAuthority,
    SkillPatchRequestAuthority,
    SkillRecoveryContext,
    SkillSnapshot,
    SkillVersionSummary,
    TaskSnapshot,
    TurnContext,
    WorldSummary,
)
from .errors import AgentContextError, AgentDependencyError
from .learner_projection_policy import EvidenceStage
from .pedagogy_policy import (
    LearnerCompetencySummary,
    PedagogyEvidence,
    PedagogyEvidenceOutcome,
    PedagogyInput,
    PedagogyPolicy,
    PedagogyPolicyError,
)
from .ports import (
    CounterexampleReadPort,
    DraftReadPort,
    InteractionReadPort,
    LearnerReadPort,
    MessageReadPort,
    RunReadPort,
    SessionReadPort,
    SkillReadPort,
    TaskReadPort,
)
from .role_config import RoleConfigProvider
from .router import calculate_hint_level


def _context_error(code: str, message: str, **details: object) -> AgentContextError:
    return AgentContextError(code, message, details)


def _require_snapshot[T](value: object, expected_type: type[T], field_name: str) -> T:
    if not isinstance(value, expected_type):
        raise _context_error(
            "CONTEXT_PORT_TYPE_MISMATCH",
            f"{field_name} port returned the wrong snapshot type",
            field=field_name,
            expected=expected_type.__name__,
            actual=type(value).__name__,
        )
    return value


def _require_snapshot_sequence[T](
    value: object,
    expected_type: type[T],
    field_name: str,
    *,
    maximum: int,
) -> tuple[T, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise _context_error(
            "CONTEXT_PORT_TYPE_MISMATCH",
            f"{field_name} port must return a sequence",
        )
    values = tuple(cast(Sequence[object], value))
    if len(values) > maximum:
        raise _context_error(
            "CONTEXT_RESULT_TOO_LARGE",
            f"{field_name} exceeded its bounded context limit",
            field=field_name,
            maximum=maximum,
            actual=len(values),
        )
    if any(not isinstance(item, expected_type) for item in values):
        raise _context_error(
            "CONTEXT_PORT_TYPE_MISMATCH",
            f"{field_name} contains an invalid snapshot type",
        )
    return cast(tuple[T, ...], values)


def _validate_operation_identity(event: GameEvent, context: OperationContext) -> None:
    if event.command_id != context.command_id:
        raise _context_error(
            "CONTEXT_COMMAND_MISMATCH",
            "event command_id does not match OperationContext",
            event_command_id=event.command_id,
            context_command_id=context.command_id,
        )
    if event.student_id != context.actor.actor_id:
        raise _context_error(
            "CONTEXT_ACTOR_MISMATCH",
            "event student_id does not match the authenticated actor",
            event_student_id=event.student_id,
            actor_id=context.actor.actor_id,
        )
    if (
        event.event_type == "skill_patch_requested"
        and context.actor.actor_type is not ActorType.STUDENT
    ):
        raise _context_error(
            "CONTEXT_PATCH_STUDENT_REQUIRED",
            "Skill Patch requires an authenticated student actor",
            actor_type=context.actor.actor_type.value,
        )


def _validate_snapshot_provenance(
    snapshot_context: object,
    operation_context: OperationContext,
    label: str,
) -> None:
    if not isinstance(snapshot_context, RequestContext):
        raise _context_error(
            "CONTEXT_PROVENANCE_MISSING",
            f"{label} snapshot has no typed RequestContext provenance",
        )
    snapshot_actor = snapshot_context.actor
    operation_actor = operation_context.actor
    if (
        snapshot_actor.tenant_id,
        snapshot_actor.actor_id,
        snapshot_actor.actor_type,
    ) != (
        operation_actor.tenant_id,
        operation_actor.actor_id,
        operation_actor.actor_type,
    ):
        raise _context_error(
            "CONTEXT_ACTOR_MISMATCH",
            f"{label} snapshot was authorized for a different actor",
        )
    if snapshot_context.content_ref != operation_context.content_ref:
        raise _context_error(
            "CONTEXT_CONTENT_MISMATCH",
            f"{label} snapshot uses a different pinned content version",
        )


def _validate_run_identity(
    run: RunResultSnapshot, event: GameEvent, session: SessionSnapshot
) -> None:
    expected = {
        "run_id": event.run_id,
        "session_id": event.session_id,
        "world_id": session.world_id,
        "world_revision_before": event.expected_world_revision,
    }
    actual = {
        "run_id": run.run_id,
        "session_id": run.session_id,
        "world_id": run.world_id,
        "world_revision_before": run.world_revision_before,
    }
    if event.event_type != "skill_patch_requested":
        expected.update(turn_id=event.turn_id, command_id=event.command_id)
        actual.update(turn_id=run.turn_id, command_id=run.command_id)
    if actual != expected:
        raise _context_error(
            "CONTEXT_RUN_IDENTITY_MISMATCH",
            "run snapshot is not the canonical run for this turn",
            expected=expected,
            actual=actual,
        )
    if event.skill_ref is not None and run.skill_ref != event.skill_ref:
        raise _context_error(
            "CONTEXT_SKILL_BINDING_MISMATCH",
            "run snapshot does not use the event's certified skill binding",
        )
    event_evidence = {item.evidence_id: item for item in event.evidence_refs}
    run_evidence = {item.evidence_id: item for item in run.evidence_refs}
    for evidence_id, evidence in event_evidence.items():
        if run_evidence.get(evidence_id) != evidence:
            raise _context_error(
                "CONTEXT_EVIDENCE_MISMATCH",
                "event evidence is absent or relabelled in the canonical run",
                evidence_id=evidence_id,
            )


def _validate_world_snapshot(
    snapshot: WorldSnapshot,
    event: GameEvent,
    session: SessionSnapshot,
    context: OperationContext,
) -> None:
    if snapshot.world_id != session.world_id:
        raise _context_error(
            "CONTEXT_WORLD_MISMATCH",
            "world snapshot does not belong to the session",
            expected=session.world_id,
            actual=snapshot.world_id,
        )
    if snapshot.revision != event.expected_world_revision:
        raise _context_error(
            "CONTEXT_WORLD_REVISION_MISMATCH",
            "world snapshot revision differs from the accepted turn precondition",
            expected=event.expected_world_revision,
            actual=snapshot.revision,
        )
    snapshot_actor = snapshot.request_context.actor
    context_actor = context.actor
    if (
        snapshot_actor.tenant_id,
        snapshot_actor.actor_id,
        snapshot_actor.actor_type,
    ) != (
        context_actor.tenant_id,
        context_actor.actor_id,
        context_actor.actor_type,
    ):
        raise _context_error(
            "CONTEXT_WORLD_ACTOR_MISMATCH",
            "world snapshot was authorized for a different actor",
        )
    if snapshot.request_context.content_ref != context.content_ref:
        raise _context_error(
            "CONTEXT_CONTENT_MISMATCH",
            "world snapshot uses a different pinned content version",
        )


def summarize_world(snapshot: WorldSnapshot) -> WorldSummary:
    """Create a bounded learner-visible summary without losing identity anchors."""

    plots_raw = snapshot.state["plots"]
    agents_raw = snapshot.state["agents"]
    if not isinstance(plots_raw, tuple) or not isinstance(agents_raw, tuple):
        raise _context_error(
            "CONTEXT_WORLD_STATE_INVALID",
            "validated world state projections must expose immutable arrays",
        )

    plot_summaries: list[dict[str, object]] = []
    for raw_plot in plots_raw[:100]:
        if not isinstance(raw_plot, Mapping):
            raise _context_error("CONTEXT_WORLD_STATE_INVALID", "plot state must be an object")
        crop = raw_plot["crop"]
        crop_type = crop.get("crop_type") if isinstance(crop, Mapping) else None
        plot_summaries.append(
            {
                "plot_id": raw_plot["plot_id"],
                "hydration": raw_plot["hydration"],
                "crop_type": crop_type,
            }
        )
    agent_summaries: list[dict[str, object]] = []
    for raw_agent in agents_raw[:20]:
        if not isinstance(raw_agent, Mapping):
            raise _context_error("CONTEXT_WORLD_STATE_INVALID", "agent state must be an object")
        agent_summaries.append(
            {
                "entity_id": raw_agent["entity_id"],
                "activity": raw_agent["activity"],
            }
        )
    visible_state = {
        "plot_count": len(plots_raw),
        "plots": plot_summaries,
        "plots_truncated": len(plots_raw) > len(plot_summaries),
        "agent_count": len(agents_raw),
        "agents": agent_summaries,
        "agents_truncated": len(agents_raw) > len(agent_summaries),
    }
    return WorldSummary(
        world_id=snapshot.world_id,
        revision=snapshot.revision,
        last_event_sequence=snapshot.last_event_sequence,
        state_hash=snapshot.state_hash,
        visible_state=visible_state,
    )


def _competency_summaries(
    profile: LearnerProfileSnapshot,
) -> tuple[LearnerCompetencySummary, ...]:
    """Decode only the closed projection-policy fields consumed by pedagogy."""

    summaries: list[LearnerCompetencySummary] = []
    for concept, raw in profile.competencies.items():
        if not isinstance(raw, Mapping):
            raise _context_error(
                "CONTEXT_LEARNER_PROJECTION_INVALID",
                "learner competency is not a projection object",
                concept=concept,
            )
        value = cast(Mapping[str, object], raw)
        expected = {
            "concept",
            "evidence_stage",
            "assistance_level",
            "last_observed_at",
            "next_review_at",
            "evidence_ids",
        }
        if set(value) != expected or value.get("concept") != concept:
            raise _context_error(
                "CONTEXT_LEARNER_PROJECTION_INVALID",
                "learner competency fields or concept identity drifted",
                concept=concept,
            )
        next_review_raw = value["next_review_at"]
        try:
            next_review_at = (
                next_review_raw
                if isinstance(next_review_raw, datetime)
                else datetime.fromisoformat(cast(str, next_review_raw).replace("Z", "+00:00"))
            )
            evidence_ids_raw = value["evidence_ids"]
            if isinstance(evidence_ids_raw, (str, bytes, bytearray)) or not isinstance(
                evidence_ids_raw, Sequence
            ):
                raise TypeError("evidence_ids must be an array")
            evidence_items = cast(Sequence[object], evidence_ids_raw)
            if any(not isinstance(item, str) for item in evidence_items):
                raise TypeError("evidence_ids must contain only strings")
            evidence_ids = tuple(item for item in evidence_items if isinstance(item, str))
            summary = LearnerCompetencySummary(
                concept=concept,
                evidence_stage=EvidenceStage(cast(str, value["evidence_stage"])),
                assistance_level=cast(int, value["assistance_level"]),
                next_review_at=next_review_at,
                evidence_ids=evidence_ids,
            )
        except (TypeError, ValueError, AttributeError) as error:
            raise _context_error(
                "CONTEXT_LEARNER_PROJECTION_INVALID",
                "learner competency contains invalid deterministic policy fields",
                concept=concept,
            ) from error
        summaries.append(summary)
    return tuple(summaries)


def _pedagogy_evidence(event: GameEvent) -> tuple[PedagogyEvidence, ...]:
    if event.event_type in {"compile_failed", "run_failed", "skill_patch_requested"} or (
        event.event_type == "hint_requested" and event.failure_count > 0
    ):
        outcome = PedagogyEvidenceOutcome.FAILED
    elif event.event_type == "task_completed":
        outcome = PedagogyEvidenceOutcome.SUCCESS
    else:
        outcome = PedagogyEvidenceOutcome.PARTIAL
    concept_raw = event.payload.get("concept")
    concept = concept_raw if isinstance(concept_raw, str) else None
    return tuple(
        PedagogyEvidence(
            evidence_id=item.evidence_id,
            outcome=outcome,
            occurred_at=item.created_at,
            concept=concept,
        )
        for item in event.evidence_refs
    )


class ContextBuilder:
    def __init__(
        self,
        *,
        tasks: TaskReadPort,
        sessions: SessionReadPort,
        skills: SkillReadPort,
        runs: RunReadPort,
        counterexamples: CounterexampleReadPort,
        learners: LearnerReadPort,
        messages: MessageReadPort,
        worlds: WorldPort,
        role_configs: RoleConfigProvider,
        drafts: DraftReadPort | None = None,
        interactions: InteractionReadPort | None = None,
        pedagogy_policy: PedagogyPolicy | None = None,
        teaching_spec_version: str = "agent-teaching-v1",
    ) -> None:
        self._tasks = tasks
        self._sessions = sessions
        self._skills = skills
        self._runs = runs
        self._counterexamples = counterexamples
        self._learners = learners
        self._messages = messages
        self._worlds = worlds
        self._drafts = drafts
        self._interactions = interactions
        self._role_configs = role_configs
        self._pedagogy_policy = pedagogy_policy or PedagogyPolicy()
        if not isinstance(teaching_spec_version, str) or not teaching_spec_version.strip():
            raise ValueError("teaching_spec_version cannot be blank")
        self._teaching_spec_version = teaching_spec_version

    async def build_skill_recovery(
        self,
        event: GameEvent,
        operation_context: OperationContext,
    ) -> SkillRecoveryContext:
        """Read only immutable identity scope; never require the stale pre-commit World."""

        _validate_operation_identity(event, operation_context)
        if event.event_type != "run_skill_requested" or event.skill_ref is None:
            raise _context_error(
                "CONTEXT_RECOVERY_EVENT_INVALID",
                "Skill receipt recovery requires one bound run_skill_requested event",
            )
        task = _require_snapshot(
            await self._tasks.get_task(event.task_id, operation_context),
            TaskSnapshot,
            "task",
        )
        session = _require_snapshot(
            await self._sessions.get_session(event.session_id, operation_context),
            SessionSnapshot,
            "session",
        )
        skill = _require_snapshot(
            await self._skills.get_bound_skill(event.skill_ref, operation_context),
            SkillSnapshot,
            "skill",
        )
        if task.task_id != event.task_id:
            raise _context_error("CONTEXT_TASK_MISMATCH", "task port returned a different task")
        if (
            session.session_id,
            session.student_id,
            session.task_id,
        ) != (
            event.session_id,
            event.student_id,
            event.task_id,
        ):
            raise _context_error(
                "CONTEXT_SESSION_MISMATCH",
                "session port returned a resource outside this event identity",
            )
        if skill.ref != event.skill_ref:
            raise _context_error(
                "CONTEXT_SKILL_BINDING_MISMATCH",
                "skill port returned a different certified binding",
            )
        _validate_snapshot_provenance(task.request_context, operation_context, "task")
        _validate_snapshot_provenance(session.request_context, operation_context, "session")
        _validate_snapshot_provenance(skill.request_context, operation_context, "skill")
        return SkillRecoveryContext(event, task, session, skill)

    async def build(
        self,
        event: GameEvent,
        role: RoleId,
        operation_context: OperationContext,
    ) -> TurnContext:
        _validate_operation_identity(event, operation_context)
        role_config = self._role_configs.get(role)
        if event.event_type not in role_config.allowed_events:
            raise _context_error(
                "CONTEXT_EVENT_NOT_ALLOWED",
                "the selected role configuration does not allow this event",
                role=role,
                event_type=event.event_type,
            )

        task = _require_snapshot(
            await self._tasks.get_task(event.task_id, operation_context),
            TaskSnapshot,
            "task",
        )
        session = _require_snapshot(
            await self._sessions.get_session(event.session_id, operation_context),
            SessionSnapshot,
            "session",
        )
        if task.task_id != event.task_id:
            raise _context_error("CONTEXT_TASK_MISMATCH", "task port returned a different task")
        if (
            session.session_id,
            session.student_id,
            session.task_id,
        ) != (
            event.session_id,
            event.student_id,
            event.task_id,
        ):
            raise _context_error(
                "CONTEXT_SESSION_MISMATCH",
                "session port returned a resource outside this event identity",
            )
        _validate_snapshot_provenance(task.request_context, operation_context, "task")
        _validate_snapshot_provenance(session.request_context, operation_context, "session")

        world: WorldSummary | None = None
        skill: SkillSnapshot | None = None
        available_skills: tuple[SkillSnapshot, ...] = ()
        compile_result: CompileResultSnapshot | None = None
        run_result: RunResultSnapshot | None = None
        failure_history: tuple[RunResultSnapshot, ...] = ()
        counterexamples: tuple[CounterexampleSnapshot, ...] = ()
        learner_profile: LearnerProfileSnapshot | None = None
        recent_messages: tuple[MessageSnapshot, ...] = ()
        session_runs: tuple[RunResultSnapshot, ...] = ()
        skill_history: tuple[SkillVersionSummary, ...] = ()
        patch_authority: SkillPatchAuthority | None = None

        if role in {"world_agent", "xiaohutao"}:
            world_result = await self._worlds.get_snapshot(session.world_id, operation_context)
            if isinstance(world_result, Failure):
                raise AgentDependencyError(
                    "WORLD_CONTEXT_UNAVAILABLE",
                    "WorldPort could not return the canonical snapshot",
                    {"contract_error_code": world_result.error.code},
                )
            if not isinstance(world_result, Success) or not isinstance(
                world_result.value, WorldSnapshot
            ):
                raise _context_error(
                    "CONTEXT_PORT_TYPE_MISMATCH",
                    "WorldPort returned a value outside its Result contract",
                )
            _validate_world_snapshot(world_result.value, event, session, operation_context)
            world = summarize_world(world_result.value)

        if role in {"xiaohutao", "teaching_agent", "bug_agent"}:
            if event.skill_ref is None:
                # Asking for help before building anything is legitimate: at the
                # start of a level the Registry holds no activation, so the
                # teaching roles advise from the task alone rather than refusing
                # the learner outright. Every other turn still requires a binding.
                if event.event_type != "hint_requested":
                    raise _context_error(
                        "CONTEXT_SKILL_REQUIRED",
                        f"{role} requires an exact certified skill binding",
                    )
            else:
                skill = _require_snapshot(
                    await self._skills.get_bound_skill(event.skill_ref, operation_context),
                    SkillSnapshot,
                    "skill",
                )
                if skill.ref != event.skill_ref:
                    raise _context_error(
                        "CONTEXT_SKILL_BINDING_MISMATCH",
                        "skill port returned a different certified binding",
                    )
                _validate_snapshot_provenance(skill.request_context, operation_context, "skill")

        if role == "xiaohutao":
            available_skills = _require_snapshot_sequence(
                await self._skills.list_active_skills(event.student_id, operation_context),
                SkillSnapshot,
                "available_skills",
                maximum=32,
            )
            for item in available_skills:
                _validate_snapshot_provenance(
                    item.request_context,
                    operation_context,
                    "active skill",
                )
            refs = [item.ref for item in available_skills]
            bound_matches = [item for item in available_skills if item.ref == event.skill_ref]
            if len(refs) != len(set(refs)) or len(bound_matches) != 1 or bound_matches[0] != skill:
                raise _context_error(
                    "CONTEXT_ACTIVE_SKILLS_MISMATCH",
                    "active list does not contain the exact bound Skill snapshot once",
                )

        if role == "teaching_agent":
            if event.event_type == "skill_patch_requested":
                declared_draft = event.patch_draft_authority
                if declared_draft is None or self._drafts is None:
                    raise _context_error(
                        "CONTEXT_PATCH_DRAFT_REQUIRED",
                        "Skill Patch requires the exact current Draft read boundary",
                    )
                if self._interactions is None:
                    raise _context_error(
                        "CONTEXT_PATCH_INTERACTION_REQUIRED",
                        "Skill Patch requires the selected failed Interaction read boundary",
                    )
                if event.build_id is None or event.run_id is None:
                    raise _context_error(
                        "CONTEXT_PATCH_FAILURE_REQUIRED",
                        "Skill Patch requires exact failed Build and Run identities",
                    )
                requested_interaction_id = cast(
                    str,
                    event.payload["requested_interaction_id"],
                )
                selected_interaction = _require_snapshot(
                    await self._interactions.get_current_failed_interaction(
                        event.session_id,
                        requested_interaction_id,
                        operation_context,
                    ),
                    FailedInteractionSnapshot,
                    "selected_interaction",
                )
                _validate_snapshot_provenance(
                    selected_interaction.request_context,
                    operation_context,
                    "selected interaction",
                )
                draft = _require_snapshot(
                    await self._drafts.get_current_draft(
                        event.session_id,
                        declared_draft.draft_id,
                        operation_context,
                    ),
                    DraftSnapshot,
                    "draft",
                )
                _validate_snapshot_provenance(
                    draft.request_context,
                    operation_context,
                    "draft",
                )
                if draft.authority != declared_draft:
                    raise _context_error(
                        "CONTEXT_PATCH_DRAFT_DRIFT",
                        "current Draft revision/hash/entrypoint no longer match the request",
                    )
                compile_result = _require_snapshot(
                    await self._runs.get_compile_result(event.build_id, operation_context),
                    CompileResultSnapshot,
                    "compile_result",
                )
                run_result = _require_snapshot(
                    await self._runs.get_run(event.run_id, operation_context),
                    RunResultSnapshot,
                    "run_result",
                )
                _validate_snapshot_provenance(
                    compile_result.request_context,
                    operation_context,
                    "compile result",
                )
                _validate_snapshot_provenance(
                    run_result.request_context,
                    operation_context,
                    "run result",
                )
                _validate_run_identity(run_result, event, session)
                if (
                    selected_interaction.interaction_id != requested_interaction_id
                    or selected_interaction.session_id != event.session_id
                    or selected_interaction.task_id != event.task_id
                    or selected_interaction.world_id != session.world_id
                    or selected_interaction.turn_id != run_result.turn_id
                    or selected_interaction.command_id != run_result.command_id
                    or selected_interaction.run_id != event.run_id
                    or selected_interaction.build_id != event.build_id
                    or selected_interaction.skill_ref != event.skill_ref
                    or selected_interaction.failure_count != event.failure_count
                    or selected_interaction.failure_key != event.failure_key
                    or selected_interaction.evidence_refs != event.evidence_refs
                    or compile_result.build_id != event.build_id
                    or not compile_result.succeeded
                    or compile_result.skill_ref != event.skill_ref
                    or compile_result.draft_authority != declared_draft
                    or run_result.build_id != event.build_id
                    or run_result.task_success
                    or run_result.failure_key != event.failure_key
                    or run_result.evidence_refs != event.evidence_refs
                    or run_result.world_revision_after != event.expected_world_revision
                    or run_result.world_commit is not None
                ):
                    raise _context_error(
                        "CONTEXT_PATCH_AUTHORITY_MISMATCH",
                        "Draft, Build, failed Run and Evidence are not one immutable provenance chain",
                    )
                if (
                    skill is None
                    or skill.entrypoint != declared_draft.entrypoint
                    or skill.source_sha256 != declared_draft.entrypoint_sha256
                    or skill.source_code != draft.source_code
                ):
                    raise _context_error(
                        "CONTEXT_PATCH_SOURCE_MISMATCH",
                        "canonical entrypoint source does not match the failed Draft authority",
                    )
                patch_authority = SkillPatchAuthority(
                    draft=draft,
                    request=SkillPatchRequestAuthority(
                        tenant_id=operation_context.actor.tenant_id,
                        actor_id=operation_context.actor.actor_id,
                        actor_type=operation_context.actor.actor_type,
                        session_id=event.session_id,
                        task_id=event.task_id,
                        turn_id=event.turn_id,
                        command_id=event.command_id,
                        requested_interaction_id=requested_interaction_id,
                    ),
                    failed=SkillPatchFailureAuthority(
                        tenant_id=operation_context.actor.tenant_id,
                        actor_id=operation_context.actor.actor_id,
                        session_id=selected_interaction.session_id,
                        interaction_id=selected_interaction.interaction_id,
                        interaction_revision=selected_interaction.interaction_revision,
                        interaction_sequence=selected_interaction.interaction_sequence,
                        same_failure_suffix_end_sequence=(
                            selected_interaction.same_failure_suffix_end_sequence
                        ),
                        turn_id=selected_interaction.turn_id,
                        command_id=selected_interaction.command_id,
                        task_id=selected_interaction.task_id,
                        world_id=selected_interaction.world_id,
                        skill_ref=selected_interaction.skill_ref,
                        failure_count=selected_interaction.failure_count,
                        failure_key=selected_interaction.failure_key,
                        build_id=event.build_id,
                        run_id=event.run_id,
                        evidence_refs=event.evidence_refs,
                        feedback_event_id=selected_interaction.feedback_event_id,
                        projection_receipt_id=selected_interaction.projection_receipt_id,
                    ),
                )
            elif event.event_type == "compile_failed":
                if event.build_id is None:
                    raise _context_error(
                        "CONTEXT_BUILD_REQUIRED", "compile_failed requires build_id"
                    )
                compile_result = _require_snapshot(
                    await self._runs.get_compile_result(event.build_id, operation_context),
                    CompileResultSnapshot,
                    "compile_result",
                )
                _validate_snapshot_provenance(
                    compile_result.request_context,
                    operation_context,
                    "compile result",
                )
                if compile_result.build_id != event.build_id or compile_result.succeeded:
                    raise _context_error(
                        "CONTEXT_COMPILE_MISMATCH",
                        "compile port returned a different or successful build",
                    )
                if event.skill_ref is None or compile_result.skill_ref != event.skill_ref:
                    raise _context_error(
                        "CONTEXT_SKILL_BINDING_MISMATCH",
                        "compile result belongs to a different skill",
                    )
                compile_evidence = {item.evidence_id: item for item in compile_result.evidence_refs}
                for evidence in event.evidence_refs:
                    if compile_evidence.get(evidence.evidence_id) != evidence:
                        raise _context_error(
                            "CONTEXT_EVIDENCE_MISMATCH",
                            "event evidence is absent or relabelled in the exact compile result",
                            evidence_id=evidence.evidence_id,
                        )
            elif event.run_id is not None:
                run_result = _require_snapshot(
                    await self._runs.get_run(event.run_id, operation_context),
                    RunResultSnapshot,
                    "run_result",
                )
                _validate_snapshot_provenance(
                    run_result.request_context,
                    operation_context,
                    "run result",
                )
                _validate_run_identity(run_result, event, session)
                if event.event_type == "run_failed" and (
                    run_result.task_success or run_result.failure_key != event.failure_key
                ):
                    raise _context_error(
                        "CONTEXT_FAILURE_KEY_MISMATCH",
                        "run_failed must reference an unsuccessful Run with the same failure key",
                    )
            learner_profile = _require_snapshot(
                await self._learners.get_profile(
                    event.student_id,
                    task.knowledge_points,
                    operation_context,
                ),
                LearnerProfileSnapshot,
                "learner_profile",
            )
            if learner_profile.student_id != event.student_id:
                raise _context_error(
                    "CONTEXT_LEARNER_MISMATCH",
                    "learner profile belongs to a different student",
                )
            _validate_snapshot_provenance(
                learner_profile.request_context,
                operation_context,
                "learner profile",
            )
            if not set(learner_profile.competencies).issubset(task.knowledge_points):
                raise _context_error(
                    "CONTEXT_LEARNER_SCOPE_MISMATCH",
                    "learner profile contains concepts outside the current task",
                )
            if event.event_type != "skill_patch_requested":
                recent_messages = _require_snapshot_sequence(
                    await self._messages.list_recent(event.session_id, 8, operation_context),
                    MessageSnapshot,
                    "recent_messages",
                    maximum=8,
                )
                if any(item.session_id != event.session_id for item in recent_messages):
                    raise _context_error(
                        "CONTEXT_MESSAGE_MISMATCH",
                        "recent messages contain a different session",
                    )
                for item in recent_messages:
                    _validate_snapshot_provenance(
                        item.request_context,
                        operation_context,
                        "recent message",
                    )

        if role == "bug_agent":
            if event.run_id is None or event.failure_key is None:
                raise _context_error(
                    "CONTEXT_FAILURE_EVIDENCE_REQUIRED",
                    "bug_agent requires an exact run and same-failure key",
                )
            run_result = _require_snapshot(
                await self._runs.get_run(event.run_id, operation_context),
                RunResultSnapshot,
                "run_result",
            )
            _validate_snapshot_provenance(
                run_result.request_context,
                operation_context,
                "run result",
            )
            _validate_run_identity(run_result, event, session)
            if run_result.task_success or run_result.failure_key != event.failure_key:
                raise _context_error(
                    "CONTEXT_FAILURE_KEY_MISMATCH",
                    "current run is not the declared reproducible failure",
                )
            history_limit = event.failure_count + 1
            failure_history = _require_snapshot_sequence(
                await self._runs.list_same_failure_runs(
                    event.session_id,
                    event.failure_key,
                    event.run_id,
                    history_limit,
                    operation_context,
                ),
                RunResultSnapshot,
                "failure_history",
                maximum=history_limit,
            )
            if len(failure_history) != event.failure_count:
                raise _context_error(
                    "CONTEXT_FAILURE_HISTORY_COUNT_MISMATCH",
                    "bug_agent failure count is not the exact canonical same-class suffix",
                    required=event.failure_count,
                    actual=len(failure_history),
                )
            run_ids: set[str] = set()
            turn_ids: set[str] = set()
            command_ids: set[str] = set()
            for item in failure_history:
                _validate_snapshot_provenance(
                    item.request_context,
                    operation_context,
                    "failure history run",
                )
                if (
                    item.session_id != event.session_id
                    or item.world_id != session.world_id
                    or item.failure_key != event.failure_key
                    or item.task_success
                    or item.skill_ref != event.skill_ref
                ):
                    raise _context_error(
                        "CONTEXT_FAILURE_HISTORY_MISMATCH",
                        "failure history contains an unrelated run",
                    )
                if (
                    item.run_id in run_ids
                    or item.turn_id in turn_ids
                    or item.command_id in command_ids
                ):
                    raise _context_error(
                        "CONTEXT_FAILURE_HISTORY_DUPLICATE",
                        "failure history duplicates a run, turn or command",
                    )
                run_ids.add(item.run_id)
                turn_ids.add(item.turn_id)
                command_ids.add(item.command_id)
            if event.run_id not in run_ids:
                raise _context_error(
                    "CONTEXT_FAILURE_HISTORY_MISMATCH",
                    "failure history does not include the current run",
                )
            if failure_history[-1].run_id != event.run_id:
                raise _context_error(
                    "CONTEXT_FAILURE_HISTORY_ORDER_MISMATCH",
                    "same-failure history is not ordered through the current run",
                )
            history_current = next(item for item in failure_history if item.run_id == event.run_id)
            if history_current != run_result:
                raise _context_error(
                    "CONTEXT_RUN_FACT_COLLISION",
                    "current run has conflicting facts in the failure history",
                )
            counterexamples = _require_snapshot_sequence(
                await self._counterexamples.list_counterexamples(
                    event.task_id,
                    event.failure_key,
                    operation_context,
                ),
                CounterexampleSnapshot,
                "counterexamples",
                maximum=20,
            )
            if any(
                item.failure_key != event.failure_key or item.task_id != event.task_id
                for item in counterexamples
            ):
                raise _context_error(
                    "CONTEXT_COUNTEREXAMPLE_MISMATCH",
                    "counterexample port returned a different failure class",
                )
            for item in counterexamples:
                _validate_snapshot_provenance(
                    item.request_context,
                    operation_context,
                    "counterexample",
                )

        if role == "book_agent":
            if event.run_id is None:
                raise _context_error(
                    "CONTEXT_RUN_REQUIRED", "book_agent requires completion run_id"
                )
            run_result = _require_snapshot(
                await self._runs.get_run(event.run_id, operation_context),
                RunResultSnapshot,
                "run_result",
            )
            _validate_snapshot_provenance(
                run_result.request_context,
                operation_context,
                "run result",
            )
            _validate_run_identity(run_result, event, session)
            if not run_result.task_success:
                raise _context_error(
                    "CONTEXT_COMPLETION_MISMATCH",
                    "task_completed cannot reference an unsuccessful run",
                )
            session_runs = _require_snapshot_sequence(
                await self._runs.list_session_runs(
                    event.session_id,
                    event.run_id,
                    operation_context,
                ),
                RunResultSnapshot,
                "session_runs",
                maximum=200,
            )
            run_ids = {item.run_id for item in session_runs}
            turn_ids = {item.turn_id for item in session_runs}
            command_ids = {item.command_id for item in session_runs}
            for item in session_runs:
                _validate_snapshot_provenance(
                    item.request_context,
                    operation_context,
                    "session run",
                )
            if (
                len(run_ids) != len(session_runs)
                or len(turn_ids) != len(session_runs)
                or len(command_ids) != len(session_runs)
                or event.run_id not in run_ids
                or not session_runs
                or session_runs[-1].run_id != event.run_id
                or any(
                    item.session_id != event.session_id or item.world_id != session.world_id
                    for item in session_runs
                )
            ):
                raise _context_error(
                    "CONTEXT_SESSION_RUNS_MISMATCH",
                    "session run history is incomplete or contains another session",
                )
            # Solving a task on the first try used to be rejected here, because a
            # growth summary was assumed to need a failure to contrast against.
            # That punished the learners who did best: the Turn dead-lettered,
            # which stranded the client envelope and left them unable to run or
            # ask for a hint at all. A first-try success is still a real result,
            # and book_agent summarizes it from the completion Run.
            session_current = next(item for item in session_runs if item.run_id == event.run_id)
            if session_current != run_result:
                raise _context_error(
                    "CONTEXT_RUN_FACT_COLLISION",
                    "completion run has conflicting facts in the session history",
                )
            skill_ref = event.skill_ref or run_result.skill_ref
            skill_history = _require_snapshot_sequence(
                await self._skills.list_skill_history(
                    skill_ref.skill_id,
                    event.session_id,
                    operation_context,
                ),
                SkillVersionSummary,
                "skill_history",
                maximum=100,
            )
            history_versions = {item.skill_version_id for item in skill_history}
            for item in skill_history:
                _validate_snapshot_provenance(
                    item.request_context,
                    operation_context,
                    "skill history",
                )
            if (
                not skill_history
                or len(history_versions) != len(skill_history)
                or skill_ref.skill_version_id not in history_versions
                or any(
                    item.skill_id != skill_ref.skill_id or item.session_id != event.session_id
                    for item in skill_history
                )
            ):
                raise _context_error(
                    "CONTEXT_SKILL_HISTORY_MISMATCH",
                    "skill history is duplicated, incomplete or contains a different skill",
                )
            learner_profile = _require_snapshot(
                await self._learners.get_profile(
                    event.student_id,
                    task.knowledge_points,
                    operation_context,
                ),
                LearnerProfileSnapshot,
                "learner_profile",
            )
            if learner_profile.student_id != event.student_id:
                raise _context_error(
                    "CONTEXT_LEARNER_MISMATCH",
                    "learner profile belongs to a different student",
                )
            _validate_snapshot_provenance(
                learner_profile.request_context,
                operation_context,
                "learner profile",
            )
            if not set(learner_profile.competencies).issubset(task.knowledge_points):
                raise _context_error(
                    "CONTEXT_LEARNER_SCOPE_MISMATCH",
                    "learner profile contains concepts outside the current task",
                )

        if role != "xiaohutao" and learner_profile is None:
            learner_profile = _require_snapshot(
                await self._learners.get_profile(
                    event.student_id,
                    task.knowledge_points,
                    operation_context,
                ),
                LearnerProfileSnapshot,
                "learner_profile",
            )
            if learner_profile.student_id != event.student_id:
                raise _context_error(
                    "CONTEXT_LEARNER_MISMATCH",
                    "learner profile belongs to a different student",
                )
            _validate_snapshot_provenance(
                learner_profile.request_context,
                operation_context,
                "learner profile",
            )
            if not set(learner_profile.competencies).issubset(task.knowledge_points):
                raise _context_error(
                    "CONTEXT_LEARNER_SCOPE_MISMATCH",
                    "learner profile contains concepts outside the current task",
                )

        directive = None
        if role != "xiaohutao":
            if learner_profile is None:
                raise AssertionError("directive-bearing role lost its learner profile")
            try:
                directive = self._pedagogy_policy.decide(
                    PedagogyInput(
                        role=role,
                        event_type=event.event_type,
                        failure_count=event.failure_count,
                        hint_requested=event.event_type == "hint_requested",
                        teaching_spec_version=self._teaching_spec_version,
                        task_concepts=task.knowledge_points,
                        max_hint_level=task.max_hint_level,
                        learner_revision=learner_profile.revision,
                        learner_competencies=_competency_summaries(learner_profile),
                        learner_evidence_ids=tuple(
                            item.evidence_id for item in learner_profile.evidence_refs
                        ),
                        current_validated_evidence=_pedagogy_evidence(event),
                        event_time=event.occurred_at,
                        explicit_skill_patch_request=(
                            event.event_type == "skill_patch_requested"
                            and event.payload.get("source_event_type") == "UI_ACTION"
                            and event.payload.get("action_id") == "request_ai_patch"
                        ),
                        skill_patch_feature_enabled=event.skill_patch_feature_enabled,
                        skill_patch_capability_enabled=event.skill_patch_capability_enabled,
                        draft_authority_validated=patch_authority is not None,
                    )
                )
            except (PedagogyPolicyError, TypeError, ValueError) as error:
                raise _context_error(
                    "CONTEXT_PEDAGOGY_POLICY_REJECTED",
                    "trusted facts could not produce a TeachingDirective",
                    role=role,
                    event_type=event.event_type,
                ) from error
            if directive is None:
                raise AssertionError("directive-bearing role produced no TeachingDirective")

        hint_level = (
            calculate_hint_level(
                event.failure_count,
                requested_hint=event.event_type == "hint_requested",
                maximum=task.max_hint_level,
            )
            if directive is None
            else directive.hint_level
        )
        turn_context = TurnContext(
            role=role,
            event=event,
            task=task,
            session=session,
            hint_level=hint_level,
            world=world,
            skill=skill,
            available_skills=available_skills,
            compile_result=compile_result,
            run_result=run_result,
            failure_history=failure_history,
            counterexamples=counterexamples,
            learner_profile=learner_profile,
            recent_messages=recent_messages,
            session_runs=session_runs,
            skill_history=skill_history,
            teaching_directive=directive,
            patch_authority=patch_authority,
        )
        validate_context_for_role(turn_context)
        return turn_context


def validate_context_for_role(context: TurnContext) -> None:
    """Reject both missing facts and accidental over-fetching before prompting."""

    role = context.role
    if role == "world_agent":
        if context.world is None or context.learner_profile is None:
            raise _context_error(
                "CONTEXT_WORLD_REQUIRED",
                "world_agent requires world summary and learner revision",
            )
        if any(
            (
                context.skill,
                context.compile_result,
                context.run_result,
                context.available_skills,
                context.failure_history,
                context.counterexamples,
                context.recent_messages,
                context.session_runs,
                context.skill_history,
            )
        ):
            raise _context_error(
                "CONTEXT_OVERFETCHED",
                "world_agent context contains data outside its role boundary",
            )
        return
    if role == "xiaohutao":
        if context.world is None or context.skill is None or not context.available_skills:
            raise _context_error(
                "CONTEXT_SKILL_EXECUTION_INCOMPLETE",
                "xiaohutao requires world, bound skill and active skill list",
            )
        if any(
            (
                context.compile_result,
                context.run_result,
                context.failure_history,
                context.counterexamples,
                context.learner_profile,
                context.recent_messages,
                context.session_runs,
                context.skill_history,
            )
        ):
            raise _context_error(
                "CONTEXT_OVERFETCHED",
                "xiaohutao context contains unrelated teaching or history data",
            )
        return
    if role == "teaching_agent":
        # A hint raised before the learner has built anything has no certified
        # source to reason about; every other teaching turn still requires it.
        skill_optional = context.event.event_type == "hint_requested"
        if (context.skill is None and not skill_optional) or context.learner_profile is None:
            raise _context_error(
                "CONTEXT_TEACHING_INCOMPLETE",
                "teaching_agent requires bound source and learner projection",
            )
        if context.event.event_type == "compile_failed" and context.compile_result is None:
            raise _context_error(
                "CONTEXT_COMPILE_REQUIRED",
                "compile_failed teaching requires the exact compile result",
            )
        if context.event.event_type == "run_failed" and context.run_result is None:
            raise _context_error(
                "CONTEXT_RUN_REQUIRED",
                "run_failed teaching requires the exact run result",
            )
        if context.event.event_type == "skill_patch_requested" and (
            context.patch_authority is None
            or context.compile_result is None
            or context.run_result is None
            or context.hint_level != 4
            or context.teaching_directive is None
            or not context.teaching_directive.patch_eligible
        ):
            raise _context_error(
                "CONTEXT_PATCH_AUTHORITY_INCOMPLETE",
                "Skill Patch teaching requires exact Draft/Build/Run/Evidence/L4 authority",
            )
        if any(
            (
                context.world,
                context.available_skills,
                context.failure_history,
                context.counterexamples,
                context.session_runs,
                context.skill_history,
            )
        ):
            raise _context_error(
                "CONTEXT_OVERFETCHED",
                "teaching_agent context contains unrelated role data",
            )
        return
    if role == "bug_agent":
        if (
            context.skill is None
            or context.run_result is None
            or context.learner_profile is None
            or len(context.failure_history) < 3
        ):
            raise _context_error(
                "CONTEXT_BUG_EVIDENCE_INCOMPLETE",
                "bug_agent requires source, current run and repeated canonical failures",
            )
        if any(
            (
                context.world,
                context.available_skills,
                context.compile_result,
                context.recent_messages,
                context.session_runs,
                context.skill_history,
            )
        ):
            raise _context_error(
                "CONTEXT_OVERFETCHED",
                "bug_agent context contains unrelated role data",
            )
        return
    if role == "book_agent":
        if (
            context.run_result is None
            or not context.session_runs
            or not context.skill_history
            or context.learner_profile is None
        ):
            raise _context_error(
                "CONTEXT_BOOK_HISTORY_INCOMPLETE",
                "book_agent requires completion run, session runs, skill history and learner projection",
            )
        if any(
            (
                context.world,
                context.skill,
                context.available_skills,
                context.compile_result,
                context.failure_history,
                context.counterexamples,
                context.recent_messages,
            )
        ):
            raise _context_error(
                "CONTEXT_OVERFETCHED",
                "book_agent context contains unrelated role data",
            )
        return
    raise AssertionError(f"unreachable validated role: {role}")


__all__ = ["ContextBuilder", "summarize_world", "validate_context_for_role"]
