"""Backend-owned read adapters for the provider-neutral Agent runtime."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_build import canonical_source_bundle_sha256
from yaya_agent_contracts import (
    EvidenceRef,
    EvidenceType,
    OperationContext,
    RequestContext,
    SkillRef,
    canonical_json_sha256,
)
from yaya_agent_runtime import (
    AgentTraceEvent,
    CompileResultSnapshot,
    CounterexampleSnapshot,
    DraftAuthority,
    DraftSnapshot,
    FailedInteractionSnapshot,
    LearnerProfileSnapshot,
    MessageSnapshot,
    RunResultSnapshot,
    SessionSnapshot,
    SkillSnapshot,
    SkillVersionSummary,
    TaskSnapshot,
)

from walnut_backend.certified_skill_schema import (
    CertifiedSkillSchemaError,
    validated_certified_parameter_schema,
)

from .activation_authority import load_current_activation_authority
from .agent_trace_identity import (
    AGENT_TRACE_OPERATION as _AGENT_TRACE_OPERATION,
)
from .agent_trace_identity import (
    AGENT_TRACE_OUTCOME as _AGENT_TRACE_OUTCOME,
)
from .agent_trace_identity import (
    AgentTraceIdentityError,
    agent_trace_audit_id,
)
from .models import (
    AgentSessionRow,
    AgentTurnRow,
    AuditRow,
    BuildPolicyRow,
    CurrentSessionBindingRow,
    LaunchAuthorityRow,
    ProductContentUnitRow,
    ProductDraftRevisionRow,
    ProductDraftRow,
    ProductInteractionRow,
    RegistryEntryRow,
    RegistryHeadRow,
    RunRow,
    SkillActivationRow,
    SkillArtifactRow,
    SkillBuildProvenanceRow,
    SkillBuildRow,
    SkillCertificationRevocationRow,
    SkillCertificationRow,
    SkillRunProvenanceRow,
    json_value,
    request_context_from_data,
)
from .product_interactions import _interactions_have_authority
from .run_outcomes import (
    exact_failure_suffix_count,
    list_validated_session_runs,
    load_validated_run,
    validate_terminal_projection,
)
from .session_binding_authority import (
    current_session_binding_matches,
    current_session_binding_observed_at,
)
from .skill_provenance import validate_run_provenance
from .workflow_jobs import WorkflowInvariantError


class AgentRuntimeAuthorityError(RuntimeError):
    pass


class PostgresAgentRuntimeReads:
    """One adapter implementing the narrow xiaohutao context read Ports."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def get_task(self, task_id: str, context: OperationContext) -> TaskSnapshot:
        async with self._sessions() as session:
            content = await _content(session, context)
        task = _object(content.content_json.get("task"), "Content task")
        if task.get("task_id") != task_id:
            raise AgentRuntimeAuthorityError("task_id differs from pinned ContentUnit")
        story = _object(task.get("story"), "Content task story")
        hint = _object(task.get("hint_policy"), "Content hint policy")
        return TaskSnapshot(
            task_id=task_id,
            title=_text(task, "name"),
            goal=_text(task, "goal"),
            story=cast(str, story.get("opening", "")),
            knowledge_points=_strings(task.get("knowledge_points"), "knowledge_points"),
            request_context=_request_context(context),
            max_hint_level=_int(hint, "max_level"),
        )

    async def get_session(self, session_id: str, context: OperationContext) -> SessionSnapshot:
        async with self._sessions() as session:
            _, row, _ = await _current_session_binding_authority(
                session,
                context,
                session_id=session_id,
            )
            content = await _content(session, context)
        wire = dict(row.session_json)
        origin = _wire_context(wire)
        task = _object(content.content_json.get("task"), "Content task")
        if (
            wire.get("session_id") != row.session_id
            or wire.get("world_id") != row.world_id
            or wire.get("learner_id") != context.actor.actor_id
            or wire.get("status") != "ACTIVE"
        ):
            raise AgentRuntimeAuthorityError("Agent Session durable identity drifted")
        return SessionSnapshot(
            session_id=row.session_id,
            student_id=context.actor.actor_id,
            task_id=_text(task, "task_id"),
            world_id=row.world_id,
            request_context=origin,
        )

    async def get_bound_skill(
        self, skill_ref: SkillRef, context: OperationContext
    ) -> SkillSnapshot:
        async with self._sessions() as session:
            return await self._skill(session, skill_ref, context, require_active=True)

    async def list_active_skills(
        self, student_id: str, context: OperationContext
    ) -> tuple[SkillSnapshot, ...]:
        if student_id != context.actor.actor_id:
            raise AgentRuntimeAuthorityError("student differs from authenticated actor")
        async with self._sessions() as session:
            binding, _, _ = await _current_session_binding_authority(session, context)
            head = await session.scalar(
                select(RegistryHeadRow).where(
                    RegistryHeadRow.tenant_id == context.actor.tenant_id,
                    RegistryHeadRow.actor_id == binding.actor_id,
                    RegistryHeadRow.content_hash == binding.content_hash,
                    RegistryHeadRow.world_id == binding.world_id,
                    RegistryHeadRow.agent_profile_id == binding.agent_profile_id,
                    RegistryHeadRow.authority_id == binding.authority_id,
                )
            )
            if head is None or head.revision < 1:
                return ()
            activation = await session.scalar(
                select(SkillActivationRow).where(
                    SkillActivationRow.tenant_id == context.actor.tenant_id,
                    SkillActivationRow.actor_id == binding.actor_id,
                    SkillActivationRow.content_hash == binding.content_hash,
                    SkillActivationRow.world_id == binding.world_id,
                    SkillActivationRow.agent_profile_id == binding.agent_profile_id,
                    SkillActivationRow.registry_revision == head.revision,
                )
            )
            if activation is None:
                raise AgentRuntimeAuthorityError("Registry head has no exact Activation")
            reference = SkillRef(
                activation.skill_id,
                activation.skill_version_id,
                activation.artifact_sha256,
                activation.certification_id,
            )
            try:
                await load_current_activation_authority(
                    session,
                    tenant_id=context.actor.tenant_id,
                    actor_id=binding.actor_id,
                    content_hash=binding.content_hash,
                    world_id=binding.world_id,
                    agent_profile_id=binding.agent_profile_id,
                    authority_id=binding.authority_id,
                    skill_ref=reference,
                )
            except (TypeError, ValueError, WorkflowInvariantError) as error:
                raise AgentRuntimeAuthorityError(
                    "current Skill Activation authority is corrupt"
                ) from error
            return (await self._skill(session, reference, context, require_active=True),)

    async def list_skill_history(
        self,
        skill_id: str,
        session_id: str,
        context: OperationContext,
    ) -> tuple[SkillVersionSummary, ...]:
        async with self._sessions() as session:
            binding, _, _ = await _current_session_binding_authority(
                session,
                context,
                session_id=session_id,
            )
            entries = list(
                (
                    await session.scalars(
                        select(RegistryEntryRow)
                        .where(
                            RegistryEntryRow.tenant_id == context.actor.tenant_id,
                            RegistryEntryRow.actor_id == binding.actor_id,
                            RegistryEntryRow.content_hash == binding.content_hash,
                            RegistryEntryRow.world_id == binding.world_id,
                            RegistryEntryRow.agent_profile_id == binding.agent_profile_id,
                            RegistryEntryRow.skill_id == skill_id,
                        )
                        .order_by(RegistryEntryRow.revision)
                        .limit(100)
                    )
                ).all()
            )
            result: list[SkillVersionSummary] = []
            seen_versions: set[str] = set()
            for entry in entries:
                if entry.skill_version_id in seen_versions:
                    # The same version can legitimately be activated more than
                    # once: a student who edits their code and then changes it
                    # back rebuilds to an identical artifact, so the version id
                    # repeats, and re-activating the current version is allowed
                    # outright. This is a version *history*, so record where each
                    # version first entered the Registry and skip the repeats.
                    #
                    # Rejecting them used to end the Session for good -- history
                    # is replayed on every completed task, so one re-activation
                    # meant the learner could never finish a task again.
                    continue
                seen_versions.add(entry.skill_version_id)
                certification = await session.scalar(
                    select(SkillCertificationRow).where(
                        SkillCertificationRow.tenant_id == context.actor.tenant_id,
                        SkillCertificationRow.actor_id == context.actor.actor_id,
                        SkillCertificationRow.content_hash == context.content_ref.content_hash,
                        SkillCertificationRow.skill_id == entry.skill_id,
                        SkillCertificationRow.skill_version_id == entry.skill_version_id,
                        SkillCertificationRow.certification_id == entry.certification_id,
                        SkillCertificationRow.artifact_sha256 == entry.artifact_sha256,
                    )
                )
                if certification is None or entry.entry_sha256 != canonical_json_sha256(
                    entry.entry_json
                ):
                    raise AgentRuntimeAuthorityError("Skill history Registry entry drifted")
                artifact = await session.scalar(
                    select(SkillArtifactRow).where(
                        SkillArtifactRow.tenant_id == context.actor.tenant_id,
                        SkillArtifactRow.artifact_sha256 == certification.artifact_sha256,
                        SkillArtifactRow.build_id == certification.build_id,
                    )
                )
                if artifact is None:
                    raise AgentRuntimeAuthorityError("Skill history Artifact is missing")
                result.append(
                    SkillVersionSummary(
                        session_id=session_id,
                        skill_id=skill_id,
                        skill_version_id=certification.skill_version_id,
                        source_sha256=artifact.source_sha256,
                        change_summary="Certified student Skill version.",
                        request_context=_request_context(context),
                    )
                )
        return tuple(result)

    async def get_compile_result(
        self, build_id: str, context: OperationContext
    ) -> CompileResultSnapshot:
        async with self._sessions() as session:
            build = await session.scalar(
                select(SkillBuildRow).where(
                    SkillBuildRow.tenant_id == context.actor.tenant_id,
                    SkillBuildRow.build_id == build_id,
                    SkillBuildRow.actor_id == context.actor.actor_id,
                )
            )
            provenance = (
                await session.scalar(
                    select(SkillBuildProvenanceRow).where(
                        SkillBuildProvenanceRow.build_id == build_id,
                        SkillBuildProvenanceRow.tenant_id == context.actor.tenant_id,
                        SkillBuildProvenanceRow.actor_id == context.actor.actor_id,
                    )
                )
                if build is not None
                else None
            )
            revision = (
                await session.scalar(
                    select(ProductDraftRevisionRow).where(
                        ProductDraftRevisionRow.draft_revision_row_id
                        == provenance.draft_revision_row_id
                    )
                )
                if provenance is not None
                else None
            )
        if build is None:
            raise AgentRuntimeAuthorityError("Build was not found")
        wire = dict(build.build_json)
        if not wire.get("skill_version_id") or not isinstance(wire.get("artifact"), Mapping):
            raise AgentRuntimeAuthorityError("Build has no certified Skill reference")
        certification = _object(wire.get("certification"), "Build certification")
        artifact = _object(wire.get("artifact"), "Build artifact")
        reference = SkillRef(
            build.skill_id,
            cast(str, wire["skill_version_id"]),
            _text(artifact, "artifact_sha256"),
            _text(certification, "certification_id"),
        )
        diagnostics = tuple(
            code
            for raw in cast(Sequence[object], wire.get("phases", []))
            for code in _strings(_object(raw, "Build phase").get("diagnostic_codes"), "diagnostics")
        )
        refs = _evidence_refs(wire.get("evidence_refs", []))
        draft_authority = None
        if provenance is not None:
            if (
                revision is None
                or revision.tenant_id != provenance.tenant_id
                or revision.actor_id != provenance.actor_id
                or revision.session_id != provenance.session_id
                or revision.draft_id != provenance.draft_id
                or revision.skill_id != provenance.skill_id
                or revision.revision != provenance.draft_revision
                or revision.draft_sha256 != provenance.draft_sha256
                or revision.source_bundle_sha256 != provenance.source_bundle_sha256
            ):
                raise AgentRuntimeAuthorityError("Build Draft provenance drifted")
            source = _object(revision.draft_json.get("source_bundle"), "Draft source bundle")
            files = source.get("files")
            entrypoint = source.get("entrypoint")
            matches = (
                [
                    item
                    for item in cast(Sequence[object], files)
                    if isinstance(item, Mapping) and item.get("path") == entrypoint
                ]
                if isinstance(files, Sequence) and not isinstance(files, str | bytes)
                else []
            )
            if len(matches) != 1:
                raise AgentRuntimeAuthorityError("Build Draft entrypoint is not unique")
            entrypoint_sha256 = _text(matches[0], "content_sha256")
            draft_authority = DraftAuthority(
                draft_id=revision.draft_id,
                session_id=revision.session_id,
                skill_id=revision.skill_id,
                draft_revision=revision.revision,
                draft_sha256=revision.draft_sha256,
                source_bundle_sha256=revision.source_bundle_sha256,
                entrypoint=revision.entrypoint,
                entrypoint_sha256=entrypoint_sha256,
            )
        return CompileResultSnapshot(
            build_id=build_id,
            skill_ref=reference,
            succeeded=wire.get("status") == "CERTIFIED",
            diagnostics=diagnostics,
            evidence_refs=refs,
            request_context=_wire_context(wire),
            draft_authority=draft_authority,
        )

    async def get_run(self, run_id: str, context: OperationContext) -> RunResultSnapshot:
        async with self._sessions() as session:
            row = await session.scalar(
                select(RunRow).where(
                    RunRow.tenant_id == context.actor.tenant_id,
                    RunRow.actor_id == context.actor.actor_id,
                    RunRow.content_hash == context.content_ref.content_hash,
                    RunRow.run_id == run_id,
                )
            )
            if row is None:
                raise AgentRuntimeAuthorityError("Run was not found")
            run_context = replace(
                context,
                command_id=row.command_id,
                causation_id=None,
            )
            try:
                authority = await load_validated_run(
                    session,
                    tenant_id=context.actor.tenant_id,
                    actor_id=context.actor.actor_id,
                    content_hash=context.content_ref.content_hash,
                    command_id=row.command_id,
                    expected_context=run_context,
                    require_current_world=True,
                )
            except RuntimeError as error:
                raise AgentRuntimeAuthorityError(str(error)) from error
        if authority.run.run_id != run_id:
            raise AgentRuntimeAuthorityError("Run identity changed during closure")
        return authority.run

    async def get_current_draft(
        self,
        session_id: str,
        draft_id: str,
        context: OperationContext,
    ) -> DraftSnapshot:
        async with self._sessions() as session:
            current = await session.scalar(
                select(ProductDraftRow).where(
                    ProductDraftRow.tenant_id == context.actor.tenant_id,
                    ProductDraftRow.actor_id == context.actor.actor_id,
                    ProductDraftRow.session_id == session_id,
                    ProductDraftRow.draft_id == draft_id,
                )
            )
            revision = (
                await session.scalar(
                    select(ProductDraftRevisionRow).where(
                        ProductDraftRevisionRow.tenant_id == context.actor.tenant_id,
                        ProductDraftRevisionRow.actor_id == context.actor.actor_id,
                        ProductDraftRevisionRow.session_id == session_id,
                        ProductDraftRevisionRow.draft_id == draft_id,
                        ProductDraftRevisionRow.revision == current.revision,
                        ProductDraftRevisionRow.draft_sha256 == current.draft_sha256,
                    )
                )
                if current is not None
                else None
            )
        if current is None or revision is None or revision.draft_json != current.draft_json:
            raise AgentRuntimeAuthorityError("current Draft authority is missing or drifted")
        source = _object(current.draft_json.get("source_bundle"), "Draft source bundle")
        files = source.get("files")
        if not isinstance(files, Sequence) or isinstance(files, str | bytes):
            raise AgentRuntimeAuthorityError("Draft source files are invalid")
        matches = [
            item
            for item in files
            if isinstance(item, Mapping) and item.get("path") == revision.entrypoint
        ]
        if len(matches) != 1:
            raise AgentRuntimeAuthorityError("Draft entrypoint is not unique")
        entry = cast(Mapping[str, Any], matches[0])
        content = _text(entry, "content")
        content_sha256 = _text(entry, "content_sha256")
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != content_sha256:
            raise AgentRuntimeAuthorityError("Draft entrypoint content hash drifted")
        return DraftSnapshot(
            authority=DraftAuthority(
                draft_id=revision.draft_id,
                session_id=revision.session_id,
                skill_id=revision.skill_id,
                draft_revision=revision.revision,
                draft_sha256=revision.draft_sha256,
                source_bundle_sha256=revision.source_bundle_sha256,
                entrypoint=revision.entrypoint,
                entrypoint_sha256=content_sha256,
            ),
            source_code=content,
            request_context=_wire_context(current.draft_json),
        )

    async def get_current_failed_interaction(
        self,
        session_id: str,
        interaction_id: str,
        context: OperationContext,
    ) -> FailedInteractionSnapshot:
        """Return only the current, terminal, canonically failed Interaction.

        The session-local Product Interaction sequence is the independent
        latestness authority.  The selected row must be the high watermark;
        its canonical OUTCOME_DERIVED receipt then re-closes the exact
        same-failure suffix, Run, Build and Evidence before any Provider call.
        """

        async with self._sessions() as session, session.begin():
            row = await session.scalar(
                select(ProductInteractionRow).where(
                    ProductInteractionRow.tenant_id == context.actor.tenant_id,
                    ProductInteractionRow.actor_id == context.actor.actor_id,
                    ProductInteractionRow.session_id == session_id,
                    ProductInteractionRow.interaction_id == interaction_id,
                )
            )
            owner = await session.scalar(
                select(AgentSessionRow).where(
                    AgentSessionRow.tenant_id == context.actor.tenant_id,
                    AgentSessionRow.actor_id == context.actor.actor_id,
                    AgentSessionRow.session_id == session_id,
                    AgentSessionRow.status == "ACTIVE",
                )
            )
            high = await session.scalar(
                select(func.max(ProductInteractionRow.sequence)).where(
                    ProductInteractionRow.tenant_id == context.actor.tenant_id,
                    ProductInteractionRow.actor_id == context.actor.actor_id,
                    ProductInteractionRow.session_id == session_id,
                )
            )
            if (
                row is None
                or owner is None
                or row.sequence != high
                or row.interaction_revision != 1
                or not await _interactions_have_authority(session, [row], owner)
            ):
                raise AgentRuntimeAuthorityError(
                    "selected Interaction is stale, decided, or corrupt"
                )
            interaction_value = row.interaction_json
            role = interaction_value.get("role")
            response_type = interaction_value.get("response_type")
            hint_level = interaction_value.get("hint_level")
            if (
                role not in {"teaching_agent", "bug_agent"}
                or response_type not in {"question", "hint", "message"}
                or isinstance(hint_level, bool)
                or (
                    hint_level is not None
                    and (not isinstance(hint_level, int) or not 0 <= hint_level <= 3)
                )
            ):
                raise AgentRuntimeAuthorityError(
                    "selected Interaction is not an eligible failed student response"
                )
            feedback = _object(row.interaction_json.get("feedback"), "Interaction feedback")
            feedback_event = _object(
                row.interaction_json.get("feedback_event"),
                "Interaction feedback event",
            )
            source = _object(
                row.interaction_json.get("projection_source"),
                "Interaction projection source",
            )
            run_id = _text(feedback, "run_id")
            run_row = await session.scalar(
                select(RunRow).where(
                    RunRow.tenant_id == context.actor.tenant_id,
                    RunRow.actor_id == context.actor.actor_id,
                    RunRow.session_id == session_id,
                    RunRow.run_id == run_id,
                )
            )
            if run_row is None:
                raise AgentRuntimeAuthorityError("selected Interaction Run disappeared")
            failure_context = replace(
                context,
                command_id=run_row.command_id,
                causation_id=None,
            )
            try:
                authority = await load_validated_run(
                    session,
                    tenant_id=context.actor.tenant_id,
                    actor_id=context.actor.actor_id,
                    content_hash=context.content_ref.content_hash,
                    command_id=run_row.command_id,
                    expected_context=failure_context,
                    require_current_world=True,
                )
                await validate_terminal_projection(session, authority)
                failure_count = await exact_failure_suffix_count(
                    session,
                    current=authority,
                    context=authority.context,
                    current_must_be_live=False,
                )
            except RuntimeError as error:
                raise AgentRuntimeAuthorityError(str(error)) from error
            provenance = await session.scalar(
                select(SkillRunProvenanceRow).where(
                    SkillRunProvenanceRow.run_id == run_id,
                    SkillRunProvenanceRow.tenant_id == context.actor.tenant_id,
                    SkillRunProvenanceRow.actor_id == context.actor.actor_id,
                    SkillRunProvenanceRow.session_id == session_id,
                )
            )
            if (
                authority.run.run_id != run_id
                or authority.run.task_success
                or authority.run.failure_key is None
                or provenance is None
                or await validate_run_provenance(session, provenance, require_immutable=True)
                is None
            ):
                raise AgentRuntimeAuthorityError(
                    "selected Interaction is not one failed Run/Build authority"
                )
            task = _object(
                (await _content(session, context)).content_json.get("task"),
                "Content task",
            )
            return FailedInteractionSnapshot(
                interaction_id=row.interaction_id,
                interaction_revision=row.interaction_revision,
                interaction_sequence=row.sequence,
                same_failure_suffix_end_sequence=row.sequence,
                session_id=row.session_id,
                turn_id=authority.run.turn_id,
                command_id=authority.run.command_id,
                run_id=authority.run.run_id,
                build_id=provenance.build_id,
                task_id=_text(task, "task_id"),
                world_id=authority.run.world_id,
                skill_ref=authority.run.skill_ref,
                failure_count=failure_count,
                failure_key=authority.run.failure_key,
                evidence_refs=authority.run.evidence_refs,
                feedback_event_id=_text(feedback_event, "event_id"),
                projection_receipt_id=_text(source, "receipt_id"),
                request_context=_request_context(authority.context),
            )

    async def list_same_failure_runs(
        self,
        session_id: str,
        failure_key: str,
        through_run_id: str,
        limit: int,
        context: OperationContext,
    ) -> tuple[RunResultSnapshot, ...]:
        if limit < 1:
            return ()
        async with self._sessions() as session:
            try:
                history = await list_validated_session_runs(
                    session,
                    session_id=session_id,
                    through_run_id=through_run_id,
                    context=context,
                )
            except RuntimeError as error:
                raise AgentRuntimeAuthorityError(str(error)) from error
        suffix: list[RunResultSnapshot] = []
        for run in reversed(history):
            if run.task_success or run.failure_key != failure_key:
                break
            if suffix and (
                run.skill_ref != suffix[-1].skill_ref or run.world_id != suffix[-1].world_id
            ):
                break
            suffix.append(run)
        result = tuple(reversed(suffix))
        if not result or result[-1].run_id != through_run_id or len(result) > limit:
            raise AgentRuntimeAuthorityError("same-failure Run suffix is not canonical")
        return result

    async def list_session_runs(
        self,
        session_id: str,
        through_run_id: str,
        context: OperationContext,
    ) -> tuple[RunResultSnapshot, ...]:
        async with self._sessions() as session:
            try:
                return await list_validated_session_runs(
                    session,
                    session_id=session_id,
                    through_run_id=through_run_id,
                    context=context,
                )
            except RuntimeError as error:
                raise AgentRuntimeAuthorityError(str(error)) from error

    async def list_counterexamples(
        self, task_id: str, failure_key: str, context: OperationContext
    ) -> tuple[CounterexampleSnapshot, ...]:
        del task_id, failure_key, context
        return ()

    async def get_profile(
        self,
        student_id: str,
        knowledge_points: tuple[str, ...],
        context: OperationContext,
    ) -> LearnerProfileSnapshot:
        del knowledge_points
        if student_id != context.actor.actor_id:
            raise AgentRuntimeAuthorityError("Learner differs from authenticated actor")
        from .models import LearnerProfileRow

        async with self._sessions() as session:
            row = await session.scalar(
                select(LearnerProfileRow).where(
                    LearnerProfileRow.tenant_id == context.actor.tenant_id,
                    LearnerProfileRow.learner_id == student_id,
                    LearnerProfileRow.actor_id == context.actor.actor_id,
                    LearnerProfileRow.content_hash == context.content_ref.content_hash,
                )
            )
        if row is None:
            raise AgentRuntimeAuthorityError("Learner profile is missing")
        value = dict(row.profile_json)
        revision = value.get("revision", 0)
        competencies = value.get("competencies", {})
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise AgentRuntimeAuthorityError("Learner revision is invalid")
        return LearnerProfileSnapshot(
            student_id=student_id,
            revision=revision,
            competencies=cast(Mapping[str, object], competencies),
            request_context=_request_context(context),
            evidence_refs=_evidence_refs(value.get("evidence_refs", [])),
        )

    async def list_recent(
        self, session_id: str, limit: int, context: OperationContext
    ) -> tuple[MessageSnapshot, ...]:
        del session_id, limit, context
        return ()

    async def _skill(
        self,
        session: AsyncSession,
        reference: SkillRef,
        context: OperationContext,
        *,
        require_active: bool,
    ) -> SkillSnapshot:
        certification = await session.scalar(
            select(SkillCertificationRow).where(
                SkillCertificationRow.tenant_id == context.actor.tenant_id,
                SkillCertificationRow.actor_id == context.actor.actor_id,
                SkillCertificationRow.content_hash == context.content_ref.content_hash,
                SkillCertificationRow.skill_id == reference.skill_id,
                SkillCertificationRow.skill_version_id == reference.skill_version_id,
                SkillCertificationRow.artifact_sha256 == reference.artifact_sha256,
                SkillCertificationRow.certification_id == reference.certification_id,
            )
        )
        revoked = await session.scalar(
            select(
                exists().where(
                    SkillCertificationRevocationRow.tenant_id == context.actor.tenant_id,
                    SkillCertificationRevocationRow.certification_id == reference.certification_id,
                )
            )
        )
        if certification is None or revoked is True:
            raise AgentRuntimeAuthorityError("Skill certification is missing or revoked")
        if require_active:
            binding, _, _ = await _current_session_binding_authority(session, context)
            try:
                await load_current_activation_authority(
                    session,
                    tenant_id=context.actor.tenant_id,
                    actor_id=binding.actor_id,
                    content_hash=binding.content_hash,
                    world_id=binding.world_id,
                    agent_profile_id=binding.agent_profile_id,
                    authority_id=binding.authority_id,
                    skill_ref=reference,
                )
            except (TypeError, ValueError, WorkflowInvariantError) as error:
                raise AgentRuntimeAuthorityError(
                    "Skill is not active in the exact durable scope"
                ) from error
        build = await session.scalar(
            select(SkillBuildRow).where(
                SkillBuildRow.tenant_id == context.actor.tenant_id,
                SkillBuildRow.build_id == certification.build_id,
                SkillBuildRow.actor_id == context.actor.actor_id,
                SkillBuildRow.status == "CERTIFIED",
            )
        )
        artifact = await session.scalar(
            select(SkillArtifactRow).where(
                SkillArtifactRow.tenant_id == context.actor.tenant_id,
                SkillArtifactRow.artifact_sha256 == reference.artifact_sha256,
                SkillArtifactRow.build_id == certification.build_id,
            )
        )
        if build is None or artifact is None:
            raise AgentRuntimeAuthorityError("Skill Build/Artifact closure is missing")
        certification_data = certification.certification_json
        metadata = artifact.metadata_json
        build_policy_id = certification_data.get("build_policy_id")
        if not isinstance(build_policy_id, str) or not build_policy_id:
            raise AgentRuntimeAuthorityError("Skill certification Build policy is invalid")
        policy = await session.scalar(
            select(BuildPolicyRow).where(
                BuildPolicyRow.tenant_id == context.actor.tenant_id,
                BuildPolicyRow.build_policy_id == build_policy_id,
                BuildPolicyRow.actor_id == context.actor.actor_id,
                BuildPolicyRow.content_hash == context.content_ref.content_hash,
                BuildPolicyRow.active.is_(True),
            )
        )
        if policy is None:
            raise AgentRuntimeAuthorityError("Skill Build policy closure is missing")
        source = _object(build.request_json.get("source_bundle"), "source_bundle")
        try:
            source_bundle_sha256 = canonical_source_bundle_sha256(source)
        except (TypeError, ValueError) as error:
            raise AgentRuntimeAuthorityError("Skill source bundle is invalid") from error
        entrypoint = _text(source, "entrypoint")
        raw_files = source.get("files")
        if isinstance(raw_files, str | bytes | bytearray) or not isinstance(raw_files, Sequence):
            raise AgentRuntimeAuthorityError("Skill source files are invalid")
        selected: dict[str, Any] | None = None
        for raw in raw_files:
            item = _object(raw, "source file")
            if item.get("path") == entrypoint:
                selected = item
                break
        if selected is None:
            raise AgentRuntimeAuthorityError("Skill entrypoint source is missing")
        source_code = _text(selected, "content")
        source_sha256 = hashlib.sha256(source_code.encode()).hexdigest()
        requested_capabilities = build.request_json.get("requested_capabilities")
        expected_metadata_keys = {
            "schema_version",
            "artifact_sha256",
            "source_sha256",
            "build_identity",
            "size_bytes",
            "compiler_profile",
            "compiler_version",
            "compiler_image",
            "test_suite_version",
            "policy_sha256",
            "parameter_schema",
            "parameter_schema_sha256",
        }
        expected_certification_keys = {
            "schema_version",
            "certification_id",
            "build_id",
            "skill_id",
            "skill_version_id",
            "artifact_sha256",
            "source_sha256",
            "actor_id",
            "content_hash",
            "build_policy_id",
            "policy_sha256",
            "capabilities",
            "issued_at",
            "parameter_schema",
            "parameter_schema_sha256",
        }
        if (
            build.skill_id != reference.skill_id
            or build.status != "CERTIFIED"
            or not build.terminal
            or build.build_json.get("status") != "CERTIFIED"
            or build.build_json.get("terminal") is not True
            or build.build_json.get("skill_version_id") != reference.skill_version_id
            or build.request_json.get("skill_id") != reference.skill_id
            or not isinstance(requested_capabilities, list)
            or any(not isinstance(item, str) or not item for item in requested_capabilities)
            or len(set(requested_capabilities)) != len(requested_capabilities)
            or any(item not in policy.allowed_capabilities for item in requested_capabilities)
            or artifact.actor_id != context.actor.actor_id
            or artifact.content_hash != context.content_ref.content_hash
            or artifact.skill_id != reference.skill_id
            or artifact.source_sha256 != source_bundle_sha256
            or selected.get("content_sha256") != source_sha256
            or set(metadata) != expected_metadata_keys
            or metadata.get("schema_version") != "1.0.0"
            or metadata.get("artifact_sha256") != artifact.artifact_sha256
            or metadata.get("source_sha256") != artifact.source_sha256
            or metadata.get("policy_sha256") != policy.policy_sha256
            or metadata.get("compiler_profile") != policy.compiler_profile
            or metadata.get("compiler_version") != policy.compiler_version
            or metadata.get("compiler_image") != policy.policy_json.get("compiler_image")
            or metadata.get("test_suite_version") != policy.test_suite_version
            or certification.certification_sha256 != canonical_json_sha256(certification_data)
            or certification.actor_id != context.actor.actor_id
            or certification.content_hash != context.content_ref.content_hash
            or certification_data.get("schema_version") != "1.0.0"
            or set(certification_data) != expected_certification_keys
            or certification_data.get("certification_id") != reference.certification_id
            or certification_data.get("build_id") != build.build_id
            or certification_data.get("skill_id") != reference.skill_id
            or certification_data.get("skill_version_id") != reference.skill_version_id
            or certification_data.get("artifact_sha256") != reference.artifact_sha256
            or certification_data.get("source_sha256") != artifact.source_sha256
            or certification_data.get("actor_id") != context.actor.actor_id
            or certification_data.get("content_hash") != context.content_ref.content_hash
            or certification_data.get("build_policy_id") != policy.build_policy_id
            or certification_data.get("policy_sha256") != policy.policy_sha256
            or certification_data.get("capabilities") != requested_capabilities
            or build.request_json.get("compiler_profile") != policy.compiler_profile
            or build.request_json.get("test_suite_version") != policy.test_suite_version
        ):
            raise AgentRuntimeAuthorityError("Skill durable authority closure drifted")
        try:
            parameter_schema = validated_certified_parameter_schema(
                policy.policy_json,
                metadata,
                certification_data,
                policy_sha256=policy.policy_sha256,
                build_id=build.build_id,
                skill_id=reference.skill_id,
                skill_version_id=reference.skill_version_id,
                source_sha256=artifact.source_sha256,
                artifact_sha256=reference.artifact_sha256,
                certification_id=reference.certification_id,
                build_policy_id=policy.build_policy_id,
                actor_id=context.actor.actor_id,
                content_hash=context.content_ref.content_hash,
                capabilities=requested_capabilities,
            )
        except CertifiedSkillSchemaError as error:
            raise AgentRuntimeAuthorityError("Skill certified parameter schema drifted") from error
        return SkillSnapshot(
            ref=reference,
            source_code=source_code,
            source_sha256=source_sha256,
            entrypoint=entrypoint,
            parameter_schema=parameter_schema,
            request_context=_wire_context(build.build_json),
        )


