"""Activation command acceptance and immutable activation reads."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    CommandCreateReceipt,
    ContentRef,
    ContractError,
    Failure,
    NewCommand,
    OperationContext,
    Result,
    Success,
    VersionSet,
    canonical_json_sha256,
)

from .activation_authority import validate_historical_activation_authority
from .certification_authority import (
    artifact_authority_sha256,
    validate_certification_authority,
)
from .command_store import PostgresCommandStore
from .models import (
    AgentProfileRow,
    BuildPolicyRow,
    IdempotencyReceiptRow,
    LaunchAuthorityRow,
    LearnerProfileRow,
    ProductContentUnitRow,
    RegistryHeadRow,
    SkillActivationRow,
    SkillArtifactRow,
    SkillBuildProvenanceRow,
    SkillCertificationRevocationRow,
    SkillCertificationRow,
    WorkflowJobRow,
    WorldSnapshotRow,
    request_context_data,
    world_snapshot_from_data,
)
from .workflow_jobs import PostgresWorkflowJobStore


class _RejectActivation(Exception):
    def __init__(self, error: ContractError) -> None:
        super().__init__(error.code)
        self.error = error


class PostgresSkillActivationStore:
    """Accept full-scope activation work without materializing it early."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        command_store: PostgresCommandStore,
        workflow_jobs: PostgresWorkflowJobStore | None = None,
    ) -> None:
        self._sessions = session_factory
        self._command_store = command_store
        self._workflow_jobs = workflow_jobs or PostgresWorkflowJobStore(session_factory)

    async def accept(
        self,
        command: NewCommand,
        skill_version_id: str,
        request_body: Mapping[str, Any],
        context: OperationContext,
    ) -> Result[tuple[str, CommandCreateReceipt]]:
        try:
            async with self._sessions() as session, session.begin():
                tenant_id, actor_id, operation, idempotency_key = command.idempotency_scope(
                    context
                )
                replay = await session.scalar(
                    select(IdempotencyReceiptRow).where(
                        IdempotencyReceiptRow.tenant_id == tenant_id,
                        IdempotencyReceiptRow.actor_id == actor_id,
                        IdempotencyReceiptRow.operation == operation,
                        IdempotencyReceiptRow.idempotency_key == idempotency_key,
                    )
                )
                effective_command = command
                effective_context = context
                authority: LaunchAuthorityRow | None = None
                certification: SkillCertificationRow | None = None
                expected_revision: int | None = None
                world_id: str | None = None
                agent_profile_id: str | None = None
                if replay is None:
                    resolved = await _resolve_activation_authority(
                        session,
                        skill_version_id,
                        request_body,
                        context,
                    )
                    if isinstance(resolved, Failure):
                        return resolved
                    (
                        authority,
                        certification,
                        expected_revision,
                        world_id,
                        agent_profile_id,
                        effective_context,
                        versions,
                    ) = resolved.value
                    effective_command = replace(command, versions=versions)
                command_result = await self._command_store.accept_once_in_session(
                    session, effective_command, effective_context
                )
                if isinstance(command_result, Failure):
                    return command_result
                receipt = command_result.value
                if not receipt.created:
                    row = await session.scalar(
                        select(WorkflowJobRow).where(
                            WorkflowJobRow.tenant_id
                            == receipt.command.request_context.actor.tenant_id,
                            WorkflowJobRow.command_id == receipt.command.command_id,
                        )
                    )
                    replay_provenance = (
                        await _activation_job_provenance(session, row)
                        if row is not None
                        else None
                    )
                    if (
                        row is None
                        or row.subject_type != "SKILL_ACTIVATION"
                        or replay_provenance is None
                    ):
                        raise _RejectActivation(
                            _invariant("accepted activation command has no durable workflow")
                        )
                    return Success((row.subject_id, receipt))
                if (
                    authority is None
                    or certification is None
                    or expected_revision is None
                    or world_id is None
                    or agent_profile_id is None
                ):
                    raise _RejectActivation(
                        _invariant("new activation command lost its resolved authority")
                    )
                activation_id = _activation_id(receipt.command.command_id)
                build_provenance = await _certification_build_provenance(
                    session, certification, for_update=True
                )
                if build_provenance is None:
                    raise _RejectActivation(
                        _invariant("activation Build provenance drifted before enqueue")
                    )
                await session.flush()
                await self._workflow_jobs.enqueue_in_session(
                    session,
                    tenant_id=effective_context.actor.tenant_id,
                    command_id=receipt.command.command_id,
                    operation=effective_command.command_type,
                    subject_type="SKILL_ACTIVATION",
                    subject_id=activation_id,
                    request_sha256=effective_command.request_sha256,
                    job={
                        "schema_version": "1.0.0",
                        "request_context": request_context_data(
                            receipt.command.request_context
                        ),
                        "activation_id": activation_id,
                        "authority_id": authority.authority_id,
                        "expected_registry_revision": expected_revision,
                        "activation_scope": {
                            "world_id": world_id,
                            "agent_profile_id": agent_profile_id,
                        },
                        "skill": {
                            "skill_id": certification.skill_id,
                            "skill_version_id": certification.skill_version_id,
                            "certification_id": certification.certification_id,
                            "artifact_sha256": certification.artifact_sha256,
                        },
                        "build_provenance_sha256": build_provenance.authority_sha256,
                        "certification_sha256": certification.certification_sha256,
                        "artifact_authority_sha256": artifact_authority_sha256(
                            await _required_certification_artifact(
                                session, certification, for_update=True
                            )
                        ),
                        "reason": request_body.get("reason"),
                    },
                )
                return Success((activation_id, receipt))
        except _RejectActivation as error:
            return Failure(error.error)

    async def get(
        self, activation_id: str, context: OperationContext
    ) -> Result[dict[str, Any]]:
        async with self._sessions() as session:
            row = await session.scalar(
                select(SkillActivationRow).where(
                    SkillActivationRow.activation_id == activation_id,
                    SkillActivationRow.tenant_id == context.actor.tenant_id,
                    SkillActivationRow.actor_id == context.actor.actor_id,
                )
            )
            certification = (
                await session.scalar(
                    select(SkillCertificationRow).where(
                        SkillCertificationRow.tenant_id == row.tenant_id,
                        SkillCertificationRow.actor_id == row.actor_id,
                        SkillCertificationRow.certification_id == row.certification_id,
                        SkillCertificationRow.skill_version_id == row.skill_version_id,
                        SkillCertificationRow.artifact_sha256 == row.artifact_sha256,
                    )
                )
                if row is not None
                else None
            )
            closed_activation = (
                await validate_historical_activation_authority(session, row)
                if row is not None
                else False
            )
        if row is None:
            return Failure(_not_found())
        value = dict(row.activation_json)
        origin = value.get("request_context")
        actor = origin.get("actor") if isinstance(origin, Mapping) else None
        content = origin.get("content_ref") if isinstance(origin, Mapping) else None
        scope = value.get("activation_scope")
        activated_at = _timestamp(value.get("activated_at"))
        if (
            not isinstance(actor, Mapping)
            or actor.get("tenant_id") != row.tenant_id
            or actor.get("actor_id") != row.actor_id
            or not isinstance(content, Mapping)
            or content.get("content_hash") != row.content_hash
            or not isinstance(scope, Mapping)
            or scope.get("world_id") != row.world_id
            or scope.get("agent_profile_id") != row.agent_profile_id
            or value.get("activation_id") != row.activation_id
            or value.get("skill_id") != row.skill_id
            or value.get("skill_version_id") != row.skill_version_id
            or value.get("certification_id") != row.certification_id
            or value.get("artifact_sha256") != row.artifact_sha256
            or value.get("registry_revision") != row.registry_revision
            or value.get("previous_registry_revision")
            != row.previous_registry_revision
            or activated_at != row.activated_at
            or canonical_json_sha256(value) != row.activation_sha256
            or certification is None
            or not closed_activation
        ):
            return Failure(_invariant("activation durable identity drifted"))
        return Success(value)


