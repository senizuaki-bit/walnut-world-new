"""Atomic Sandbox -> World -> Run/Evidence/idempotency application service."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Never, cast

import psycopg
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from yaya_agent_contracts import (
    ActiveSkill,
    ActorRef,
    CertifiedSkill,
    CommandRecord,
    CommandStatus,
    ContractError,
    ErrorCategory,
    EvidenceRef,
    EvidenceType,
    Failure,
    FrozenJsonObject,
    OperationContext,
    RequestContext,
    SandboxLimits,
    SandboxPort,
    SandboxRunRequest,
    SandboxRunResult,
    SkillRef,
    Success,
    UncommittedEvent,
    VersionSet,
    WaterIntent,
    WorldAtomicCommit,
    WorldCommand,
    WorldCommitReceipt,
    WorldSnapshot,
    canonical_json_sha256,
)
from yaya_agent_runtime.domain import (
    RunResultSnapshot,
    SessionSnapshot,
    SkillInvocationRequest,
    SkillInvocationResult,
    SkillSnapshot,
    world_commit_receipt_sha256,
)
from yaya_agent_runtime.errors import AgentPersistenceError, AgentToolExecutionError

from .codec import decode_as, encode, plain
from .database import (
    PostgresCommitStateUnknown,
    PostgresDatabase,
    transaction_with_commit_boundary_on,
)
from .wire import ContractSchemaValidator
from .world import StagedWateringProposal, WateringWorldEngine, WorldRuleViolation
from .world_uow import PostgresWorldUnitOfWork, world_commit_identifier


def _stable_authority(left: ActorRef, right: ActorRef) -> bool:
    return (
        left.tenant_id,
        left.actor_id,
        left.actor_type,
    ) == (
        right.tenant_id,
        right.actor_id,
        right.actor_type,
    )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _identifier(prefix: str, value: str, length: int = 24) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def _scoped_identifier(prefix: str, *parts: str) -> str:
    framed = "".join(f"{len(part)}:{part}" for part in parts)
    return f"{prefix}_{hashlib.sha256(framed.encode('utf-8')).hexdigest()[:24]}"


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AgentPersistenceError(
            "AGENT_STORED_RECORD_INVALID",
            f"{label} must be an object",
        )
    return cast(Mapping[str, object], value)


def _versions_wire(versions: VersionSet) -> dict[str, object]:
    raw = cast(Mapping[str, object], plain(versions))
    return {key: value for key, value in raw.items() if value is not None}


def _context_wire(context: OperationContext | RequestContext) -> dict[str, object]:
    return {
        "schema_version": context.schema_version,
        "request_id": context.request_id,
        "correlation_id": context.correlation_id,
        "trace_id": context.trace_id,
        "requested_at": _iso(context.requested_at),
        "actor": {
            "tenant_id": context.actor.tenant_id,
            "actor_id": context.actor.actor_id,
            "actor_type": context.actor.actor_type.value,
            "roles": list(context.actor.roles),
        },
        "content_ref": {
            "unit_id": context.content_ref.unit_id,
            "version": context.content_ref.version,
            "content_hash": context.content_ref.content_hash,
        },
    }


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


def _intent_wire(intent: WaterIntent) -> FrozenJsonObject:
    return cast(
        FrozenJsonObject,
        {
            "intent_id": intent.intent_id,
            "action_type": intent.action_type,
            "actor_entity_id": intent.actor_entity_id,
            "expected_world_revision": intent.expected_world_revision,
            "plot_id": intent.plot_id,
            "amount_ml": intent.amount_ml,
        },
    )


def _error_wire(error: ContractError) -> dict[str, object]:
    value: dict[str, object] = {
        "code": error.code,
        "category": error.category.value,
        "retryable": error.retryable,
        "user_message_key": error.user_message_key,
        "stage": error.stage,
    }
    if error.message is not None:
        value["message"] = error.message
    if error.details:
        value["details"] = plain(error.details)
    if error.evidence_ids:
        value["evidence_ids"] = list(error.evidence_ids)
    return value


def _world_rejected(reason: str) -> ContractError:
    return ContractError(
        code="WORLD_RULE_REJECTED",
        category=ErrorCategory.WORLD_RULE,
        retryable=False,
        user_message_key="world.rule_rejected",
        stage="WORLD_VALIDATE",
        message="The staged actions did not complete the watering task.",
        details={"reason": reason},
    )


def _sandbox_protocol_error(reason: str) -> ContractError:
    return ContractError(
        code="SANDBOX_RUNTIME_ERROR",
        category=ErrorCategory.SANDBOX,
        retryable=False,
        user_message_key="sandbox.runtime_error",
        stage="SANDBOX",
        message="The Sandbox returned an authority-inconsistent result.",
        details={"reason": reason},
    )


class PostgresSkillInvocationService:
    """Production SkillInvocationPort implementation.

    A PostgreSQL session advisory lock serializes equal invocation identities
    across worker processes.  The final transaction publishes World CAS, Run,
    Evidence, World events/outbox and the receipt together.  If commit response
    state is uncertain, canonical receipt reconciliation runs before surfacing
    UNKNOWN_COMMIT_STATE.
    """

    def __init__(
        self,
        *,
        database: PostgresDatabase,
        sandbox: SandboxPort,
        world_engine: WateringWorldEngine,
        world_uow: PostgresWorldUnitOfWork,
        limits: SandboxLimits,
        versions: VersionSet,
        contracts_root: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._database = database
        self._sandbox = sandbox
        self._world_engine = world_engine
        self._world_uow = world_uow
        self._limits = limits
        self._versions = versions
        self._validator = ContractSchemaValidator(Path(contracts_root))
        self._clock = clock

    async def get_result(
        self,
        invocation_id: str,
        context: OperationContext,
    ) -> SkillInvocationResult | None:
        connection: AsyncConnection[dict[str, object]] | None = None
        try:
            connection = await self._database.connect(autocommit=True)
            result = await connection.execute(
                """
                SELECT actor_id, content_hash, request_sha256, run_id, result_json
                FROM yaya_skill_invocations
                WHERE tenant_id=%s AND invocation_id=%s
                """,
                (context.actor.tenant_id, invocation_id),
            )
            row = await result.fetchone()
            if row is None:
                return None
            if (
                row["actor_id"] != context.actor.actor_id
                or row["content_hash"] != context.content_ref.content_hash
            ):
                raise AgentPersistenceError(
                    "AGENT_SKILL_RECEIPT_AUTHORITY_MISMATCH",
                    "Skill receipt belongs to another actor or content version",
                )
            receipt = decode_as(row["result_json"], SkillInvocationResult)
            if not _stable_authority(receipt.run.request_context.actor, context.actor):
                raise AgentPersistenceError(
                    "AGENT_SKILL_RECEIPT_AUTHORITY_MISMATCH",
                    "Skill receipt actor identity drifted in storage",
                )
            if receipt.run.request_context.content_ref != context.content_ref:
                raise AgentPersistenceError(
                    "AGENT_SKILL_RECEIPT_CONTENT_MISMATCH",
                    "Skill receipt content identity drifted in storage",
                )
            if (
                receipt.invocation_id != invocation_id
                or receipt.tenant_id != context.actor.tenant_id
                or receipt.run.command_id != context.command_id
                or row["request_sha256"] != receipt.request_sha256
                or row["run_id"] != receipt.run.run_id
            ):
                raise AgentPersistenceError(
                    "AGENT_SKILL_RECEIPT_IDENTITY_MISMATCH",
                    "Skill receipt identity drifted in storage",
                )
            return receipt
        except AgentPersistenceError:
            raise
        except psycopg.Error as error:
            raise AgentPersistenceError(
                "AGENT_SKILL_RECEIPT_LOOKUP_FAILED",
                "PostgreSQL could not reconcile the Skill receipt",
                {"exception_type": type(error).__name__},
            ) from error
        finally:
            if connection is not None:
                await connection.close()

    async def invoke(
        self,
        request: SkillInvocationRequest,
        context: OperationContext,
    ) -> SkillInvocationResult:
        self._validate_request(request, context)
        connection = await self._database.connect(autocommit=True)
        # PostgreSQL TEXT rejects NUL bytes.  Length-prefix both components so
        # the advisory identity stays unambiguous without relying on a sentinel.
        advisory_key = (
            f"tenant:{len(request.tenant_id)}:{request.tenant_id}:"
            f"invocation:{len(request.invocation_id)}:{request.invocation_id}"
        )
        try:
            await connection.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
                (advisory_key,),
            )
            replay = await self._read_locked_receipt(connection, request, context)
            if replay is not None:
                return replay
            await self._validate_session_command(connection, request, context)
            skill = await self._load_active_skill(connection, request, context)
            initial_world, initial_stream_id = await self._load_world(
                connection,
                request.world_id,
                context,
            )
            if initial_world.world_rules_version != self._versions.world_rules_version:
                raise AgentToolExecutionError(
                    "TOOL_WORLD_RULES_VERSION_MISMATCH",
                    "World rules differ from the pinned production VersionSet",
                )
            if initial_world.revision != request.expected_world_revision:
                raise AgentToolExecutionError(
                    "TOOL_WORLD_REVISION_CONFLICT",
                    "World revision changed before Sandbox execution",
                    {
                        "expected": request.expected_world_revision,
                        "actual": initial_world.revision,
                    },
                )
            run_id = _identifier("run", request.invocation_id)
            sandbox_result = await self._sandbox.run(
                SandboxRunRequest(
                    run_id=run_id,
                    skill_ref=request.skill_ref,
                    world_id=request.world_id,
                    world_snapshot=initial_world,
                    input=cast(FrozenJsonObject, request.arguments),
                    deterministic_seed=request.invocation_id,
                    limits=self._limits,
                ),
                context,
            )
            try:
                async with transaction_with_commit_boundary_on(connection):
                    concurrent = await self._read_locked_receipt(connection, request, context)
                    if concurrent is not None:
                        return concurrent
                    # The first validation deliberately does not hold database locks while
                    # untrusted code runs.  Re-lock the accepted turn before any durable
                    # side effect so cancellation, lease takeover, or identity drift during
                    # Sandbox execution fences this worker.
                    await self._validate_session_command(
                        connection,
                        request,
                        context,
                        for_update=True,
                    )
                    current_skill = await self._load_active_skill(
                        connection,
                        request,
                        context,
                        for_update=True,
                    )
                    if current_skill != skill:
                        raise AgentToolExecutionError(
                            "TOOL_SKILL_BINDING_MISMATCH",
                            "Certified active Skill changed while the Sandbox was running",
                        )
                    current_world, current_stream_id = await self._load_world(
                        connection,
                        request.world_id,
                        context,
                        for_update=True,
                    )
                    if (
                        current_world.revision != initial_world.revision
                        or current_world.state_hash != initial_world.state_hash
                        or current_world.last_event_sequence != initial_world.last_event_sequence
                        or current_stream_id != initial_stream_id
                    ):
                        raise AgentToolExecutionError(
                            "TOOL_WORLD_REVISION_CONFLICT",
                            "World CAS changed while the Sandbox was running",
                        )
                    return await self._publish(
                        connection,
                        request,
                        context,
                        skill,
                        current_world,
                        current_stream_id,
                        sandbox_result,
                        run_id,
                    )
            except AgentToolExecutionError:
                raise
            except PostgresCommitStateUnknown as error:
                try:
                    recovered = await self.get_result(request.invocation_id, context)
                except AgentPersistenceError:
                    recovered = None
                if recovered is not None:
                    if recovered.request_sha256 != request.request_sha256:
                        raise AgentToolExecutionError(
                            "TOOL_IDEMPOTENCY_KEY_REUSED",
                            "Invocation identity committed different request bytes",
                        )
                    return recovered
                raise AgentToolExecutionError(
                    "UNKNOWN_COMMIT_STATE",
                    "Skill transaction outcome is not yet reconcilable",
                    {
                        "runtime_warning": "SIDE_EFFECT_COMMIT_UNKNOWN",
                        "exception_type": type(error.__cause__).__name__,
                    },
                ) from error
            except psycopg.Error as error:
                raise AgentToolExecutionError(
                    "TOOL_PERSISTENCE_ROLLED_BACK",
                    "Skill transaction was explicitly rolled back and may be retried",
                    {
                        "commit_state": "ROLLED_BACK",
                        "runtime_warning": "SIDE_EFFECT_ROLLED_BACK",
                        "sqlstate": error.sqlstate or "UNKNOWN",
                    },
                ) from error
        finally:
            if not connection.closed:
                try:
                    await connection.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                        (advisory_key,),
                    )
                except psycopg.Error:
                    pass
                await connection.close()

    @staticmethod
    def _validate_request(
        request: SkillInvocationRequest,
        context: OperationContext,
    ) -> None:
        if request.tenant_id != context.actor.tenant_id:
            raise AgentToolExecutionError(
                "TOOL_INVOCATION_IDENTITY_MISMATCH",
                "Invocation tenant does not match authenticated authority",
            )
        if request.command_id != context.command_id:
            raise AgentToolExecutionError(
                "TOOL_INVOCATION_IDENTITY_MISMATCH",
                "Invocation command does not match OperationContext",
            )

    async def _read_locked_receipt(
        self,
        connection: AsyncConnection[dict[str, object]],
        request: SkillInvocationRequest,
        context: OperationContext,
    ) -> SkillInvocationResult | None:
        cursor = await connection.execute(
            """
            SELECT actor_id, content_hash, request_sha256, result_json
            FROM yaya_skill_invocations
            WHERE tenant_id=%s AND invocation_id=%s
            """,
            (request.tenant_id, request.invocation_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        if (
            row["actor_id"] != context.actor.actor_id
            or row["content_hash"] != context.content_ref.content_hash
        ):
            raise AgentToolExecutionError(
                "TOOL_INVOCATION_IDENTITY_MISMATCH",
                "Invocation receipt belongs to another authority",
            )
        if row["request_sha256"] != request.request_sha256:
            raise AgentToolExecutionError(
                "TOOL_IDEMPOTENCY_KEY_REUSED",
                "Invocation identity was reused with different request bytes",
            )
        receipt = decode_as(row["result_json"], SkillInvocationResult)
        if (
            receipt.invocation_id != request.invocation_id
            or receipt.tenant_id != request.tenant_id
            or receipt.request_sha256 != request.request_sha256
            or receipt.arguments != request.arguments
            or receipt.run.session_id != request.session_id
            or receipt.run.turn_id != request.turn_id
            or receipt.run.command_id != request.command_id
            or receipt.run.world_id != request.world_id
            or receipt.run.world_revision_before != request.expected_world_revision
            or receipt.run.skill_ref != request.skill_ref
        ):
            raise AgentToolExecutionError(
                "TOOL_IDEMPOTENCY_KEY_REUSED",
                "Invocation receipt identity differs from the request",
            )
        return receipt

    async def _validate_session_command(
        self,
        connection: AsyncConnection[dict[str, object]],
        request: SkillInvocationRequest,
        context: OperationContext,
        *,
        for_update: bool = False,
    ) -> None:
        lock_clause = " FOR UPDATE OF s, c" if for_update else ""
        cursor = await connection.execute(
            """
            SELECT s.world_id, s.snapshot_json AS session_json,
                   c.session_id AS command_session_id, c.turn_id AS command_turn_id,
                   c.status, c.revision, c.record_json,
                   EXISTS (
                     SELECT 1 FROM yaya_public_agent_sessions p
                     WHERE p.tenant_id=c.tenant_id AND p.session_id=c.session_id
                   ) AS public_scope
            FROM yaya_agent_sessions s
            JOIN yaya_commands c
              ON c.tenant_id=s.tenant_id AND c.session_id=s.session_id
             AND c.actor_id=s.actor_id AND c.content_hash=s.content_hash
            WHERE s.tenant_id=%s AND s.session_id=%s AND s.actor_id=%s
              AND s.content_hash=%s AND c.command_id=%s
            """
            + lock_clause,
            (
                request.tenant_id,
                request.session_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
                request.command_id,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            raise AgentToolExecutionError(
                "TOOL_INVOCATION_IDENTITY_MISMATCH",
                "Session and Command do not form one authority-closed turn",
            )
        session = decode_as(row["session_json"], SessionSnapshot)
        command = decode_as(row["record_json"], CommandRecord)
        allowed = {
            CommandStatus.ACCEPTED,
            CommandStatus.VALIDATING,
            CommandStatus.RUNNING_SANDBOX,
            CommandStatus.APPLYING_WORLD,
        }
        if (
            row["world_id"] != request.world_id
            or session.session_id != request.session_id
            or session.world_id != request.world_id
            or not _stable_authority(session.request_context.actor, context.actor)
            or session.request_context.content_ref != context.content_ref
            or row["command_session_id"] != request.session_id
            or row["command_turn_id"] != request.turn_id
            or command.command_id != request.command_id
            or command.command_type != "EXECUTE_AGENT_TURN"
            or command.status not in allowed
            or row["status"] != command.status.value
            or row["revision"] != command.revision
            or not _stable_authority(command.request_context.actor, context.actor)
            or command.request_context.content_ref != context.content_ref
            or (
                row["public_scope"]
                and (
                    command.versions.skill_version != request.skill_ref.skill_version_id
                    or command.versions.artifact_sha256 != request.skill_ref.artifact_sha256
                )
            )
        ):
            raise AgentToolExecutionError(
                "TOOL_INVOCATION_IDENTITY_MISMATCH",
                "Session, Command, turn or World identity is inconsistent",
            )

    async def _load_active_skill(
        self,
        connection: AsyncConnection[dict[str, object]],
        request: SkillInvocationRequest,
        context: OperationContext,
        *,
        for_update: bool = False,
    ) -> SkillSnapshot:
        scoped_lock = (
            " FOR UPDATE OF p, session_scope, binding, s, h, e, c, full_c" if for_update else ""
        )
        cursor = await connection.execute(
            """
            SELECT s.snapshot_json,e.record_json AS active_json,e.entry_sha256,
                   e.revision AS active_revision,
                   e.activated_at AS active_activated_at,
                   c.record_json AS certification_json,
                   binding.binding_id,binding.session_id AS binding_session_id,
                   binding.skill_id AS binding_skill_id,
                   binding.skill_version_id AS binding_skill_version_id,
                   binding.certification_id AS binding_certification_id,
                   binding.artifact_sha256 AS binding_artifact_sha256,
                   binding.actor_id AS binding_actor_id,
                   binding.content_hash AS binding_content_hash,
                   binding.binding_sha256
            FROM yaya_public_agent_sessions p
            JOIN yaya_agent_sessions session_scope
              ON session_scope.tenant_id=p.tenant_id
             AND session_scope.session_id=p.session_id
             AND session_scope.actor_id=p.actor_id
             AND session_scope.content_hash=p.content_hash
             AND session_scope.task_id=p.task_id
             AND session_scope.world_id=p.world_id
            JOIN yaya_registry_heads h
              ON h.tenant_id=p.tenant_id AND h.actor_id=p.actor_id
             AND h.content_hash=p.content_hash AND h.world_id=p.world_id
             AND h.agent_profile_id=p.agent_profile_id AND h.skill_id=%s
            JOIN yaya_registry_entries e
              ON e.tenant_id=h.tenant_id AND e.actor_id=h.actor_id
             AND e.content_hash=h.content_hash AND e.world_id=h.world_id
             AND e.agent_profile_id=h.agent_profile_id AND e.skill_id=h.skill_id
             AND e.revision=h.revision
            JOIN yaya_session_skill_versions binding
              ON binding.tenant_id=p.tenant_id
             AND binding.session_id=p.session_id
             AND binding.actor_id=p.actor_id
             AND binding.content_hash=p.content_hash
             AND binding.skill_id=e.skill_id
             AND binding.skill_version_id=e.skill_version_id
             AND binding.certification_id=e.certification_id
             AND binding.artifact_sha256=e.artifact_sha256
            JOIN yaya_skills s
              ON s.tenant_id=e.tenant_id AND s.actor_id=e.actor_id
             AND s.content_hash=e.content_hash AND s.skill_id=e.skill_id
             AND s.skill_version_id=e.skill_version_id
             AND s.certification_id=e.certification_id
             AND s.artifact_sha256=e.artifact_sha256
            JOIN yaya_registry_certifications c
              ON c.tenant_id=s.tenant_id
             AND c.certification_id=s.certification_id
             AND c.skill_id=s.skill_id
             AND c.skill_version_id=s.skill_version_id
             AND c.artifact_sha256=s.artifact_sha256
             AND c.rejected=FALSE
            JOIN yaya_skill_certifications full_c
              ON full_c.tenant_id=s.tenant_id
             AND full_c.certification_id=s.certification_id
             AND full_c.skill_id=s.skill_id
             AND full_c.skill_version_id=s.skill_version_id
             AND full_c.artifact_sha256=s.artifact_sha256
             AND full_c.actor_id=s.actor_id AND full_c.content_hash=s.content_hash
            LEFT JOIN yaya_certification_revocations r
              ON r.tenant_id=full_c.tenant_id
             AND r.certification_id=full_c.certification_id
            WHERE p.tenant_id=%s AND p.session_id=%s AND p.actor_id=%s
              AND p.content_hash=%s AND p.world_id=%s
              AND p.status='ACTIVE'
              AND e.skill_id=%s AND e.skill_version_id=%s
              AND e.certification_id=%s AND e.artifact_sha256=%s
              AND r.certification_id IS NULL
            """
            + scoped_lock,
            (
                request.skill_ref.skill_id,
                request.tenant_id,
                request.session_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
                request.world_id,
                request.skill_ref.skill_id,
                request.skill_ref.skill_version_id,
                request.skill_ref.certification_id,
                request.skill_ref.artifact_sha256,
            ),
        )
        row = await cursor.fetchone()
        public_scope = row is not None
        if row is None:
            public_cursor = await connection.execute(
                """
                SELECT 1 FROM yaya_public_agent_sessions
                WHERE tenant_id=%s AND session_id=%s
                UNION ALL
                SELECT 1 FROM yaya_session_skill_versions
                WHERE tenant_id=%s AND session_id=%s
                LIMIT 1
                """,
                (
                    request.tenant_id,
                    request.session_id,
                    request.tenant_id,
                    request.session_id,
                ),
            )
            if await public_cursor.fetchone() is not None:
                raise AgentToolExecutionError(
                    "TOOL_SKILL_BINDING_MISMATCH",
                    "Full-scope certified active Skill binding was not found",
                )
            # Explicit legacy A6 path for Sessions without a public extension.
            legacy_lock = " FOR UPDATE OF s, a, c" if for_update else ""
            cursor = await connection.execute(
                """
                SELECT s.snapshot_json,a.record_json AS active_json,
                       NULL::text AS entry_sha256
                FROM yaya_skills s
                JOIN yaya_registry_active a
              ON a.tenant_id=s.tenant_id AND a.actor_id=s.actor_id
             AND a.skill_id=s.skill_id
            JOIN yaya_registry_certifications c
              ON c.tenant_id=s.tenant_id
             AND c.certification_id=s.certification_id
             AND c.skill_id=s.skill_id
             AND c.skill_version_id=s.skill_version_id
             AND c.artifact_sha256=s.artifact_sha256
             AND c.rejected=FALSE
                WHERE s.tenant_id=%s AND s.actor_id=%s AND s.content_hash=%s
              AND s.session_id=%s
              AND s.skill_id=%s AND s.skill_version_id=%s
              AND s.certification_id=%s AND s.artifact_sha256=%s
            """
                + legacy_lock,
                (
                    request.tenant_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    request.session_id,
                    request.skill_ref.skill_id,
                    request.skill_ref.skill_version_id,
                    request.skill_ref.certification_id,
                    request.skill_ref.artifact_sha256,
                ),
            )
            row = await cursor.fetchone()
        if row is None:
            raise AgentToolExecutionError(
                "TOOL_SKILL_BINDING_MISMATCH",
                "Certified active Skill binding was not found",
            )
        skill = decode_as(row["snapshot_json"], SkillSnapshot)
        if public_scope:
            expected_binding_id = _scoped_identifier(
                "binding",
                request.tenant_id,
                request.session_id,
                request.skill_ref.skill_id,
                request.skill_ref.skill_version_id,
            )
            binding_projection: dict[str, object] = {
                "binding_id": row["binding_id"],
                "session_id": row["binding_session_id"],
                "skill_id": row["binding_skill_id"],
                "skill_version_id": row["binding_skill_version_id"],
                "certification_id": row["binding_certification_id"],
                "artifact_sha256": row["binding_artifact_sha256"],
                "actor_id": row["binding_actor_id"],
                "content_hash": row["binding_content_hash"],
            }
            if (
                row["binding_id"] != expected_binding_id
                or row["binding_session_id"] != request.session_id
                or row["binding_skill_id"] != request.skill_ref.skill_id
                or row["binding_skill_version_id"] != request.skill_ref.skill_version_id
                or row["binding_certification_id"] != request.skill_ref.certification_id
                or row["binding_artifact_sha256"] != request.skill_ref.artifact_sha256
                or row["binding_actor_id"] != context.actor.actor_id
                or row["binding_content_hash"] != context.content_ref.content_hash
                or row["binding_sha256"] != canonical_json_sha256(binding_projection)
            ):
                raise AgentToolExecutionError(
                    "TOOL_SKILL_BINDING_MISMATCH",
                    "Session SkillVersion binding drifted",
                )
            active_wire = _mapping(row["active_json"], "active Registry entry")
            if canonical_json_sha256(active_wire) != row["entry_sha256"]:
                raise AgentToolExecutionError(
                    "TOOL_SKILL_BINDING_MISMATCH",
                    "Active Registry entry hash drifted",
                )
            certified = decode_as(row["certification_json"], CertifiedSkill)
            active_revision = row["active_revision"]
            active_activated_at = row["active_activated_at"]
            if (
                isinstance(active_revision, bool)
                or not isinstance(active_revision, int)
                or not isinstance(active_activated_at, datetime)
                or active_activated_at.tzinfo is None
                or active_activated_at.utcoffset() is None
            ):
                raise AgentToolExecutionError(
                    "TOOL_SKILL_BINDING_MISMATCH",
                    "Active Registry authority is invalid",
                )
            try:
                active = ActiveSkill(
                    skill=certified,
                    registry_revision=active_revision,
                    activated_at=active_activated_at.astimezone(UTC),
                )
            except (TypeError, ValueError) as error:
                raise AgentToolExecutionError(
                    "TOOL_SKILL_BINDING_MISMATCH",
                    "Active Registry authority is invalid",
                ) from error
            if active_wire != _mapping(plain(active), "expected active Registry entry"):
                raise AgentToolExecutionError(
                    "TOOL_SKILL_BINDING_MISMATCH",
                    "Active Registry projection drifted",
                )
        else:
            active = decode_as(row["active_json"], ActiveSkill)
        certified = active.skill
        active_ref = SkillRef(
            certified.skill_id,
            certified.skill_version_id,
            certified.artifact.artifact_sha256,
            certified.certification_id,
        )
        if skill.ref != request.skill_ref or active_ref != request.skill_ref:
            raise AgentToolExecutionError(
                "TOOL_SKILL_BINDING_MISMATCH",
                "Registry, Skill snapshot and request identity drifted",
            )
        if not _stable_authority(skill.request_context.actor, context.actor):
            raise AgentToolExecutionError(
                "TOOL_INVOCATION_IDENTITY_MISMATCH",
                "Skill belongs to another actor",
            )
        if skill.request_context.content_ref != context.content_ref:
            raise AgentToolExecutionError(
                "TOOL_INVOCATION_IDENTITY_MISMATCH",
                "Skill belongs to another content version",
            )
        return skill

    async def _load_world(
        self,
        connection: AsyncConnection[dict[str, object]],
        world_id: str,
        context: OperationContext,
        *,
        for_update: bool = False,
    ) -> tuple[WorldSnapshot, str]:
        suffix = " FOR UPDATE" if for_update else ""
        query = (
            "SELECT stream_id,revision,last_event_sequence,state_hash,world_rules_version,"
            "state_json,request_context_json,updated_at FROM yaya_worlds "
            "WHERE tenant_id=%s AND world_id=%s AND actor_id=%s AND content_hash=%s" + suffix
        )
        cursor = await connection.execute(
            query,  # pyright: ignore[reportArgumentType]
            (
                context.actor.tenant_id,
                world_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            raise AgentToolExecutionError("TOOL_WORLD_NOT_FOUND", "World was not found")
        stored_context = decode_as(row["request_context_json"], RequestContext)
        if not _stable_authority(stored_context.actor, context.actor):
            raise AgentToolExecutionError(
                "TOOL_INVOCATION_IDENTITY_MISMATCH", "World actor identity mismatch"
            )
        if stored_context.content_ref != context.content_ref:
            raise AgentToolExecutionError(
                "TOOL_INVOCATION_IDENTITY_MISMATCH", "World content identity mismatch"
            )
        state = cast(FrozenJsonObject, row["state_json"])
        if canonical_json_sha256(state) != row["state_hash"]:
            raise AgentToolExecutionError(
                "TOOL_WORLD_STATE_INVALID", "Persisted World state hash is invalid"
            )
        stream_id = row["stream_id"]
        if not isinstance(stream_id, str):
            raise AgentToolExecutionError(
                "TOOL_WORLD_STATE_INVALID",
                "Persisted World stream identity is invalid",
            )
        return (
            WorldSnapshot(
                request_context=stored_context,
                world_id=world_id,
                revision=cast(int, row["revision"]),
                last_event_sequence=cast(int, row["last_event_sequence"]),
                state_hash=cast(str, row["state_hash"]),
                generated_at=cast(datetime, row["updated_at"]),
                world_rules_version=cast(str, row["world_rules_version"]),
                state=state,
            ),
            stream_id,
        )

    async def _publish(
        self,
        connection: AsyncConnection[dict[str, object]],
        request: SkillInvocationRequest,
        context: OperationContext,
        skill: SkillSnapshot,
        world: WorldSnapshot,
        world_stream_id: str,
        sandbox_result: Success[SandboxRunResult] | Failure,
        run_id: str,
    ) -> SkillInvocationResult:
        now = await self._publication_time(connection, request, context)
        versions = replace(
            self._versions,
            skill_version=request.skill_ref.skill_version_id,
            artifact_sha256=request.skill_ref.artifact_sha256,
        )
        proposal: StagedWateringProposal | None = None
        sandbox_value: SandboxRunResult | None = None
        sandbox_failure: ContractError | None = None
        world_failure: ContractError | None = None
        world_commit: WorldCommitReceipt | None = None
        if isinstance(sandbox_result, Success):
            candidate = sandbox_result.value
            if candidate.run_id != run_id:
                sandbox_failure = _sandbox_protocol_error("RUN_ID_MISMATCH")
            elif (
                candidate.stdout_ref is not None
                or candidate.stderr_ref is not None
                or candidate.evidence_refs
            ):
                # DockerCppSandbox deliberately returns inline, bounded output and no
                # unattached references.  Reject instead of silently dropping future
                # adapter evidence that this atomic service cannot persist and reconcile.
                sandbox_failure = _sandbox_protocol_error("UNATTACHED_EVIDENCE")
            elif len(candidate.action_intents) > self._limits.max_intents:
                sandbox_failure = _sandbox_protocol_error("INTENT_LIMIT_EXCEEDED")
            else:
                sandbox_value = candidate
                try:
                    proposal = self._world_engine.stage(
                        world,
                        request.skill_ref,
                        sandbox_value.action_intents,
                    )
                except WorldRuleViolation as error:
                    world_failure = _world_rejected(error.reason)
        else:
            sandbox_failure = sandbox_result.error

        task_success = bool(proposal is not None and proposal.commit_eligible)
        if proposal is not None and not proposal.commit_eligible:
            world_failure = _world_rejected(proposal.failure_key or "TASK_INCOMPLETE")
        revision_after = world.revision
        world_reference: EvidenceRef | None = None
        if task_success:
            if proposal is None or sandbox_value is None:
                raise AssertionError("successful proposal requires Sandbox output")
            expected_world_commit = WorldCommitReceipt(
                world_id=world.world_id,
                previous_revision=world.revision,
                world_revision=proposal.revision_after,
                first_event_sequence=world.last_event_sequence + 1,
                last_event_sequence=world.last_event_sequence + 1,
                committed_at=now,
                state_hash=proposal.state_hash,
            )
            world_reference = EvidenceRef(
                evidence_id=_identifier("evidence_world", request.invocation_id),
                evidence_type=EvidenceType.WORLD_COMMIT,
                created_at=now,
                sha256=world_commit_receipt_sha256(expected_world_commit),
            )
            world_event = UncommittedEvent(
                event_type="world.committed",
                event_version=1,
                producer="world_engine",
                trace_id=context.trace_id,
                command_id=request.command_id,
                correlation_id=context.correlation_id,
                causation_id=request.command_id,
                content_ref=context.content_ref,
                payload=cast(
                    FrozenJsonObject,
                    {
                        "commit_id": world_commit_identifier(
                            context.actor.tenant_id,
                            world_stream_id,
                            run_id,
                            world.revision,
                        ),
                        "run_id": run_id,
                        "world_id": world.world_id,
                        "previous_world_revision": world.revision,
                        "world_revision": proposal.revision_after,
                        "state_hash": proposal.state_hash,
                        "applied_intent_ids": tuple(
                            intent.intent_id for intent in proposal.intents
                        ),
                        "committed_at": _iso(now),
                        "evidence_refs": (_evidence_ref_wire(world_reference),),
                    },
                ),
            )
            atomic_request = WorldAtomicCommit(
                stream_id=world_stream_id,
                expected_stream_sequence=world.last_event_sequence,
                command=WorldCommand(
                    run_id=run_id,
                    world_id=world.world_id,
                    expected_world_revision=world.revision,
                    world_rules_version=world.world_rules_version,
                    skill_ref=request.skill_ref,
                    intents=proposal.intents,
                ),
                events=(world_event,),
                outbox_messages=(),
            )
            atomic_result = await self._world_uow.participant.commit_on(
                connection,
                atomic_request,
                context,
            )
            if isinstance(atomic_result, Failure):
                self._raise_world_commit_failure(atomic_result)
            world_commit = atomic_result.value.world
            if (
                atomic_result.value.stream_id != world_stream_id
                or world_commit != expected_world_commit
            ):
                raise AgentToolExecutionError(
                    "TOOL_WORLD_STATE_INVALID",
                    "WorldUnitOfWork receipt differs from the staged proposal",
                )
            revision_after = world_commit.world_revision

        if sandbox_value is not None:
            sandbox_status = "SUCCEEDED"
            intent_count = len(sandbox_value.action_intents)
        else:
            reason = (
                "" if sandbox_failure is None else str(sandbox_failure.details.get("reason", ""))
            )
            sandbox_status = "TIMED_OUT" if reason == "WALL_TIMEOUT" else "FAILED"
            intent_count = 0
        world_status = (
            "COMMITTED"
            if task_success
            else ("REJECTED" if sandbox_value is not None else "NOT_ATTEMPTED")
        )
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
            created_at=now,
            sha256=canonical_json_sha256(run_payload),
        )
        evidence_refs: list[EvidenceRef] = [run_reference]
        evidence_documents = [
            self._evidence_document(
                context=context,
                reference=run_reference,
                source_type="SKILL_RUN",
                source_id=run_id,
                world_id=world.world_id,
                occurred_at=now,
                recorded_at=now,
                payload=run_payload,
                versions=versions,
            )
        ]
        if world_commit is not None:
            if world_reference is None:
                raise AssertionError("World commit requires its predeclared EvidenceRef")
            world_payload: dict[str, object] = {
                "evidence_kind": "WORLD_COMMIT",
                "world_id": world_commit.world_id,
                "previous_revision": world_commit.previous_revision,
                "world_revision": world_commit.world_revision,
                "first_event_sequence": world_commit.first_event_sequence,
                "last_event_sequence": world_commit.last_event_sequence,
                "state_hash": world_commit.state_hash,
            }
            evidence_refs.append(world_reference)
            evidence_documents.append(
                self._evidence_document(
                    context=context,
                    reference=world_reference,
                    source_type="WORLD",
                    source_id=world.world_id,
                    world_id=world.world_id,
                    occurred_at=world_commit.committed_at,
                    recorded_at=now,
                    payload=world_payload,
                    versions=versions,
                )
            )

        if proposal is not None:
            world_difference = proposal.world_difference
            failure_key = None if task_success else (proposal.failure_key or "watering_incomplete")
        else:
            world_difference = {"watered_plots": 0, "total_plots": 8, "intent_count": 0}
            failure_key = "sandbox_execution_failed"
        failed_actions: tuple[FrozenJsonObject, ...] = ()
        if not task_success:
            reason = failure_key or "watering_incomplete"
            failed_actions = (cast(FrozenJsonObject, {"reason": reason}),)
        run = RunResultSnapshot(
            run_id=run_id,
            session_id=request.session_id,
            turn_id=request.turn_id,
            command_id=request.command_id,
            world_id=request.world_id,
            skill_ref=request.skill_ref,
            task_success=task_success,
            world_revision_before=world.revision,
            world_revision_after=revision_after,
            world_difference=cast(FrozenJsonObject, world_difference),
            failed_actions=failed_actions,
            failure_key=failure_key,
            evidence_refs=tuple(evidence_refs),
            world_commit=world_commit,
            request_context=context,
        )
        receipt = SkillInvocationResult(
            invocation_id=request.invocation_id,
            tenant_id=request.tenant_id,
            request_sha256=request.request_sha256,
            arguments=request.arguments,
            run=run,
        )
        run_wire = self._run_wire(
            request=request,
            context=context,
            run=run,
            sandbox=sandbox_value,
            sandbox_failure=sandbox_failure,
            world_failure=world_failure,
            evidence_refs=tuple(evidence_refs),
            versions=versions,
            now=now,
        )
        self._validator.validate("schemas/game/run.schema.json", run_wire)
        for document in evidence_documents:
            self._validator.validate("schemas/game/evidence.schema.json", document)
        for reference, document in zip(evidence_refs, evidence_documents, strict=True):
            await connection.execute(
                """
                INSERT INTO yaya_evidence(
                    tenant_id,evidence_id,actor_id,content_hash,evidence_type,
                    payload_sha256,evidence_json,recorded_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    request.tenant_id,
                    reference.evidence_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    reference.evidence_type.value,
                    reference.sha256,
                    Jsonb(document),
                    now,
                ),
            )
        await connection.execute(
            """
            INSERT INTO yaya_runs(
                tenant_id,run_id,actor_id,content_hash,session_id,turn_id,
                command_id,world_id,skill_version_id,failure_key,task_success,
                snapshot_json,wire_json,created_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                request.tenant_id,
                run_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
                request.session_id,
                request.turn_id,
                request.command_id,
                request.world_id,
                request.skill_ref.skill_version_id,
                failure_key,
                task_success,
                Jsonb(encode(run)),
                Jsonb(run_wire),
                now,
            ),
        )
        await connection.execute(
            """
            INSERT INTO yaya_skill_invocations(
                tenant_id,invocation_id,actor_id,content_hash,run_id,
                request_sha256,result_json,committed_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                request.tenant_id,
                request.invocation_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
                run_id,
                request.request_sha256,
                Jsonb(encode(receipt)),
                now,
            ),
        )
        return receipt

    @staticmethod
    async def _publication_time(
        connection: AsyncConnection[dict[str, object]],
        request: SkillInvocationRequest,
        context: OperationContext,
    ) -> datetime:
        """Return one PostgreSQL-authoritative time for the atomic publication.

        Agent-turn acceptance is timestamped by PostgreSQL.  Clamping the live
        database clock to the durable Command and Job times prevents host/VM
        clock skew (and a backwards wall-clock adjustment) from publishing a
        Run before its accepted root event.
        """

        cursor = await connection.execute(
            """
            SELECT GREATEST(
                     clock_timestamp(),
                     c.updated_at,
                     COALESCE(j.created_at,c.updated_at)
                   ) AS value
            FROM yaya_commands c
            LEFT JOIN yaya_command_jobs j
              ON j.tenant_id=c.tenant_id AND j.command_id=c.command_id
            WHERE c.tenant_id=%s AND c.command_id=%s AND c.actor_id=%s
              AND c.content_hash=%s AND c.session_id=%s AND c.turn_id=%s
            """,
            (
                request.tenant_id,
                request.command_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
                request.session_id,
                request.turn_id,
            ),
        )
        rows = list(await cursor.fetchall())
        if len(rows) != 1 or not isinstance(rows[0].get("value"), datetime):
            raise AgentToolExecutionError(
                "TOOL_INVOCATION_IDENTITY_MISMATCH",
                "Run publication did not resolve one authoritative Command time",
            )
        return cast(datetime, rows[0]["value"]).astimezone(UTC)

    @staticmethod
    def _raise_world_commit_failure(failure: Failure) -> Never:
        error = failure.error
        message = error.message or "WorldUnitOfWork rejected the atomic commit"
        details = cast(Mapping[str, object], error.details)
        if error.code in {"WORLD_REVISION_CONFLICT", "EVENT_SEQUENCE_GAP"}:
            raise AgentToolExecutionError(
                "TOOL_WORLD_REVISION_CONFLICT",
                message,
                details,
            )
        if error.code in {"NOT_FOUND", "AUTHORIZATION_DENIED"}:
            raise AgentToolExecutionError(
                "TOOL_INVOCATION_IDENTITY_MISMATCH",
                message,
                details,
            )
        if error.code == "CONTENT_VERSION_MISMATCH":
            raise AgentToolExecutionError(
                "TOOL_WORLD_RULES_VERSION_MISMATCH",
                message,
                details,
            )
        raise AgentToolExecutionError(
            "TOOL_WORLD_STATE_INVALID",
            message,
            details,
        )

    def _evidence_document(
        self,
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
    ) -> dict[str, object]:
        payload_hash = canonical_json_sha256(payload)
        if reference.sha256 != payload_hash:
            raise AgentPersistenceError(
                "AGENT_EVIDENCE_HASH_MISMATCH",
                "EvidenceRef hash does not match canonical payload",
            )
        return {
            "request_context": _context_wire(context),
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

    def _run_wire(
        self,
        *,
        request: SkillInvocationRequest,
        context: OperationContext,
        run: RunResultSnapshot,
        sandbox: SandboxRunResult | None,
        sandbox_failure: ContractError | None,
        world_failure: ContractError | None,
        evidence_refs: Sequence[EvidenceRef],
        versions: VersionSet,
        now: datetime,
    ) -> dict[str, object]:
        if sandbox is None:
            if sandbox_failure is None:
                raise AssertionError("missing Sandbox result and failure")
            reason = str(sandbox_failure.details.get("reason", ""))
            sandbox_status = "TIMED_OUT" if reason == "WALL_TIMEOUT" else "FAILED"
            sandbox_wire: dict[str, object] = {
                "invocation_id": request.invocation_id,
                "status": sandbox_status,
                "started_at": None,
                "finished_at": _iso(now),
                "limits": self._limits_wire(),
                "usage": None,
                "action_intents": [],
                "failure": _error_wire(sandbox_failure),
            }
            run_status = "FAILED"
            world_wire: dict[str, object] = {
                "status": "NOT_ATTEMPTED",
                "receipt": None,
                "failure": None,
            }
        else:
            sandbox_wire = {
                "invocation_id": request.invocation_id,
                "status": "SUCCEEDED",
                "started_at": _iso(sandbox.started_at),
                "finished_at": _iso(sandbox.finished_at),
                "limits": self._limits_wire(),
                "usage": {
                    "cpu_ms": sandbox.usage.cpu_ms,
                    "wall_ms": sandbox.usage.wall_ms,
                    "peak_memory_bytes": sandbox.usage.peak_memory_bytes,
                },
                "action_intents": [
                    _intent_wire(cast(WaterIntent, intent)) for intent in sandbox.action_intents
                ],
                "failure": None,
            }
            if run.task_success:
                receipt = run.world_commit
                if receipt is None:
                    raise AssertionError("successful Run requires World receipt")
                run_status = "SUCCEEDED"
                world_wire = {
                    "status": "COMMITTED",
                    "receipt": {
                        "world_id": receipt.world_id,
                        "previous_revision": receipt.previous_revision,
                        "world_revision": receipt.world_revision,
                        "first_event_sequence": receipt.first_event_sequence,
                        "last_event_sequence": receipt.last_event_sequence,
                        "state_hash": receipt.state_hash,
                        "committed_at": _iso(receipt.committed_at),
                    },
                    "failure": None,
                }
            else:
                run_status = "REJECTED"
                failure = world_failure or _world_rejected("TASK_INCOMPLETE")
                world_wire = {
                    "status": "REJECTED",
                    "receipt": None,
                    "failure": _error_wire(failure),
                }
        return {
            "request_context": _context_wire(context),
            "run_id": run.run_id,
            "session_id": run.session_id,
            "turn_id": run.turn_id,
            "command_id": run.command_id,
            "status": run_status,
            "terminal": True,
            "skill": {
                "skill_id": run.skill_ref.skill_id,
                "skill_version_id": run.skill_ref.skill_version_id,
                "artifact_sha256": run.skill_ref.artifact_sha256,
                "certification_id": run.skill_ref.certification_id,
            },
            "sandbox": sandbox_wire,
            "world_application": world_wire,
            "agent_feedback": None,
            "created_at": _iso(now),
            "updated_at": _iso(now),
            "evidence_refs": [_evidence_ref_wire(item) for item in evidence_refs],
            "versions": _versions_wire(versions),
        }

    def _limits_wire(self) -> dict[str, object]:
        return {
            "cpu_ms": self._limits.cpu_ms,
            "wall_ms": self._limits.wall_ms,
            "memory_bytes": self._limits.memory_bytes,
            "max_intents": self._limits.max_intents,
        }


__all__ = ["PostgresSkillInvocationService"]