async def _current_session_binding_authority(
    session: AsyncSession,
    context: OperationContext,
    *,
    session_id: str | None = None,
) -> tuple[CurrentSessionBindingRow, AgentSessionRow, LaunchAuthorityRow]:
    """Resolve the Turn's Session and validate the complete immutable binding."""

    effective_session_id = session_id
    if effective_session_id is None and context.command_id is not None:
        turn_session_ids = list(
            (
                await session.scalars(
                    select(AgentTurnRow.session_id)
                    .where(
                        AgentTurnRow.tenant_id == context.actor.tenant_id,
                        AgentTurnRow.actor_id == context.actor.actor_id,
                        AgentTurnRow.command_id == context.command_id,
                    )
                    .limit(2)
                )
            ).all()
        )
        if len(turn_session_ids) != 1:
            raise AgentRuntimeAuthorityError("Turn Session binding identity is ambiguous")
        effective_session_id = turn_session_ids[0]

    binding_statement = select(CurrentSessionBindingRow).where(
        CurrentSessionBindingRow.tenant_id == context.actor.tenant_id,
        CurrentSessionBindingRow.actor_id == context.actor.actor_id,
        CurrentSessionBindingRow.content_hash == context.content_ref.content_hash,
    )
    if effective_session_id is not None:
        binding_statement = binding_statement.where(
            CurrentSessionBindingRow.session_id == effective_session_id
        )
    bindings = list((await session.scalars(binding_statement.limit(2))).all())
    if len(bindings) != 1:
        raise AgentRuntimeAuthorityError("current Session binding is missing or ambiguous")
    binding = bindings[0]
    owner = await session.scalar(
        select(AgentSessionRow).where(
            AgentSessionRow.tenant_id == context.actor.tenant_id,
            AgentSessionRow.actor_id == context.actor.actor_id,
            AgentSessionRow.session_id == binding.session_id,
        )
    )
    authority = await session.scalar(
        select(LaunchAuthorityRow).where(
            LaunchAuthorityRow.tenant_id == context.actor.tenant_id,
            LaunchAuthorityRow.authority_id == binding.authority_id,
        )
    )
    observed_at = await current_session_binding_observed_at(session)
    if (
        owner is None
        or authority is None
        or observed_at is None
        or not current_session_binding_matches(
            binding,
            owner=owner,
            authority=authority,
            observed_at=observed_at,
        )
    ):
        raise AgentRuntimeAuthorityError("current Session binding authority is corrupt")
    return binding, owner, authority


