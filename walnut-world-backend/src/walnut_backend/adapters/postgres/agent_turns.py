"""Atomically accept an Agent Turn and advance its owning Session cursor."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    CommandCreateReceipt,
    ContentRef,
    Failure,
    NewCommand,
    OperationContext,
    Result,
    SkillRef,
    Success,
    canonical_json_sha256,
)

from .activation_authority import (
    ActivationAuthorityNotFound,
    load_current_activation_authority,
)
from .command_store import PostgresCommandStore
from .models import (
    AgentProfileRow,
    AgentSessionRow,
    AgentTurnRow,
    BuildPolicyRow,
    CurrentSessionBindingRow,
    IdempotencyReceiptRow,
    LaunchAuthorityRow,
    SkillCertificationRevocationRow,
    WorldSnapshotRow,
    request_context_data,
    world_snapshot_from_data,
)
from .product_workspaces import refresh_workspace_in_session
from .session_binding_authority import (
    current_session_binding_matches,
    current_session_binding_observed_at,
)
from .workflow_jobs import PostgresWorkflowJobStore, WorkflowInvariantError


class PostgresAgentTurnStore:
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
        session_id: str,
        command: NewCommand,
        request_body: Mapping[str, Any],
        context: OperationContext,
    ) -> Result[CommandCreateReceipt]:
        async with self._sessions() as session, session.begin():
            observed_at = await current_session_binding_observed_at(session)
            if observed_at is None:
                return Failure(_invariant("PostgreSQL returned an invalid binding timestamp"))
            tenant_id, actor_id, operation, idempotency_key = command.idempotency_scope(context)
            existing_receipt = await session.scalar(
                select(IdempotencyReceiptRow).where(
                    IdempotencyReceiptRow.tenant_id == tenant_id,
                    IdempotencyReceiptRow.actor_id == actor_id,
                    IdempotencyReceiptRow.operation == operation,
                    IdempotencyReceiptRow.idempotency_key == idempotency_key,
                )
            )
            owner = await session.scalar(
                select(AgentSessionRow)
                .where(
                    AgentSessionRow.session_id == session_id,
                    AgentSessionRow.tenant_id == context.actor.tenant_id,
                    AgentSessionRow.actor_id == context.actor.actor_id,
                )
                .with_for_update()
            )
            if owner is None:
                return Failure(_not_found())
            if owner.status != "ACTIVE":
                return Failure(_invalid("session is not active"))
            if owner.world_id != request_body.get("world_id", owner.world_id):
                return Failure(_invalid("turn cannot select another world"))
            durable_content = owner.session_json.get("content")
            if not isinstance(durable_content, Mapping):
                return Failure(_invariant("Session content authority is corrupt"))
            try:
                session_content = ContentRef(**durable_content)
            except (TypeError, ValueError):
                return Failure(_invariant("Session content authority is corrupt"))
            content_hash = session_content.content_hash
            command_context = replace(context, content_ref=session_content)
            binding = await session.scalar(
                select(CurrentSessionBindingRow).where(
                    CurrentSessionBindingRow.tenant_id == context.actor.tenant_id,
                    CurrentSessionBindingRow.session_id == session_id,
                    CurrentSessionBindingRow.actor_id == context.actor.actor_id,
                    CurrentSessionBindingRow.content_hash == content_hash,
                )
            )
            if binding is None:
                return Failure(_invalid("session is not the server-selected current Session"))
            authority = await session.scalar(
                select(LaunchAuthorityRow).where(
                    LaunchAuthorityRow.tenant_id == context.actor.tenant_id,
                    LaunchAuthorityRow.authority_id == binding.authority_id,
                    LaunchAuthorityRow.actor_id == binding.actor_id,
                    LaunchAuthorityRow.content_hash == binding.content_hash,
                    LaunchAuthorityRow.world_id == binding.world_id,
                    LaunchAuthorityRow.agent_profile_id == binding.agent_profile_id,
                    LaunchAuthorityRow.active.is_(True),
                )
            )
            if authority is None:
                return Failure(_invalid("Session launch authority is no longer active"))
            if not current_session_binding_matches(
                binding,
                owner=owner,
                authority=authority,
                observed_at=observed_at,
            ):
                return Failure(_invariant("Session binding authority is corrupt"))
            if existing_receipt is not None:
                return await self._command_store.accept_once_in_session(session, command, context)
            agent_profile = await session.scalar(
                select(AgentProfileRow).where(
                    AgentProfileRow.tenant_id == authority.tenant_id,
                    AgentProfileRow.agent_profile_id == authority.agent_profile_id,
                    AgentProfileRow.actor_id == authority.actor_id,
                    AgentProfileRow.content_hash == authority.content_hash,
                )
            )
            if agent_profile is None or agent_profile.profile_sha256 != canonical_json_sha256(
                agent_profile.profile_json
            ):
                return Failure(_invariant("Session Agent profile authority is missing or corrupt"))
            prompt_version = agent_profile.profile_json.get("prompt_version")
            model_version = agent_profile.profile_json.get("model_version")
            provider = agent_profile.profile_json.get("provider")
            if not all(
                isinstance(value, str) and value
                for value in (prompt_version, model_version, provider)
            ):
                return Failure(
                    _invariant("Session Agent profile has no provider/model/prompt closure")
                )
            world = await session.scalar(
                select(WorldSnapshotRow).where(
                    WorldSnapshotRow.tenant_id == context.actor.tenant_id,
                    WorldSnapshotRow.world_id == binding.world_id,
                    WorldSnapshotRow.actor_id == binding.actor_id,
                    WorldSnapshotRow.content_hash == binding.content_hash,
                )
            )
            if world is None:
                return Failure(_invariant("Session World authority is missing"))
            if request_body.get("expected_world_revision") != world.revision:
                return Failure(_world_conflict())
            client_state = request_body.get("client_state")
            if (
                not isinstance(client_state, Mapping)
                or client_state.get("last_event_sequence") != world.last_event_sequence
            ):
                return Failure(_event_gap())
            # A hint asks the teaching roles to explain the student's current
            # situation.  It never compiles or executes the Skill, so it is the
            # one Turn that declares no binding at all, and the server adopts
            # its own Registry head as the Skill the hint is about.  A declared
            # binding always means "execute this Skill" regardless of the input
            # type, and is still validated against that same head.
            turn_input = request_body.get("input")
            bindings = request_body.get("skill_bindings")
            if not isinstance(bindings, list):
                return Failure(_not_certified())
            is_hint_request = (
                not bindings
                and isinstance(turn_input, Mapping)
                and turn_input.get("type") == "MESSAGE"
            )
            requested_skill: SkillRef | None = None
            if not is_hint_request:
                if len(bindings) != 1 or not isinstance(bindings[0], Mapping):
                    return Failure(_not_certified())
                try:
                    requested_skill = SkillRef(**dict(bindings[0]))
                except (TypeError, ValueError):
                    return Failure(_not_certified())
            try:
                active = await load_current_activation_authority(
                    session,
                    tenant_id=context.actor.tenant_id,
                    actor_id=binding.actor_id,
                    content_hash=binding.content_hash,
                    world_id=binding.world_id,
                    agent_profile_id=binding.agent_profile_id,
                    authority_id=binding.authority_id,
                    skill_ref=requested_skill,
                )
            except ActivationAuthorityNotFound:
                return Failure(_not_certified())
            except WorkflowInvariantError as error:
                return Failure(_invariant(str(error)))
            activation = active.activation
            revoked = await session.scalar(
                select(
                    exists().where(
                        SkillCertificationRevocationRow.tenant_id == context.actor.tenant_id,
                        SkillCertificationRevocationRow.certification_id
                        == activation.certification_id,
                    )
                )
            )
            if revoked is True:
                return Failure(_not_certified())
            policy = await session.scalar(
                select(BuildPolicyRow).where(
                    BuildPolicyRow.tenant_id == context.actor.tenant_id,
                    BuildPolicyRow.build_policy_id == authority.build_policy_id,
                    BuildPolicyRow.actor_id == authority.actor_id,
                    BuildPolicyRow.content_hash == authority.content_hash,
                    BuildPolicyRow.active.is_(True),
                )
            )
            if policy is None:
                return Failure(_invariant("Session Build policy authority is missing"))
            try:
                world_snapshot = world_snapshot_from_data(world.snapshot_json)
            except (KeyError, TypeError, ValueError):
                return Failure(_invariant("Session World snapshot is corrupt"))
            prior_turn = await session.scalar(
                select(AgentTurnRow).where(
                    AgentTurnRow.tenant_id == context.actor.tenant_id,
                    AgentTurnRow.session_id == session_id,
                    AgentTurnRow.turn_id == request_body["turn_id"],
                )
            )
            if prior_turn is not None:
                return Failure(_invalid("turn_id already exists for this session"))
            current_sequence = int(owner.session_json["last_turn_sequence"])
            expected_sequence = current_sequence + 1
            client_sequence = int(client_state["client_turn_sequence"])
            if client_sequence != expected_sequence:
                return Failure(_invalid("client_turn_sequence must be the next session sequence"))
            effective_command = replace(
                command,
                versions=replace(
                    command.versions,
                    policy_version=authority.build_policy_id,
                    world_rules_version=world_snapshot.world_rules_version,
                    teaching_spec_version=authority.teaching_spec_version,
                    skill_version=activation.skill_version_id,
                    artifact_sha256=activation.artifact_sha256,
                    compiler_version=policy.compiler_version,
                    sandbox_image_digest=policy.sandbox_image_digest,
                    test_suite_version=policy.test_suite_version,
                    prompt_version=prompt_version,
                    model_version=model_version,
                ),
            )
            command_result = await self._command_store.accept_once_in_session(
                session, effective_command, command_context
            )
            if isinstance(command_result, Failure):
                return command_result
            receipt = command_result.value
            if receipt.created:
                updated = dict(owner.session_json)
                updated["last_turn_sequence"] = expected_sequence
                updated["updated_at"] = (
                    receipt.command.updated_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
                )
                owner.session_json = updated
                owner.updated_at = receipt.command.updated_at
                session.add(
                    AgentTurnRow(
                        tenant_id=context.actor.tenant_id,
                        actor_id=context.actor.actor_id,
                        session_id=session_id,
                        turn_id=request_body["turn_id"],
                        command_id=receipt.command.command_id,
                        turn_sequence=expected_sequence,
                        created_at=receipt.command.accepted_at,
                        request_json=dict(request_body),
                    )
                )
                await session.flush()
                await refresh_workspace_in_session(
                    session,
                    tenant_id=context.actor.tenant_id,
                    actor_id=context.actor.actor_id,
                    session_id=session_id,
                    updated_at=receipt.command.updated_at,
                )
                await self._workflow_jobs.enqueue_in_session(
                    session,
                    tenant_id=context.actor.tenant_id,
                    command_id=receipt.command.command_id,
                    operation=effective_command.command_type,
                    subject_type="AGENT_TURN",
                    subject_id=request_body["turn_id"],
                    request_sha256=effective_command.request_sha256,
                    job={
                        "schema_version": "1.0.0",
                        "request_context": request_context_data(receipt.command.request_context),
                        "session_id": session_id,
                        "turn_id": request_body["turn_id"],
                        "turn_sequence": expected_sequence,
                        "request": dict(request_body),
                    },
                )
            return Success(receipt)


def _not_found() -> Any:
    return _error("NOT_FOUND", "READ", "agent session not found")


def _invalid(message: str) -> Any:
    return _error("INVALID_REQUEST", "VALIDATE", message)


def _invariant(message: str) -> Any:
    return _error("INVARIANT_VIOLATION", "VALIDATE", message)


def _world_conflict() -> Any:
    return _error("WORLD_REVISION_CONFLICT", "WORLD_VALIDATE", "World revision is stale")


def _event_gap() -> Any:
    return _error("EVENT_SEQUENCE_GAP", "WORLD_VALIDATE", "World event cursor is stale")


def _not_certified() -> Any:
    return _error("SKILL_NOT_CERTIFIED", "REGISTRY", "Skill binding is not active and certified")


def _error(code: str, stage: str, message: str) -> Any:
    from yaya_agent_contracts import ContractError, ErrorCategory

    metadata = {
        "NOT_FOUND": (ErrorCategory.VALIDATION, False, "resource.not_found"),
        "INVALID_REQUEST": (ErrorCategory.VALIDATION, False, "request.invalid"),
        "INVARIANT_VIOLATION": (
            ErrorCategory.INVARIANT,
            False,
            "system.invariant_violation",
        ),
        "WORLD_REVISION_CONFLICT": (
            ErrorCategory.CONCURRENCY,
            True,
            "world.changed_retry",
        ),
        "EVENT_SEQUENCE_GAP": (
            ErrorCategory.CONCURRENCY,
            True,
            "event.resync_required",
        ),
        "SKILL_NOT_CERTIFIED": (ErrorCategory.SKILL, False, "skill.not_certified"),
    }[code]
    return ContractError(
        code=code,
        category=metadata[0],
        retryable=metadata[1],
        user_message_key=metadata[2],
        stage=stage,
        message=message,
    )