async def _resolve_activation_authority(
    session: AsyncSession,
    skill_version_id: str,
    request_body: Mapping[str, Any],
    context: OperationContext,
) -> Result[
    tuple[
        LaunchAuthorityRow,
        SkillCertificationRow,
        int,
        str,
        str,
        OperationContext,
        VersionSet,
    ]
]:
    """Resolve the exact durable launch closure before accepting a Command."""

    scope_value = request_body.get("activation_scope")
    if not isinstance(scope_value, Mapping):
        return Failure(_invalid("activation_scope must be an object"))
    world_id = scope_value.get("world_id")
    agent_profile_id = scope_value.get("agent_profile_id")
    expected_revision = request_body.get("expected_registry_revision")
    if (
        not isinstance(world_id, str)
        or not isinstance(agent_profile_id, str)
        or isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        return Failure(_invalid("activation scope or revision is invalid"))

    authorities = list(
        (
            await session.scalars(
                select(LaunchAuthorityRow)
                .where(
                    LaunchAuthorityRow.tenant_id == context.actor.tenant_id,
                    LaunchAuthorityRow.actor_id == context.actor.actor_id,
                    LaunchAuthorityRow.world_id == world_id,
                    LaunchAuthorityRow.agent_profile_id == agent_profile_id,
                    LaunchAuthorityRow.active.is_(True),
                )
                .limit(2)
            )
        ).all()
    )
    if not authorities:
        return Failure(_mismatch("activation scope is not the active launch authority"))
    if len(authorities) != 1:
        return Failure(_invariant("activation launch authority is ambiguous"))
    authority = authorities[0]
    if context.actor.actor_type.value == "student" and authority.learner_id != context.actor.actor_id:
        return Failure(_invariant("activation learner differs from authenticated student"))
    try:
        content_ref = ContentRef(
            unit_id=authority.content_unit_id,
            version=authority.content_version,
            content_hash=authority.content_hash,
        )
    except (TypeError, ValueError):
        return Failure(_invariant("activation launch content identity is invalid"))

    content = await session.scalar(
        select(ProductContentUnitRow).where(
            ProductContentUnitRow.tenant_id == authority.tenant_id,
            ProductContentUnitRow.unit_id == authority.content_unit_id,
            ProductContentUnitRow.version == authority.content_version,
            ProductContentUnitRow.content_hash == authority.content_hash,
        )
    )
    world = await session.scalar(
        select(WorldSnapshotRow).where(
            WorldSnapshotRow.tenant_id == authority.tenant_id,
            WorldSnapshotRow.world_id == authority.world_id,
            WorldSnapshotRow.actor_id == authority.actor_id,
            WorldSnapshotRow.content_hash == authority.content_hash,
        )
    )
    learner = await session.scalar(
        select(LearnerProfileRow).where(
            LearnerProfileRow.tenant_id == authority.tenant_id,
            LearnerProfileRow.learner_id == authority.learner_id,
            LearnerProfileRow.actor_id == authority.actor_id,
            LearnerProfileRow.content_hash == authority.content_hash,
        )
    )
    profile = await session.scalar(
        select(AgentProfileRow).where(
            AgentProfileRow.tenant_id == authority.tenant_id,
            AgentProfileRow.agent_profile_id == authority.agent_profile_id,
            AgentProfileRow.actor_id == authority.actor_id,
            AgentProfileRow.content_hash == authority.content_hash,
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
    if content is None or world is None or learner is None or profile is None or policy is None:
        return Failure(_invariant("activation launch authority closure is incomplete"))

    reference = content.content_json.get("content_ref")
    published_at = _timestamp(content.content_json.get("published_at"))
    expected_content = {
        "unit_id": content_ref.unit_id,
        "version": content_ref.version,
        "content_hash": content_ref.content_hash,
    }
    if (
        not isinstance(reference, Mapping)
        or dict(reference) != expected_content
        or content.content_json.get("status") != "PUBLISHED"
        or content.content_json.get("unit_type") != "TASK"
        or content.content_json.get("audiences") != content.audiences
        or "LEARNER" not in content.audiences
        or published_at != content.published_at
    ):
        return Failure(_invariant("activation published Content authority drifted"))

    authority_wire = {
        "schema_version": "1.0.0",
        "authority_id": authority.authority_id,
        "actor_id": authority.actor_id,
        "content": expected_content,
        "world_id": authority.world_id,
        "learner_id": authority.learner_id,
        "agent_profile_id": authority.agent_profile_id,
        "build_policy_id": authority.build_policy_id,
        "channel": authority.channel,
        "teaching_spec_version": authority.teaching_spec_version,
        "active": authority.active,
    }
    if (
        authority.channel != "GAME"
        or not authority.teaching_spec_version
        or canonical_json_sha256(authority_wire) != authority.authority_sha256
    ):
        return Failure(_invariant("activation LaunchAuthority bytes drifted"))

    try:
        snapshot = world_snapshot_from_data(world.snapshot_json)
    except (KeyError, TypeError, ValueError):
        return Failure(_invariant("activation World authority is invalid"))
    if (
        snapshot.request_context.actor.tenant_id != authority.tenant_id
        or snapshot.request_context.actor.actor_id != authority.actor_id
        or snapshot.request_context.content_ref != content_ref
        or snapshot.world_id != world.world_id
        or snapshot.revision != world.revision
        or snapshot.last_event_sequence != world.last_event_sequence
        or snapshot.state_hash != world.state_hash
        or snapshot.generated_at != world.generated_at
        or canonical_json_sha256(snapshot.state) != snapshot.state_hash
    ):
        return Failure(_invariant("activation World authority drifted"))

    learner_json = learner.profile_json
    learner_content = learner_json.get("content")
    if (
        canonical_json_sha256(learner_json) != learner.profile_sha256
        or learner_json.get("learner_id") != learner.learner_id
        or learner_json.get("actor_id") != learner.actor_id
        or not isinstance(learner_content, Mapping)
        or dict(learner_content) != expected_content
    ):
        return Failure(_invariant("activation LearnerProfile authority drifted"))

    profile_json = profile.profile_json
    profile_content = profile_json.get("content")
    provider = profile_json.get("provider")
    model_version = profile_json.get("model_version")
    prompt_version = profile_json.get("prompt_version")
    if (
        canonical_json_sha256(profile_json) != profile.profile_sha256
        or profile_json.get("agent_profile_id") != profile.agent_profile_id
        or profile_json.get("actor_id") != profile.actor_id
        or not isinstance(profile_content, Mapping)
        or dict(profile_content) != expected_content
        or any(
            not isinstance(value, str) or not value
            for value in (provider, model_version, prompt_version)
        )
    ):
        return Failure(_invariant("activation AgentProfile authority drifted"))

    policy_json = policy.policy_json
    compiler_image = policy_json.get("compiler_image")
    if (
        canonical_json_sha256(policy_json) != policy.policy_sha256
        or policy_json.get("compiler_profile") != policy.compiler_profile
        or policy_json.get("compiler_version") != policy.compiler_version
        or policy_json.get("test_suite_version") != policy.test_suite_version
        or not isinstance(compiler_image, str)
        or not compiler_image.endswith(f"@{policy.sandbox_image_digest}")
    ):
        return Failure(_invariant("activation BuildPolicy authority drifted"))

    head = await session.scalar(
        select(RegistryHeadRow)
        .where(
            RegistryHeadRow.tenant_id == authority.tenant_id,
            RegistryHeadRow.actor_id == authority.actor_id,
            RegistryHeadRow.content_hash == authority.content_hash,
            RegistryHeadRow.world_id == authority.world_id,
            RegistryHeadRow.agent_profile_id == authority.agent_profile_id,
            RegistryHeadRow.authority_id == authority.authority_id,
        )
        .with_for_update()
    )
    if head is None:
        return Failure(_invariant("server-owned registry head is missing for launch authority"))
    if head.revision != expected_revision:
        return Failure(_mismatch("expected_registry_revision is stale"))

    revoked = exists(
        select(SkillCertificationRevocationRow.revocation_id).where(
            SkillCertificationRevocationRow.tenant_id == SkillCertificationRow.tenant_id,
            SkillCertificationRevocationRow.certification_id
            == SkillCertificationRow.certification_id,
        )
    )
    certifications = list(
        (
            await session.scalars(
                select(SkillCertificationRow)
                .where(
                    SkillCertificationRow.tenant_id == authority.tenant_id,
                    SkillCertificationRow.actor_id == authority.actor_id,
                    SkillCertificationRow.content_hash == authority.content_hash,
                    SkillCertificationRow.skill_version_id == skill_version_id,
                    ~revoked,
                )
                .limit(2)
            )
        ).all()
    )
    if not certifications:
        return Failure(_not_certified())
    if len(certifications) != 1:
        return Failure(_invariant("skill version has multiple active certifications"))
    certification = certifications[0]
    artifact = await session.scalar(
        select(SkillArtifactRow).where(
            SkillArtifactRow.tenant_id == certification.tenant_id,
            SkillArtifactRow.artifact_sha256 == certification.artifact_sha256,
            SkillArtifactRow.build_id == certification.build_id,
            SkillArtifactRow.actor_id == certification.actor_id,
            SkillArtifactRow.content_hash == certification.content_hash,
            SkillArtifactRow.skill_id == certification.skill_id,
        )
    )
    provenance = await _certification_build_provenance(session, certification)
    certification_json = certification.certification_json
    capabilities = certification_json.get("capabilities")
    issued_at = _timestamp(certification_json.get("issued_at"))
    if (
        artifact is None
        or provenance is None
        or canonical_json_sha256(certification_json) != certification.certification_sha256
        or certification_json.get("schema_version") != "1.0.0"
        or certification_json.get("certification_id") != certification.certification_id
        or certification_json.get("build_id") != certification.build_id
        or certification_json.get("skill_id") != certification.skill_id
        or certification_json.get("skill_version_id") != certification.skill_version_id
        or certification_json.get("artifact_sha256") != certification.artifact_sha256
        or certification_json.get("source_sha256") != artifact.source_sha256
        or certification_json.get("actor_id") != certification.actor_id
        or certification_json.get("content_hash") != certification.content_hash
        or certification_json.get("build_policy_id") != policy.build_policy_id
        or certification_json.get("policy_sha256") != policy.policy_sha256
        or not isinstance(capabilities, list)
        or any(
            not isinstance(capability, str)
            or capability not in policy.allowed_capabilities
            for capability in capabilities
        )
        or len(set(capabilities)) != len(capabilities)
        or issued_at != certification.certified_at
    ):
        return Failure(_invariant("activation Certification authority drifted"))

    effective_context = replace(context, content_ref=content_ref)
    versions = VersionSet(
        api_version=context.schema_version,
        event_version="1",
        policy_version=policy.build_policy_id,
        world_rules_version=snapshot.world_rules_version,
        teaching_spec_version=authority.teaching_spec_version,
        skill_version=certification.skill_version_id,
        artifact_sha256=certification.artifact_sha256,
        compiler_version=policy.compiler_version,
        sandbox_image_digest=policy.sandbox_image_digest,
        test_suite_version=policy.test_suite_version,
        prompt_version=prompt_version,
        model_version=model_version,
    )
    return Success(
        (
            authority,
            certification,
            expected_revision,
            world_id,
            agent_profile_id,
            effective_context,
            versions,
        )
    )


async def _certification_build_provenance(
    session: AsyncSession,
    certification: SkillCertificationRow,
    *,
    for_update: bool = False,
) -> SkillBuildProvenanceRow | None:
    authority = await validate_certification_authority(
        session, certification, for_update=for_update
    )
    return authority[1] if authority is not None else None


async def _required_certification_artifact(
    session: AsyncSession,
    certification: SkillCertificationRow,
    *,
    for_update: bool = False,
) -> SkillArtifactRow:
    authority = await validate_certification_authority(
        session, certification, for_update=for_update
    )
    if authority is None:
        raise _RejectActivation(_invariant("activation Certification authority drifted"))
    return authority[0]


async def _activation_job_provenance(
    session: AsyncSession, job: WorkflowJobRow
) -> SkillBuildProvenanceRow | None:
    skill = job.job_json.get("skill")
    digest = job.job_json.get("build_provenance_sha256")
    certification_digest = job.job_json.get("certification_sha256")
    artifact_digest = job.job_json.get("artifact_authority_sha256")
    if (
        not isinstance(skill, Mapping)
        or not isinstance(digest, str)
        or not isinstance(certification_digest, str)
        or not isinstance(artifact_digest, str)
    ):
        return None
    certification = await session.scalar(
        select(SkillCertificationRow).where(
            SkillCertificationRow.tenant_id == job.tenant_id,
            SkillCertificationRow.certification_id == skill.get("certification_id"),
            SkillCertificationRow.skill_version_id == skill.get("skill_version_id"),
            SkillCertificationRow.artifact_sha256 == skill.get("artifact_sha256"),
        )
    )
    if certification is None:
        return None
    authority = await validate_certification_authority(
        session,
        certification,
        expected_certification_sha256=certification_digest,
        expected_artifact_authority_sha256=artifact_digest,
        expected_build_provenance_sha256=digest,
    )
    return authority[1] if authority is not None else None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo is not None else None


def _activation_id(command_id: str) -> str:
    digest = hashlib.sha256(f"activation\x00{command_id}".encode()).hexdigest()
    return f"activation_{digest[:24]}"


def _error(code: str, category: str, stage: str, message: str, key: str) -> ContractError:
    from yaya_agent_contracts import ErrorCategory

    return ContractError(
        code=code,
        category=ErrorCategory(category),
        retryable=False,
        user_message_key=key,
        stage=stage,
        message=message,
    )


def _invalid(message: str) -> ContractError:
    return _error("INVALID_REQUEST", "VALIDATION", "VALIDATE", message, "request.invalid")


def _mismatch(message: str) -> ContractError:
    return _error(
        "CONTENT_VERSION_MISMATCH",
        "VALIDATION",
        "REGISTRY",
        message,
        "content.version_mismatch",
    )


def _not_certified() -> ContractError:
    return _error(
        "SKILL_NOT_CERTIFIED",
        "SKILL",
        "REGISTRY",
        "skill version has no active certification",
        "skill.not_certified",
    )


def _not_found() -> ContractError:
    return _error("NOT_FOUND", "VALIDATION", "READ", "activation not found", "resource.not_found")


def _invariant(message: str) -> ContractError:
    return _error(
        "INVARIANT_VIOLATION",
        "INVARIANT",
        "REGISTRY",
        message,
        "system.invariant_violation",
    )


__all__ = ["PostgresSkillActivationStore"]