class PostgresAgentTrace:
    """Persist bounded runtime trace fields in the backend audit ledger."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def record(self, event: AgentTraceEvent, context: OperationContext) -> None:
        tenant_id = context.actor.tenant_id
        record_json = _agent_trace_record(event, context)
        audit_id = _agent_trace_audit_id(tenant_id, record_json)
        now = datetime.now(UTC)
        async with self._sessions() as session, session.begin():
            await session.execute(
                insert(AuditRow)
                .values(
                    audit_id=audit_id,
                    tenant_id=tenant_id,
                    occurred_at=now,
                    operation=_AGENT_TRACE_OPERATION,
                    outcome=_AGENT_TRACE_OUTCOME,
                    record_json=record_json,
                )
                .on_conflict_do_nothing(index_elements=[AuditRow.audit_id])
            )
            persisted = await session.scalar(select(AuditRow).where(AuditRow.audit_id == audit_id))
            if (
                persisted is None
                or persisted.tenant_id != tenant_id
                or persisted.operation != _AGENT_TRACE_OPERATION
                or persisted.outcome != _AGENT_TRACE_OUTCOME
                or persisted.record_json != record_json
            ):
                raise AgentRuntimeAuthorityError(
                    "Agent trace identity resolved to different immutable audit bytes"
                )


def _agent_trace_record(
    event: AgentTraceEvent,
    context: OperationContext,
) -> dict[str, Any]:
    return {
        "name": event.name,
        "turn_id": event.turn_id,
        "role": event.role,
        "fields": json_value(dict(event.fields)),
        "command_id": context.command_id,
        "trace_id": context.trace_id,
    }


def _agent_trace_audit_id(tenant_id: str, record_json: Mapping[str, Any]) -> str:
    try:
        return agent_trace_audit_id(tenant_id, record_json)
    except AgentTraceIdentityError as error:
        raise AgentRuntimeAuthorityError(str(error)) from error


async def _content(session: AsyncSession, context: OperationContext) -> ProductContentUnitRow:
    row = await session.scalar(
        select(ProductContentUnitRow).where(
            ProductContentUnitRow.tenant_id == context.actor.tenant_id,
            ProductContentUnitRow.unit_id == context.content_ref.unit_id,
            ProductContentUnitRow.version == context.content_ref.version,
            ProductContentUnitRow.content_hash == context.content_ref.content_hash,
        )
    )
    if row is None:
        raise AgentRuntimeAuthorityError("pinned ContentUnit is missing")
    return row


def _request_context(context: OperationContext) -> RequestContext:
    return RequestContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        trace_id=context.trace_id,
        requested_at=context.requested_at,
        actor=context.actor,
        content_ref=context.content_ref,
        schema_version=context.schema_version,
    )


def _wire_context(value: Mapping[str, Any]) -> RequestContext:
    raw = value.get("request_context")
    if not isinstance(raw, dict):
        raise AgentRuntimeAuthorityError("resource request_context is missing")
    return request_context_from_data(raw)


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentRuntimeAuthorityError(f"{label} must be an object")
    return dict(value)


def _text(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise AgentRuntimeAuthorityError(f"{key} must be text")
    return item


def _int(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise AgentRuntimeAuthorityError(f"{key} must be an integer")
    return item


def _strings(value: object, label: str) -> tuple[str, ...]:
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        raise AgentRuntimeAuthorityError(f"{label} must be an array")
    if any(not isinstance(item, str) for item in value):
        raise AgentRuntimeAuthorityError(f"{label} must contain text")
    return tuple(cast(Sequence[str], value))


def _evidence_refs(value: object) -> tuple[EvidenceRef, ...]:
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        raise AgentRuntimeAuthorityError("evidence_refs must be an array")
    result: list[EvidenceRef] = []
    for raw in value:
        item = _object(raw, "EvidenceRef")
        created = _text(item, "created_at")
        created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
        result.append(
            EvidenceRef(
                evidence_id=_text(item, "evidence_id"),
                evidence_type=EvidenceType(_text(item, "evidence_type")),
                created_at=created_at,
                sha256=cast(str | None, item.get("sha256")),
                uri=cast(str | None, item.get("uri")),
            )
        )
    return tuple(result)


__all__ = [
    "AgentRuntimeAuthorityError",
    "PostgresAgentRuntimeReads",
    "PostgresAgentTrace",
]
