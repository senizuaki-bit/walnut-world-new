"""Public v0.4 student launch authority assembled without client-side defaults."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from yaya_agent_contracts import Failure, OperationContext, Result, Success


@dataclass(frozen=True, slots=True)
class ActiveSkillAuthority:
    activation_id: str
    skill_id: str
    skill_version_id: str
    artifact_sha256: str
    certification_id: str
    registry_revision: int
    activated_at: datetime


@dataclass(frozen=True, slots=True)
class StudentLaunchAuthority:
    content_unit_id: str
    content_version: str
    content_hash: str
    world_id: str
    world_revision: int
    last_event_sequence: int
    state_hash: str
    learner_id: str
    agent_profile_id: str
    channel: str
    locale: str
    teaching_spec_version: str
    current_session_id: str | None
    build_policy_id: str
    compiler_profile: str
    compiler_version: str
    sandbox_image_digest: str
    test_suite_version: str
    allowed_capabilities: tuple[str, ...]
    max_source_files: int
    max_source_bytes: int
    registry_revision: int
    active_skill: ActiveSkillAuthority | None


class StudentBootstrapReader(Protocol):
    async def resolve(self, context: OperationContext) -> Result[StudentLaunchAuthority]: ...


class StudentBootstrapQueries:
    """Serialize only a fully closed durable authority returned by the PostgreSQL adapter."""

    def __init__(
        self,
        reader: StudentBootstrapReader,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._reader = reader
        self._clock = clock or (lambda: datetime.now(UTC))

    async def get(self, context: OperationContext) -> Result[Mapping[str, Any]]:
        result = await self._reader.resolve(context)
        if isinstance(result, Failure):
            return result
        authority = result.value
        actor = {
            "tenant_id": context.actor.tenant_id,
            "actor_id": context.actor.actor_id,
            "actor_type": context.actor.actor_type.value,
            "roles": list(context.actor.roles),
        }
        content = {
            "unit_id": authority.content_unit_id,
            "version": authority.content_version,
            "content_hash": authority.content_hash,
        }
        active = authority.active_skill
        return Success(
            {
                "request_context": {
                    "request_id": context.request_id,
                    "correlation_id": context.correlation_id,
                    "trace_id": context.trace_id,
                    "requested_at": _timestamp(context.requested_at),
                    "actor": actor,
                    "content_ref": content,
                    "schema_version": context.schema_version,
                },
                "api_version": "1.1.0",
                "contract_version": "0.4.0",
                "server_time": _timestamp(self._clock()),
                "actor": actor,
                "content": content,
                "capabilities": {
                    "skill_builds": True,
                    "skill_activations": True,
                    "agent_sessions": True,
                    "http_world_recovery": True,
                    "evidence_query": True,
                },
                "session": {
                    "current_session_id": authority.current_session_id,
                    "teaching_spec_version": authority.teaching_spec_version,
                    "create_request": {
                        "world_id": authority.world_id,
                        "learner_id": authority.learner_id,
                        "agent_profile_id": authority.agent_profile_id,
                        "channel": authority.channel,
                        "locale": authority.locale,
                        "content": content,
                        "expected_world_revision": authority.world_revision,
                    },
                },
                "build": {
                    "build_policy_id": authority.build_policy_id,
                    "compiler_profile": authority.compiler_profile,
                    "compiler_version": authority.compiler_version,
                    "sandbox_image_digest": authority.sandbox_image_digest,
                    "test_suite_version": authority.test_suite_version,
                    "allowed_capabilities": list(authority.allowed_capabilities),
                    "max_source_files": authority.max_source_files,
                    "max_source_bytes": authority.max_source_bytes,
                },
                "activation": {
                    "scope": {
                        "world_id": authority.world_id,
                        "agent_profile_id": authority.agent_profile_id,
                    },
                    "registry_revision": authority.registry_revision,
                    "active": None
                    if active is None
                    else {
                        "activation_id": active.activation_id,
                        "skill_id": active.skill_id,
                        "skill_version_id": active.skill_version_id,
                        "artifact_sha256": active.artifact_sha256,
                        "certification_id": active.certification_id,
                        "registry_revision": active.registry_revision,
                        "activated_at": _timestamp(active.activated_at),
                    },
                },
                "world": {
                    "world_id": authority.world_id,
                    "revision": authority.world_revision,
                    "last_event_sequence": authority.last_event_sequence,
                    "state_hash": authority.state_hash,
                    "snapshot_url": f"/v1/worlds/{authority.world_id}/snapshot",
                    "events_url": f"/v1/worlds/{authority.world_id}/events",
                },
            }
        )


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("student bootstrap timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "ActiveSkillAuthority",
    "StudentBootstrapQueries",
    "StudentBootstrapReader",
    "StudentLaunchAuthority",
]
