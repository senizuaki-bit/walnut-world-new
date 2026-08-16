"""Strict PostgreSQL resolver for the public v0.4 student launch authority."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import Failure, OperationContext, Result, SkillRef, Success

from walnut_backend.application.game.student_bootstrap import (
    ActiveSkillAuthority,
    StudentLaunchAuthority,
)

from .activation_authority import load_current_activation_authority
from .models import (
    AgentProfileRow,
    AgentSessionRow,
    BuildPolicyRow,
    CurrentSessionBindingRow,
    LaunchAuthorityRow,
    LearnerProfileRow,
    ProductContentUnitRow,
    RegistryEntryRow,
    RegistryHeadRow,
    SkillActivationRow,
    SkillCertificationRevocationRow,
    SkillCertificationRow,
    WorldSnapshotRow,
)
from .workflow_jobs import WorkflowInvariantError

_ALLOWED_CAPABILITIES = {
    "WORLD_READ",
    "MOVE",
    "PLANT",
    "WATER",
    "HARVEST",
    "INTERACT",
    "SPEAK",
}
_LOCALE_PATTERN = re.compile(r"^[a-z]{2,3}(?:-[A-Z]{2})?$")


class PostgresStudentBootstrapReader:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def resolve(self, context: OperationContext) -> Result[StudentLaunchAuthority]:
        if context.actor.actor_type.value != "student":
            return Failure(_denied("student bootstrap requires a student actor"))
        async with self._sessions() as session:
            authorities = list(
                await session.scalars(
                    select(LaunchAuthorityRow).where(
                        LaunchAuthorityRow.tenant_id == context.actor.tenant_id,
                        LaunchAuthorityRow.actor_id == context.actor.actor_id,
                        LaunchAuthorityRow.active.is_(True),
                    )
                )
            )
            if not authorities:
                return Failure(_denied("no active launch authority exists for this actor"))
            if len(authorities) != 1:
                return Failure(_invariant("active launch authority is ambiguous"))
            authority = authorities[0]
            if authority.learner_id != context.actor.actor_id:
                return Failure(_invariant("launch learner differs from authenticated student"))

            content = await session.scalar(
                select(ProductContentUnitRow).where(
                    ProductContentUnitRow.tenant_id == authority.tenant_id,
                    ProductContentUnitRow.unit_id == authority.content_unit_id,
                    ProductContentUnitRow.version == authority.content_version,
                    ProductContentUnitRow.content_hash == authority.content_hash,
                )
            )
            if content is None or "LEARNER" not in content.audiences:
                return Failure(_invariant("launch content is not a published learner authority"))
            learner = await session.scalar(
                select(LearnerProfileRow).where(
                    LearnerProfileRow.tenant_id == authority.tenant_id,
                    LearnerProfileRow.learner_id == authority.learner_id,
                    LearnerProfileRow.actor_id == authority.actor_id,
                    LearnerProfileRow.content_hash == authority.content_hash,
                )
            )
            agent_profile = await session.scalar(
                select(AgentProfileRow).where(
                    AgentProfileRow.tenant_id == authority.tenant_id,
                    AgentProfileRow.agent_profile_id == authority.agent_profile_id,
                    AgentProfileRow.actor_id == authority.actor_id,
                    AgentProfileRow.content_hash == authority.content_hash,
                )
            )
            if learner is None or agent_profile is None:
                return Failure(_invariant("launch learner or Agent profile authority is missing"))
            locale = learner.profile_json.get("locale")
            if not isinstance(locale, str) or _LOCALE_PATTERN.fullmatch(locale) is None:
                return Failure(_invariant("launch learner profile has no valid locale authority"))

            policy = await session.scalar(
                select(BuildPolicyRow).where(
                    BuildPolicyRow.tenant_id == authority.tenant_id,
                    BuildPolicyRow.build_policy_id == authority.build_policy_id,
                    BuildPolicyRow.actor_id == authority.actor_id,
                    BuildPolicyRow.content_hash == authority.content_hash,
                    BuildPolicyRow.active.is_(True),
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
            if policy is None or world is None:
                return Failure(_invariant("launch BuildPolicy or World authority is missing"))
            capabilities = _capabilities(policy.allowed_capabilities)
            if capabilities is None:
                return Failure(_invariant("BuildPolicy capabilities are not canonical"))
            if policy.max_source_files != 32 or policy.max_source_bytes != 1_048_576:
                return Failure(_invariant("BuildPolicy source limits differ from contract v0.4"))

            session_result = await self._current_session(session, authority)
            if isinstance(session_result, Failure):
                return session_result
            registry_result = await self._registry(session, authority)
            if isinstance(registry_result, Failure):
                return registry_result
            registry_revision, active_skill = registry_result.value

        return Success(
            StudentLaunchAuthority(
                content_unit_id=authority.content_unit_id,
                content_version=authority.content_version,
                content_hash=authority.content_hash,
                world_id=world.world_id,
                world_revision=world.revision,
                last_event_sequence=world.last_event_sequence,
                state_hash=world.state_hash,
                learner_id=authority.learner_id,
                agent_profile_id=authority.agent_profile_id,
                channel=authority.channel,
                locale=locale,
                teaching_spec_version=authority.teaching_spec_version,
                current_session_id=session_result.value,
                build_policy_id=policy.build_policy_id,
                compiler_profile=policy.compiler_profile,
                compiler_version=policy.compiler_version,
                sandbox_image_digest=policy.sandbox_image_digest,
                test_suite_version=policy.test_suite_version,
                allowed_capabilities=capabilities,
                max_source_files=policy.max_source_files,
                max_source_bytes=policy.max_source_bytes,
                registry_revision=registry_revision,
                active_skill=active_skill,
            )
        )

    async def _current_session(
        self, session: AsyncSession, authority: LaunchAuthorityRow
    ) -> Result[str | None]:
        binding = await session.scalar(
            select(CurrentSessionBindingRow).where(
                CurrentSessionBindingRow.tenant_id == authority.tenant_id,
                CurrentSessionBindingRow.authority_id == authority.authority_id,
            )
        )
        if binding is None:
            return Success(None)
        expected = (
            authority.actor_id,
            authority.content_hash,
            authority.world_id,
            authority.learner_id,
            authority.agent_profile_id,
        )
        actual = (
            binding.actor_id,
            binding.content_hash,
            binding.world_id,
            binding.learner_id,
            binding.agent_profile_id,
        )
        if actual != expected:
            return Failure(_invariant("current Session binding differs from launch scope"))
        resource = await session.scalar(
            select(AgentSessionRow).where(
                AgentSessionRow.session_id == binding.session_id,
                AgentSessionRow.tenant_id == authority.tenant_id,
                AgentSessionRow.actor_id == authority.actor_id,
                AgentSessionRow.world_id == authority.world_id,
            )
        )
        if resource is None or resource.status != "ACTIVE":
            return Failure(_invariant("current Session binding has no active durable Session"))
        try:
            content = _mapping(resource.session_json, "content")
            if (
                resource.session_json.get("learner_id") != authority.learner_id
                or resource.session_json.get("agent_profile_id") != authority.agent_profile_id
                or resource.session_json.get("channel") != authority.channel
                or content.get("unit_id") != authority.content_unit_id
                or content.get("version") != authority.content_version
                or content.get("content_hash") != authority.content_hash
            ):
                return Failure(_invariant("current Session resource differs from launch authority"))
        except TypeError as error:
            return Failure(_invariant(str(error)))
        return Success(binding.session_id)

    async def _registry(
        self, session: AsyncSession, authority: LaunchAuthorityRow
    ) -> Result[tuple[int, ActiveSkillAuthority | None]]:
        head = await session.scalar(
            select(RegistryHeadRow).where(
                RegistryHeadRow.tenant_id == authority.tenant_id,
                RegistryHeadRow.authority_id == authority.authority_id,
                RegistryHeadRow.actor_id == authority.actor_id,
                RegistryHeadRow.content_hash == authority.content_hash,
                RegistryHeadRow.world_id == authority.world_id,
                RegistryHeadRow.agent_profile_id == authority.agent_profile_id,
            )
        )
        if head is None:
            return Failure(_invariant("registry scope head is missing; revision cannot be defaulted"))
        if head.revision < 0:
            return Failure(_invariant("registry scope revision is invalid"))
        entry = await session.scalar(
            select(RegistryEntryRow).where(
                RegistryEntryRow.tenant_id == authority.tenant_id,
                RegistryEntryRow.actor_id == authority.actor_id,
                RegistryEntryRow.content_hash == authority.content_hash,
                RegistryEntryRow.world_id == authority.world_id,
                RegistryEntryRow.agent_profile_id == authority.agent_profile_id,
                RegistryEntryRow.revision == head.revision,
            )
        )
        activation = await session.scalar(
            select(SkillActivationRow).where(
                SkillActivationRow.tenant_id == authority.tenant_id,
                SkillActivationRow.actor_id == authority.actor_id,
                SkillActivationRow.content_hash == authority.content_hash,
                SkillActivationRow.world_id == authority.world_id,
                SkillActivationRow.agent_profile_id == authority.agent_profile_id,
                SkillActivationRow.registry_revision == head.revision,
            )
        )
        if head.revision == 0:
            if entry is not None or activation is not None:
                return Failure(_invariant("registry revision zero has an active entry"))
            return Success((0, None))
        if entry is None or activation is None:
            return Failure(_invariant("registry head has no exact entry and Activation"))
        reference = SkillRef(
            skill_id=activation.skill_id,
            skill_version_id=activation.skill_version_id,
            artifact_sha256=activation.artifact_sha256,
            certification_id=activation.certification_id,
        )
        try:
            closed = await load_current_activation_authority(
                session,
                tenant_id=authority.tenant_id,
                actor_id=authority.actor_id,
                content_hash=authority.content_hash,
                world_id=authority.world_id,
                agent_profile_id=authority.agent_profile_id,
                authority_id=authority.authority_id,
                skill_ref=reference,
            )
        except (TypeError, ValueError, WorkflowInvariantError) as error:
            return Failure(_invariant(f"active Skill authority is corrupt: {error}"))
        entry = closed.entry
        activation = closed.activation
        certification = await session.scalar(
            select(SkillCertificationRow).where(
                SkillCertificationRow.tenant_id == authority.tenant_id,
                SkillCertificationRow.certification_id == entry.certification_id,
                SkillCertificationRow.actor_id == authority.actor_id,
                SkillCertificationRow.content_hash == authority.content_hash,
                SkillCertificationRow.skill_id == entry.skill_id,
                SkillCertificationRow.skill_version_id == entry.skill_version_id,
                SkillCertificationRow.artifact_sha256 == entry.artifact_sha256,
            )
        )
        revocation = await session.scalar(
            select(SkillCertificationRevocationRow).where(
                SkillCertificationRevocationRow.tenant_id == authority.tenant_id,
                SkillCertificationRevocationRow.certification_id == entry.certification_id,
            )
        )
        if certification is None or revocation is not None:
            return Failure(_invariant("active registry entry is uncertified or revoked"))
        return Success(
            (
                head.revision,
                ActiveSkillAuthority(
                    activation_id=activation.activation_id,
                    skill_id=entry.skill_id,
                    skill_version_id=entry.skill_version_id,
                    artifact_sha256=entry.artifact_sha256,
                    certification_id=entry.certification_id,
                    registry_revision=head.revision,
                    activated_at=activation.activated_at,
                ),
            )
        )


def _capabilities(value: object) -> tuple[str, ...] | None:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) for item in value)
        or len(value) != len(set(value))
        or not set(value) <= _ALLOWED_CAPABILITIES
    ):
        return None
    return tuple(value)


def _mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    item = value.get(key)
    if not isinstance(item, Mapping):
        raise TypeError(f"{key} must be an object")
    return item


def _denied(message: str) -> Any:
    return _error("AUTHORIZATION_DENIED", "AUTHORITY", message)


def _invariant(message: str) -> Any:
    return _error("INVARIANT_VIOLATION", "AUTHORITY", message)


def _error(code: str, stage: str, message: str) -> Any:
    from yaya_agent_contracts import ContractError, ErrorCategory

    category, key = {
        "AUTHORIZATION_DENIED": (ErrorCategory.AUTHORIZATION, "auth.permission_denied"),
        "INVARIANT_VIOLATION": (ErrorCategory.INVARIANT, "system.invariant_violation"),
    }[code]
    return ContractError(code, category, False, key, stage, message)


__all__ = ["PostgresStudentBootstrapReader"]
