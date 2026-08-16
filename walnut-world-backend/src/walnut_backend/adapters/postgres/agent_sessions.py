"""Atomic creation and authorized retrieval for Agent Session resources."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    CommandCreateReceipt,
    CommandRecord,
    CommandStatus,
    Failure,
    NewCommand,
    OperationContext,
    Result,
    Success,
    VersionSet,
    canonical_json_sha256,
)

from .command_store import PostgresCommandStore, validated_command_record
from .models import (
    AgentProfileRow,
    AgentSessionRow,
    AgentTurnRow,
    BuildPolicyRow,
    CommandRow,
    CurrentSessionBindingRow,
    IdempotencyReceiptRow,
    LaunchAuthorityRow,
    LearnerProfileRow,
    WorkflowJobRow,
    WorldSnapshotRow,
    json_value,
    request_context_data,
)
from .session_binding_authority import (
    current_session_binding_matches,
    current_session_binding_observed_at,
)
from .workflow_jobs import PostgresWorkflowJobStore


class PostgresAgentSessionStore:
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
        request_body: Mapping[str, Any],
        context: OperationContext,
    ) -> Result[tuple[dict[str, Any], CommandCreateReceipt]]:
        async with self._sessions() as session, session.begin():
            observed_at = await current_session_binding_observed_at(session)
            if observed_at is None:
                return Failure(
                    _invariant("ACCEPT", "PostgreSQL returned an invalid binding timestamp")
                )
            tenant_id, actor_id, operation, idempotency_key = command.idempotency_scope(context)
            replay = await session.scalar(
                select(IdempotencyReceiptRow).where(
                    IdempotencyReceiptRow.tenant_id == tenant_id,
                    IdempotencyReceiptRow.actor_id == actor_id,
                    IdempotencyReceiptRow.operation == operation,
                    IdempotencyReceiptRow.idempotency_key == idempotency_key,
                )
            )
            effective_command = command
            if replay is None:
                authority_result = await _session_accept_authority(session, request_body, context)
                if isinstance(authority_result, Failure):
                    return authority_result
                effective_command = replace(command, versions=authority_result.value)
            command_result = await self._command_store.accept_once_in_session(
                session, effective_command, context
            )
            if isinstance(command_result, Failure):
                return command_result
            receipt = command_result.value
            if receipt.created:
                resource = _initial_session(receipt, request_body)
                session.add(
                    AgentSessionRow(
                        session_id=resource["session_id"],
                        tenant_id=context.actor.tenant_id,
                        actor_id=context.actor.actor_id,
                        command_id=receipt.command.command_id,
                        world_id=resource["world_id"],
                        status=resource["status"],
                        created_at=receipt.command.accepted_at,
                        updated_at=receipt.command.updated_at,
                        session_json=resource,
                    )
                )
                await session.flush()
                await self._workflow_jobs.enqueue_in_session(
                    session,
                    tenant_id=context.actor.tenant_id,
                    command_id=receipt.command.command_id,
                    operation=effective_command.command_type,
                    subject_type="AGENT_SESSION",
                    subject_id=resource["session_id"],
                    request_sha256=effective_command.request_sha256,
                    job={
                        "schema_version": "1.0.0",
                        "request_context": request_context_data(receipt.command.request_context),
                        "session_id": resource["session_id"],
                        "request": dict(request_body),
                    },
                )
                return Success((resource, receipt))
            row = await session.scalar(
                select(AgentSessionRow).where(
                    AgentSessionRow.tenant_id == context.actor.tenant_id,
                    AgentSessionRow.actor_id == context.actor.actor_id,
                    AgentSessionRow.command_id == receipt.command.command_id,
                )
            )
            if row is None:
                return Failure(
                    _invariant("ACCEPT", "accepted session command has no durable Session")
                )
            binding = await session.scalar(
                select(CurrentSessionBindingRow).where(
                    CurrentSessionBindingRow.tenant_id == row.tenant_id,
                    CurrentSessionBindingRow.session_id == row.session_id,
                )
            )
            if receipt.command.status is CommandStatus.APPLIED:
                authority = await session.scalar(
                    select(LaunchAuthorityRow).where(
                        LaunchAuthorityRow.tenant_id == row.tenant_id,
                        LaunchAuthorityRow.actor_id == row.actor_id,
                        LaunchAuthorityRow.content_unit_id == context.content_ref.unit_id,
                        LaunchAuthorityRow.content_version == context.content_ref.version,
                        LaunchAuthorityRow.content_hash == context.content_ref.content_hash,
                        LaunchAuthorityRow.world_id == request_body.get("world_id"),
                        LaunchAuthorityRow.learner_id == request_body.get("learner_id"),
                        LaunchAuthorityRow.agent_profile_id == request_body.get("agent_profile_id"),
                        LaunchAuthorityRow.channel == request_body.get("channel"),
                        LaunchAuthorityRow.active.is_(True),
                    )
                )
                if authority is None or not current_session_binding_matches(
                    binding,
                    owner=row,
                    authority=authority,
                    observed_at=observed_at,
                ):
                    return Failure(_invariant("ACCEPT", "agent session binding authority drifted"))
            elif binding is not None:
                return Failure(_invariant("ACCEPT", "non-applied Session has a current binding"))
            return Success((row.session_json, receipt))

    async def get(self, session_id: str, context: OperationContext) -> Result[dict[str, Any]]:
        async with self._sessions() as session:
            observed_at = await current_session_binding_observed_at(session)
            if observed_at is None:
                return Failure(
                    _invariant("READ", "PostgreSQL returned an invalid binding timestamp")
                )
            row = await session.scalar(
                select(AgentSessionRow).where(
                    AgentSessionRow.session_id == session_id,
                    AgentSessionRow.tenant_id == context.actor.tenant_id,
                    AgentSessionRow.actor_id == context.actor.actor_id,
                )
            )
            command = (
                await session.scalar(
                    select(CommandRow).where(
                        CommandRow.command_id == row.command_id,
                        CommandRow.tenant_id == row.tenant_id,
                        CommandRow.actor_id == row.actor_id,
                    )
                )
                if row is not None
                else None
            )
            record = (
                await validated_command_record(session, command) if command is not None else None
            )
            job = (
                await session.scalar(
                    select(WorkflowJobRow).where(
                        WorkflowJobRow.tenant_id == row.tenant_id,
                        WorkflowJobRow.command_id == row.command_id,
                    )
                )
                if row is not None
                else None
            )
            max_turn_sequence = (
                await session.scalar(
                    select(func.max(AgentTurnRow.turn_sequence)).where(
                        AgentTurnRow.tenant_id == row.tenant_id,
                        AgentTurnRow.session_id == row.session_id,
                    )
                )
                if row is not None
                else None
            )
            foreign_turn = (
                await session.scalar(
                    select(AgentTurnRow.turn_row_id)
                    .where(
                        AgentTurnRow.tenant_id == row.tenant_id,
                        AgentTurnRow.session_id == row.session_id,
                        AgentTurnRow.actor_id != row.actor_id,
                    )
                    .limit(1)
                )
                if row is not None
                else None
            )
            request = (
                job.job_json.get("request")
                if job is not None and isinstance(job.job_json, Mapping)
                else None
            )
            authorities = (
                list(
                    (
                        await session.scalars(
                            select(LaunchAuthorityRow)
                            .where(
                                LaunchAuthorityRow.tenant_id == row.tenant_id,
                                LaunchAuthorityRow.actor_id == row.actor_id,
                                LaunchAuthorityRow.content_hash
                                == record.request_context.content_ref.content_hash,
                                LaunchAuthorityRow.world_id == request.get("world_id"),
                                LaunchAuthorityRow.learner_id == request.get("learner_id"),
                                LaunchAuthorityRow.agent_profile_id
                                == request.get("agent_profile_id"),
                                LaunchAuthorityRow.channel == request.get("channel"),
                                LaunchAuthorityRow.active.is_(True),
                            )
                            .limit(2)
                        )
                    ).all()
                )
                if row is not None and record is not None and isinstance(request, Mapping)
                else []
            )
            binding = (
                await session.scalar(
                    select(CurrentSessionBindingRow).where(
                        CurrentSessionBindingRow.tenant_id == row.tenant_id,
                        CurrentSessionBindingRow.session_id == row.session_id,
                    )
                )
                if row is not None
                else None
            )
        if row is None:
            return Failure(_not_found())
        if (
            command is None
            or record is None
            or job is None
            or foreign_turn is not None
            or not _session_authority_matches(
                row,
                command,
                record,
                job,
                max_turn_sequence if max_turn_sequence is not None else 0,
                authorities,
                binding,
                observed_at,
            )
        ):
            return Failure(_invariant("READ", "agent session durable authority drifted"))
        return Success(row.session_json)


async def _session_accept_authority(
    session: AsyncSession,
    request_body: Mapping[str, Any],
    context: OperationContext,
) -> Result[VersionSet]:
    authorities = list(
        (
            await session.scalars(
                select(LaunchAuthorityRow)
                .where(
                    LaunchAuthorityRow.tenant_id == context.actor.tenant_id,
                    LaunchAuthorityRow.actor_id == context.actor.actor_id,
                    LaunchAuthorityRow.content_unit_id == context.content_ref.unit_id,
                    LaunchAuthorityRow.content_version == context.content_ref.version,
                    LaunchAuthorityRow.content_hash == context.content_ref.content_hash,
                    LaunchAuthorityRow.world_id == request_body.get("world_id"),
                    LaunchAuthorityRow.learner_id == request_body.get("learner_id"),
                    LaunchAuthorityRow.agent_profile_id == request_body.get("agent_profile_id"),
                    LaunchAuthorityRow.channel == request_body.get("channel"),
                    LaunchAuthorityRow.active.is_(True),
                )
                .limit(2)
            )
        ).all()
    )
    if not authorities:
        return Failure(_mismatch("Session request is outside the active launch authority"))
    if len(authorities) != 1:
        return Failure(_invariant("POLICY", "Session launch authority is ambiguous"))
    authority = authorities[0]
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
    profile = await session.scalar(
        select(AgentProfileRow).where(
            AgentProfileRow.tenant_id == authority.tenant_id,
            AgentProfileRow.agent_profile_id == authority.agent_profile_id,
            AgentProfileRow.actor_id == authority.actor_id,
            AgentProfileRow.content_hash == authority.content_hash,
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
    if policy is None or world is None or profile is None or learner is None:
        return Failure(_invariant("POLICY", "Session version authority closure is incomplete"))
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
        return Failure(_invariant("POLICY", "Session BuildPolicy authority drifted"))
    world_json = world.snapshot_json
    world_origin = world_json.get("request_context")
    world_actor = world_origin.get("actor") if isinstance(world_origin, Mapping) else None
    world_content = world_origin.get("content_ref") if isinstance(world_origin, Mapping) else None
    world_rules_version = world_json.get("world_rules_version")
    if (
        not isinstance(world_actor, Mapping)
        or not isinstance(world_content, Mapping)
        or world_actor.get("tenant_id") != world.tenant_id
        or world_actor.get("actor_id") != world.actor_id
        or world_content.get("unit_id") != authority.content_unit_id
        or world_content.get("version") != authority.content_version
        or world_content.get("content_hash") != world.content_hash
        or world_json.get("world_id") != world.world_id
        or world_json.get("revision") != world.revision
        or world_json.get("last_event_sequence") != world.last_event_sequence
        or world_json.get("state_hash") != world.state_hash
        or not isinstance(world_rules_version, str)
        or not world_rules_version
    ):
        return Failure(_invariant("POLICY", "Session World authority drifted"))
    expected_revision = request_body.get("expected_world_revision")
    if expected_revision is not None and expected_revision != world.revision:
        return Failure(_mismatch("Session expected_world_revision is stale"))
    profile_json = profile.profile_json
    provider = profile_json.get("provider")
    model_version = profile_json.get("model_version")
    prompt_version = profile_json.get("prompt_version")
    if (
        canonical_json_sha256(profile_json) != profile.profile_sha256
        or profile_json.get("agent_profile_id") != profile.agent_profile_id
        or any(
            not isinstance(value, str) or not value
            for value in (provider, model_version, prompt_version)
        )
    ):
        return Failure(_invariant("POLICY", "Session AgentProfile authority drifted"))
    locale = learner.profile_json.get("locale")
    if not isinstance(locale, str) or request_body.get("locale") != locale:
        return Failure(_mismatch("Session locale differs from learner authority"))
    return Success(
        VersionSet(
            api_version=context.schema_version,
            event_version="1",
            policy_version=policy.build_policy_id,
            world_rules_version=world_rules_version,
            teaching_spec_version=authority.teaching_spec_version,
            compiler_version=policy.compiler_version,
            sandbox_image_digest=policy.sandbox_image_digest,
            test_suite_version=policy.test_suite_version,
            prompt_version=prompt_version,
            model_version=model_version,
        )
    )


def _initial_session(
    receipt: CommandCreateReceipt, request_body: Mapping[str, Any]
) -> dict[str, Any]:
    command = receipt.command
    session_id = f"session_{hashlib.sha256(command.command_id.encode('utf-8')).hexdigest()[:24]}"
    timestamp = command.accepted_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return {
        "request_context": request_context_data(command.request_context),
        "session_id": session_id,
        "world_id": request_body["world_id"],
        "learner_id": request_body["learner_id"],
        "agent_profile_id": request_body["agent_profile_id"],
        "channel": request_body["channel"],
        "status": "ACTIVE",
        "created_at": timestamp,
        "updated_at": timestamp,
        "last_turn_sequence": 0,
        "content": request_body["content"],
        "versions": {
            key: value for key, value in json_value(command.versions).items() if value is not None
        },
        "links": {
            "self": f"/v1/agent-sessions/{session_id}",
            "turns": f"/v1/agent-sessions/{session_id}/turns",
            "world_snapshot": f"/v1/worlds/{request_body['world_id']}/snapshot",
        },
    }


def _session_authority_matches(
    row: AgentSessionRow,
    command: CommandRow,
    record: CommandRecord,
    job: WorkflowJobRow,
    max_turn_sequence: int,
    authorities: list[LaunchAuthorityRow],
    binding: CurrentSessionBindingRow | None,
    observed_at: datetime,
) -> bool:
    value = row.session_json
    origin = value.get("request_context")
    actor = origin.get("actor") if isinstance(origin, Mapping) else None
    content = origin.get("content_ref") if isinstance(origin, Mapping) else None
    command_origin = command.record_json.get("request_context")
    try:
        created_at = _timestamp(value.get("created_at"))
        updated_at = _timestamp(value.get("updated_at"))
    except (TypeError, ValueError):
        return False
    expected_id = f"session_{hashlib.sha256(row.command_id.encode('utf-8')).hexdigest()[:24]}"
    versions = json_value(record.versions)
    request = job.job_json.get("request")
    if not isinstance(request, Mapping):
        return False
    expected_job = {
        "schema_version": "1.0.0",
        "request_context": request_context_data(record.request_context),
        "session_id": row.session_id,
        "request": dict(request),
    }
    last_turn_sequence = value.get("last_turn_sequence")
    base_matches = (
        isinstance(actor, Mapping)
        and actor.get("tenant_id") == row.tenant_id
        and actor.get("actor_id") == row.actor_id
        and isinstance(content, Mapping)
        and value.get("content") == content
        and origin == command_origin == request_context_data(record.request_context)
        and isinstance(versions, dict)
        and value.get("versions")
        == {key: item for key, item in versions.items() if item is not None}
        and value.get("session_id") == row.session_id == expected_id
        and value.get("world_id") == row.world_id
        and value.get("world_id") == request.get("world_id")
        and value.get("learner_id") == request.get("learner_id")
        and value.get("agent_profile_id") == request.get("agent_profile_id")
        and value.get("channel") == request.get("channel")
        and value.get("content") == request.get("content")
        and value.get("status") == row.status
        and not isinstance(last_turn_sequence, bool)
        and isinstance(last_turn_sequence, int)
        and last_turn_sequence == max_turn_sequence
        and created_at == row.created_at
        and updated_at == row.updated_at
        and record.command_type == "CREATE_AGENT_SESSION"
        and job.tenant_id == row.tenant_id
        and job.command_id == row.command_id
        and job.operation == record.command_type
        and job.subject_type == "AGENT_SESSION"
        and job.subject_id == row.session_id
        and job.job_json == expected_job
    )
    if not base_matches:
        return False
    if record.status is CommandStatus.APPLIED:
        if (
            not record.terminal
            or job.status != "SUCCEEDED"
            or job.phase != "COMPLETE"
            or row.status != "ACTIVE"
            or len(authorities) != 1
            or binding is None
        ):
            return False
        authority = authorities[0]
        return (
            authority.content_unit_id == record.request_context.content_ref.unit_id
            and authority.content_version == record.request_context.content_ref.version
            and current_session_binding_matches(
                binding,
                owner=row,
                authority=authority,
                observed_at=observed_at,
            )
        )
    if record.terminal:
        return (
            record.status
            in {
                CommandStatus.REJECTED,
                CommandStatus.FAILED,
                CommandStatus.CANCELLED,
                CommandStatus.UNKNOWN,
            }
            and job.status in {"FAILED", "CANCELLED", "DEAD_LETTER"}
            and row.status == "FAILED"
            and binding is None
        )
    return (
        job.status not in {"SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTER"}
        and row.status == "ACTIVE"
    )


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("timestamp must be a string")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return result


def _not_found() -> Any:
    return _error("NOT_FOUND", "READ", "agent session not found", retryable=False)


def _invariant(stage: str, message: str) -> Any:
    return _error("INVARIANT_VIOLATION", stage, message, retryable=False)


def _mismatch(message: str) -> Any:
    return _error("CONTENT_VERSION_MISMATCH", "POLICY", message, retryable=False)


def _error(code: str, stage: str, message: str, *, retryable: bool) -> Any:
    from yaya_agent_contracts import ContractError, ErrorCategory

    metadata = {
        "NOT_FOUND": (ErrorCategory.VALIDATION, "resource.not_found"),
        "CONTENT_VERSION_MISMATCH": (
            ErrorCategory.VALIDATION,
            "content.version_mismatch",
        ),
        "INVARIANT_VIOLATION": (ErrorCategory.INVARIANT, "system.invariant_violation"),
    }[code]
    return ContractError(
        code=code,
        category=metadata[0],
        retryable=retryable,
        user_message_key=metadata[1],
        stage=stage,
        message=message,
    )
