"""Public Session/Build/Activation production chain and durable control worker.

The frozen Game API deliberately exposes asynchronous commands while the
existing Agent-turn job is turn-shaped.  This module owns the compatible,
turn-free control job used by Session creation, Skill Build and Activation.
Product SkillDraft remains in :mod:`yaya_agent_backend.skill_drafts` and Build
never reads it: the submitted source bundle is the complete build authority.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, LiteralString, cast

import psycopg
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from yaya_agent_build import SourceBundleValidationError, canonical_source_bundle_sha256
from yaya_agent_contracts import (
    ActiveSkill,
    ActorRef,
    CertifiedSkill,
    CommandRecord,
    CommandStatus,
    ContentRef,
    ContractError,
    ErrorCategory,
    EvidenceRef,
    FrozenJsonObject,
    NewCommand,
    OperationContext,
    RequestContext,
    VersionSet,
    canonical_json_sha256,
)
from yaya_agent_runtime import CompileResultSnapshot, SessionSnapshot, SkillSnapshot

from .application import BackendApplicationError, HttpAttempt, ResourceResult
from .codec import decode_as, encode, plain
from .database import PostgresCommitStateUnknown, PostgresDatabase
from .wire import ContractSchemaValidator

_MAX_BODY_BYTES = 8 * 1024 * 1024
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_RESOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_JOB_TYPES = frozenset({"CREATE_AGENT_SESSION", "CREATE_SKILL_BUILD", "ACTIVATE_SKILL_VERSION"})
_BUILD_PHASES = (
    "VALIDATE_SOURCE",
    "COMPILE",
    "PUBLIC_TEST",
    "HIDDEN_TEST",
    "CERTIFY",
)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _utc_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} must be an RFC 3339 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include an offset")
    return parsed.astimezone(UTC)


def _identifier(prefix: str, *parts: str) -> str:
    framed = "".join(f"{len(part)}:{part}" for part in parts)
    digest = hashlib.sha256(framed.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    source = cast(Mapping[object, object], value)
    if any(not isinstance(key, str) for key in source):
        raise ValueError(f"{label} has a non-string key")
    return {cast(str, key): item for key, item in source.items()}


def _sequence(value: object, label: str) -> list[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError(f"{label} must be an array")
    return list(cast(Sequence[object], value))


def _strict_object(raw: bytes) -> dict[str, object]:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError(f"duplicate JSON key {key}")
            result[key] = value
        return result

    def constant(value: str) -> object:
        raise ValueError(f"non-finite JSON number {value}")

    decoded = json.loads(
        raw.decode("utf-8", errors="strict"),
        object_pairs_hook=pairs,
        parse_constant=constant,
    )
    return _mapping(decoded, "request JSON")


def _version_wire(versions: VersionSet) -> dict[str, object]:
    value = _mapping(plain(versions), "VersionSet")
    return {key: item for key, item in value.items() if item is not None}


def _actor_wire(actor: ActorRef) -> dict[str, object]:
    return {
        "tenant_id": actor.tenant_id,
        "actor_id": actor.actor_id,
        "actor_type": actor.actor_type.value,
        "roles": list(actor.roles),
    }


def _content_wire(content: ContentRef) -> dict[str, object]:
    return {
        "unit_id": content.unit_id,
        "version": content.version,
        "content_hash": content.content_hash,
    }


def _context_wire(context: RequestContext | OperationContext) -> dict[str, object]:
    return {
        "schema_version": context.schema_version,
        "request_id": context.request_id,
        "correlation_id": context.correlation_id,
        "trace_id": context.trace_id,
        "requested_at": _iso(context.requested_at),
        "actor": _actor_wire(context.actor),
        "content_ref": _content_wire(context.content_ref),
    }


def _command_wire(record: CommandRecord) -> dict[str, object]:
    value = _mapping(plain(record), "Command")
    value["versions"] = _version_wire(record.versions)
    value["evidence_refs"] = [_evidence_ref_wire(item) for item in record.evidence_refs]
    return value


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


def _error(
    code: str,
    stage: str,
    message: str,
    details: Mapping[str, object] | None = None,
    *,
    command_id: str | None = None,
) -> BackendApplicationError:
    statuses = {
        "INVALID_REQUEST": 400,
        "CONTENT_VERSION_MISMATCH": 409,
        "NOT_FOUND": 404,
        "PAYLOAD_TOO_LARGE": 413,
        "IDEMPOTENCY_KEY_REUSED": 409,
        "WORLD_REVISION_CONFLICT": 409,
        "SKILL_NOT_CERTIFIED": 422,
        "SKILL_VERSION_MISMATCH": 409,
        "DEPENDENCY_UNAVAILABLE": 503,
        "UNKNOWN_COMMIT_STATE": 503,
        "INVARIANT_VIOLATION": 500,
        "INTERNAL_ERROR": 500,
    }
    return BackendApplicationError(
        code,
        statuses[code],
        stage,
        message,
        details,
        command_id=command_id,
    )


@dataclass(frozen=True, slots=True)
class AcceptedControlJob:
    receipt: Mapping[str, object]
    command: CommandRecord
    operation_context: OperationContext
    replayed: bool


@dataclass(frozen=True, slots=True)
class _LaunchAuthority:
    authority_id: str
    learner_id: str
    agent_profile_id: str
    world_id: str
    task_id: str
    content: ContentRef
    versions: VersionSet


@dataclass(frozen=True, slots=True)
class _ValidatedCertification:
    certified: CertifiedSkill
    skill: SkillSnapshot
    compile_result: CompileResultSnapshot
    record: Mapping[str, object]


class StudentSkillChainApplication:
    """Accept and query the frozen public student skill-chain resources."""

    def __init__(
        self,
        database: PostgresDatabase,
        validator: ContractSchemaValidator,
        versions: VersionSet,
        *,
        stream_url: str = "wss://localhost/v1/realtime",
        artifact_root: Path | None = None,
    ) -> None:
        if not stream_url.startswith("wss://") or "@" in stream_url:
            raise ValueError("bootstrap stream URL must be uncredentialized WSS")
        self._database = database
        self._validator = validator
        self._versions = versions
        self._stream_url = stream_url
        self._artifact_root: Path | None = None
        if artifact_root is not None:
            root = artifact_root.expanduser().resolve()
            if not root.is_dir() or artifact_root.is_symlink():
                raise ValueError("artifact_root must be an existing non-symlink directory")
            self._artifact_root = root

    async def get_bootstrap(self, actor: ActorRef, attempt: HttpAttempt) -> ResourceResult:
        connection: AsyncConnection[dict[str, object]] | None = None
        try:
            connection = await self._database.connect(autocommit=True)
            authority = await self._resolve_only_launch_authority(connection, actor)
            cursor = await connection.execute(
                """
                SELECT actor_id,content_hash,stream_id,revision,last_event_sequence,
                       state_hash,world_rules_version,request_context_json
                FROM yaya_worlds
                WHERE tenant_id=%s AND world_id=%s AND actor_id=%s AND content_hash=%s
                """,
                (
                    actor.tenant_id,
                    authority.world_id,
                    actor.actor_id,
                    authority.content.content_hash,
                ),
            )
            world = await cursor.fetchone()
            if world is None:
                raise _error("NOT_FOUND", "VALIDATE", "Bootstrap World was not found")
            if (
                world["stream_id"] != f"world:{authority.world_id}"
                or world["world_rules_version"] != authority.versions.world_rules_version
            ):
                raise _error(
                    "INVARIANT_VIOLATION",
                    "VALIDATE",
                    "Bootstrap World authority drifted",
                )
            context = RequestContext(
                request_id=attempt.request_id,
                correlation_id=attempt.correlation_id,
                trace_id=attempt.trace_id,
                requested_at=attempt.requested_at,
                actor=actor,
                content_ref=authority.content,
                schema_version=attempt.schema_version,
            )
            payload: dict[str, object] = {
                "request_context": _context_wire(context),
                "api_version": self._versions.api_version,
                "server_time": _iso(datetime.now(UTC)),
                "actor": _actor_wire(actor),
                "content": _content_wire(authority.content),
                "capabilities": {
                    "skill_builds": True,
                    "agent_sessions": True,
                    "world_event_stream": False,
                    "client_event_batch": False,
                    "evidence_query": True,
                },
                "limits": {
                    "max_source_files": 32,
                    "max_source_bytes": 1_048_576,
                    "max_client_events_per_batch": 500,
                    "max_agent_turn_chars": 4000,
                },
                "world": {
                    "world_id": authority.world_id,
                    "revision": world["revision"],
                    "stream_id": world["stream_id"],
                    "last_event_sequence": world["last_event_sequence"],
                    "stream_protocol_version": "1.0.0",
                    "snapshot_url": f"/v1/worlds/{authority.world_id}/snapshot",
                    "events_url": f"/v1/worlds/{authority.world_id}/events",
                    "stream_url": self._stream_url,
                },
            }
            self._validator.validate("schemas/game/bootstrap-response.schema.json", payload)
            return ResourceResult(payload, {})
        except BackendApplicationError:
            raise
        except psycopg.Error as error:
            raise _error(
                "DEPENDENCY_UNAVAILABLE", "VALIDATE", "PostgreSQL bootstrap read failed"
            ) from error
        finally:
            if connection is not None:
                await connection.close()

    async def accept_session(
        self,
        *,
        actor: ActorRef,
        attempt: HttpAttempt,
        idempotency_key: str,
        raw_body: bytes,
        body: Mapping[str, object],
    ) -> AcceptedControlJob:
        return await self._accept(
            actor=actor,
            attempt=attempt,
            operation="CREATE_AGENT_SESSION",
            schema="schemas/game/agent-session-create-request.schema.json",
            subject_id=None,
            request_target="/v1/agent-sessions",
            idempotency_key=idempotency_key,
            raw_body=raw_body,
            body=body,
        )

    async def accept_build(
        self,
        *,
        actor: ActorRef,
        attempt: HttpAttempt,
        idempotency_key: str,
        raw_body: bytes,
        body: Mapping[str, object],
    ) -> AcceptedControlJob:
        return await self._accept(
            actor=actor,
            attempt=attempt,
            operation="CREATE_SKILL_BUILD",
            schema="schemas/game/skill-build-create-request.schema.json",
            subject_id=None,
            request_target="/v1/skill-builds",
            idempotency_key=idempotency_key,
            raw_body=raw_body,
            body=body,
        )

    async def accept_activation(
        self,
        *,
        actor: ActorRef,
        attempt: HttpAttempt,
        skill_version_id: str,
        idempotency_key: str,
        raw_body: bytes,
        body: Mapping[str, object],
    ) -> AcceptedControlJob:
        if _RESOURCE_ID.fullmatch(skill_version_id) is None:
            raise _error("INVALID_REQUEST", "ACCEPT", "skill_version_id is invalid")
        return await self._accept(
            actor=actor,
            attempt=attempt,
            operation="ACTIVATE_SKILL_VERSION",
            schema="schemas/game/skill-activation-request.schema.json",
            subject_id=skill_version_id,
            request_target=f"/v1/skill-versions/{skill_version_id}/activations",
            idempotency_key=idempotency_key,
            raw_body=raw_body,
            body=body,
        )

    async def _accept(
        self,
        *,
        actor: ActorRef,
        attempt: HttpAttempt,
        operation: Literal["CREATE_AGENT_SESSION", "CREATE_SKILL_BUILD", "ACTIVATE_SKILL_VERSION"],
        schema: str,
        subject_id: str | None,
        request_target: str,
        idempotency_key: str,
        raw_body: bytes,
        body: Mapping[str, object],
    ) -> AcceptedControlJob:
        if not 2 <= len(raw_body) <= _MAX_BODY_BYTES:
            code = "PAYLOAD_TOO_LARGE" if len(raw_body) > _MAX_BODY_BYTES else "INVALID_REQUEST"
            raise _error(code, "ACCEPT", "Request body size is invalid")
        if _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
            raise _error("INVALID_REQUEST", "ACCEPT", "Idempotency-Key is invalid")
        try:
            parsed = _strict_object(raw_body)
            supplied = _mapping(body, "request body")
            if parsed != supplied:
                raise ValueError("parsed body differs from request bytes")
            self._validator.validate(schema, supplied)
        except (UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise _error("INVALID_REQUEST", "ACCEPT", "Request body is invalid") from error
        request_sha256 = hashlib.sha256(raw_body).hexdigest()
        command_id = _identifier("cmd", actor.tenant_id, actor.actor_id, operation, idempotency_key)
        replay = await self._lookup_acceptance(
            actor,
            operation,
            idempotency_key,
            request_target,
            request_sha256,
            raw_body,
        )
        if replay is not None:
            return replay

        try:
            async with self._database.transaction_with_commit_boundary() as connection:
                replay = await self._lookup_acceptance_on(
                    connection,
                    actor,
                    operation,
                    idempotency_key,
                    request_target,
                    request_sha256,
                    raw_body,
                )
                if replay is not None:
                    return replay
                authority, resolved_subject = await self._acceptance_authority(
                    connection,
                    actor,
                    operation,
                    supplied,
                    subject_id,
                )
                context = OperationContext(
                    request_id=attempt.request_id,
                    correlation_id=attempt.correlation_id,
                    trace_id=attempt.trace_id,
                    requested_at=attempt.requested_at,
                    actor=actor,
                    content_ref=authority.content,
                    schema_version=attempt.schema_version,
                    command_id=command_id,
                    causation_id=None,
                )
                accepted_at = datetime.now(UTC)
                command = NewCommand(
                    command_type=operation,
                    idempotency_key=idempotency_key,
                    request_sha256=request_sha256,
                    versions=self._versions,
                ).initial_record(context, accepted_at)
                self._validator.validate("schemas/game/command.schema.json", _command_wire(command))
                job_id = _identifier("job", command_id)
                if operation == "CREATE_AGENT_SESSION":
                    resource_id = _identifier("session", command_id)
                    subject = resource_id
                elif operation == "CREATE_SKILL_BUILD":
                    resource_id = _identifier("build", command_id)
                    subject = resource_id
                else:
                    resource_id = _identifier("activation", command_id)
                    subject = resolved_subject
                receipt: dict[str, object] = {
                    "job_id": job_id,
                    "job_type": operation,
                    "status": "ACCEPTED",
                    "created_at": _iso(accepted_at),
                    "updated_at": _iso(accepted_at),
                    "command_id": command_id,
                    "trace_id": attempt.trace_id,
                    "error": None,
                }
                self._validator.validate("schemas/game/accepted-game-job.schema.json", receipt)
                await connection.execute(
                    """
                    INSERT INTO yaya_commands(
                        tenant_id,actor_id,operation,idempotency_key,command_id,
                        session_id,turn_id,client_turn_sequence,request_sha256,
                        content_hash,revision,status,updated_at,record_json
                    ) VALUES (%s,%s,%s,%s,%s,NULL,NULL,NULL,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        actor.tenant_id,
                        actor.actor_id,
                        operation,
                        idempotency_key,
                        command_id,
                        request_sha256,
                        authority.content.content_hash,
                        command.revision,
                        command.status.value,
                        command.updated_at,
                        Jsonb(encode(command)),
                    ),
                )
                await connection.execute(
                    """
                    INSERT INTO yaya_control_jobs(
                        tenant_id,command_id,job_id,authority_id,actor_id,content_hash,
                        operation,idempotency_key,subject_id,resource_id,request_target,
                        request_body,request_sha256,request_json,operation_context_json,
                        accepted_receipt_json,created_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        actor.tenant_id,
                        command_id,
                        job_id,
                        authority.authority_id,
                        actor.actor_id,
                        authority.content.content_hash,
                        operation,
                        idempotency_key,
                        subject,
                        resource_id,
                        request_target,
                        raw_body,
                        request_sha256,
                        Jsonb(supplied),
                        Jsonb(encode(context)),
                        Jsonb(receipt),
                        accepted_at,
                    ),
                )
                if operation == "CREATE_SKILL_BUILD":
                    await self._insert_accepted_build(
                        connection,
                        context,
                        authority,
                        command_id,
                        resource_id,
                        supplied,
                        accepted_at,
                    )
                return AcceptedControlJob(receipt, command, context, False)
        except BackendApplicationError:
            raise
        except PostgresCommitStateUnknown as error:
            replay = await self._lookup_acceptance(
                actor,
                operation,
                idempotency_key,
                request_target,
                request_sha256,
                raw_body,
            )
            if replay is not None:
                return replace(replay, replayed=False)
            raise _error(
                "UNKNOWN_COMMIT_STATE",
                "WORLD_COMMIT",
                "Command acceptance requires reconciliation",
                command_id=command_id,
            ) from error
        except psycopg.errors.UniqueViolation as error:
            replay = await self._lookup_acceptance(
                actor,
                operation,
                idempotency_key,
                request_target,
                request_sha256,
                raw_body,
            )
            if replay is not None:
                return replay
            raise _error(
                "INVARIANT_VIOLATION",
                "ACCEPT",
                "A durable command identity collided",
                command_id=command_id,
            ) from error
        except psycopg.Error as error:
            raise _error(
                "DEPENDENCY_UNAVAILABLE",
                "ACCEPT",
                "PostgreSQL command acceptance failed",
                command_id=command_id,
            ) from error

    async def _acceptance_authority(
        self,
        connection: AsyncConnection[dict[str, object]],
        actor: ActorRef,
        operation: str,
        body: Mapping[str, object],
        subject_id: str | None,
    ) -> tuple[_LaunchAuthority, str]:
        if operation == "CREATE_AGENT_SESSION":
            content = _mapping(body.get("content"), "Session content")
            authority = await self.resolve_launch_authority(
                connection,
                actor,
                content=ContentRef(
                    unit_id=cast(str, content["unit_id"]),
                    version=cast(str, content["version"]),
                    content_hash=cast(str, content["content_hash"]),
                ),
                world_id=cast(str, body["world_id"]),
                learner_id=cast(str, body["learner_id"]),
                agent_profile_id=cast(str, body["agent_profile_id"]),
            )
            return authority, ""
        if operation == "CREATE_SKILL_BUILD":
            authority = await self._resolve_only_launch_authority(connection, actor)
            return authority, ""
        if subject_id is None:
            raise _error("INVALID_REQUEST", "ACCEPT", "Activation subject is missing")
        cursor = await connection.execute(
            """
            SELECT content_hash,skill_id,record_json FROM yaya_skill_certifications
            WHERE tenant_id=%s AND actor_id=%s AND skill_version_id=%s
            """,
            (actor.tenant_id, actor.actor_id, subject_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise _error("SKILL_NOT_CERTIFIED", "REGISTRY", "SkillVersion is not certified")
        record = _mapping(row["record_json"], "Certification")
        context = _mapping(record.get("request_context"), "Certification request_context")
        content = _mapping(context.get("content_ref"), "Certification content_ref")
        if content.get("content_hash") != row["content_hash"]:
            raise _error(
                "INVARIANT_VIOLATION", "REGISTRY", "Certification content authority drifted"
            )
        certified_content = ContentRef(
            unit_id=cast(str, content["unit_id"]),
            version=cast(str, content["version"]),
            content_hash=cast(str, content["content_hash"]),
        )
        scope = _mapping(body.get("activation_scope"), "Activation scope")
        authority = await self.resolve_launch_authority(
            connection,
            actor,
            content=certified_content,
            world_id=cast(str, scope["world_id"]),
            agent_profile_id=cast(str, scope["agent_profile_id"]),
        )
        head_cursor = await connection.execute(
            """
            SELECT revision FROM yaya_registry_heads
            WHERE tenant_id=%s AND actor_id=%s AND content_hash=%s
              AND world_id=%s AND agent_profile_id=%s AND skill_id=%s
            """,
            (
                actor.tenant_id,
                actor.actor_id,
                certified_content.content_hash,
                authority.world_id,
                authority.agent_profile_id,
                row["skill_id"],
            ),
        )
        head = await head_cursor.fetchone()
        current_revision = 0 if head is None else head["revision"]
        if current_revision != body.get("expected_registry_revision"):
            raise _error(
                "CONTENT_VERSION_MISMATCH",
                "REGISTRY",
                "Registry expected revision is stale",
            )
        return authority, subject_id

    async def _lookup_acceptance(
        self,
        actor: ActorRef,
        operation: str,
        idempotency_key: str,
        request_target: str,
        request_sha256: str,
        raw_body: bytes,
    ) -> AcceptedControlJob | None:
        connection: AsyncConnection[dict[str, object]] | None = None
        try:
            connection = await self._database.connect(autocommit=True)
            return await self._lookup_acceptance_on(
                connection,
                actor,
                operation,
                idempotency_key,
                request_target,
                request_sha256,
                raw_body,
            )
        except BackendApplicationError:
            raise
        except psycopg.Error as error:
            raise _error(
                "DEPENDENCY_UNAVAILABLE", "ACCEPT", "Idempotency receipt lookup failed"
            ) from error
        finally:
            if connection is not None:
                await connection.close()

    async def _lookup_acceptance_on(
        self,
        connection: AsyncConnection[dict[str, object]],
        actor: ActorRef,
        operation: str,
        idempotency_key: str,
        request_target: str,
        request_sha256: str,
        raw_body: bytes,
    ) -> AcceptedControlJob | None:
        cursor = await connection.execute(
            """
            SELECT c.command_id,c.operation,c.request_sha256,c.content_hash,c.record_json,
                   j.actor_id,j.content_hash AS job_content_hash,j.operation AS job_type,
                   j.idempotency_key AS job_idempotency_key,j.request_target,
                   j.request_body,j.request_sha256 AS job_request_sha256,
                   j.operation_context_json,j.accepted_receipt_json
            FROM yaya_commands c
            JOIN yaya_control_jobs j
              ON j.tenant_id=c.tenant_id AND j.command_id=c.command_id
            WHERE c.tenant_id=%s AND c.actor_id=%s
              AND c.operation=%s AND c.idempotency_key=%s
            """,
            (actor.tenant_id, actor.actor_id, operation, idempotency_key),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        if (
            row["request_sha256"] != request_sha256
            or row["job_request_sha256"] != request_sha256
            or row["request_target"] != request_target
            or row["request_body"] != raw_body
        ):
            raise _error("IDEMPOTENCY_KEY_REUSED", "ACCEPT", "Idempotency key has different bytes")
        if hashlib.sha256(cast(bytes, row["request_body"])).hexdigest() != request_sha256:
            raise _error("INVARIANT_VIOLATION", "ACCEPT", "Persisted idempotency bytes drifted")
        command = decode_as(row["record_json"], CommandRecord)
        context = decode_as(row["operation_context_json"], OperationContext)
        receipt = _mapping(row["accepted_receipt_json"], "accepted receipt")
        if (
            command.command_id != row["command_id"]
            or command.command_type != operation
            or command.request_context.actor.tenant_id != actor.tenant_id
            or command.request_context.actor.actor_id != actor.actor_id
            or context.command_id != command.command_id
            or context.content_ref.content_hash != row["content_hash"]
            or row["job_content_hash"] != row["content_hash"]
            or row["actor_id"] != actor.actor_id
            or row["job_type"] != operation
            or row["job_idempotency_key"] != idempotency_key
            or receipt.get("command_id") != command.command_id
            or receipt.get("trace_id") != context.trace_id
        ):
            raise _error("INVARIANT_VIOLATION", "ACCEPT", "Persisted acceptance identity drifted")
        self._validator.validate("schemas/game/accepted-game-job.schema.json", receipt)
        return AcceptedControlJob(receipt, command, context, True)

    async def _resolve_only_launch_authority(
        self,
        connection: AsyncConnection[dict[str, object]],
        actor: ActorRef,
    ) -> _LaunchAuthority:
        return await self.resolve_launch_authority(connection, actor)

    async def resolve_launch_authority(
        self,
        connection: AsyncConnection[dict[str, object]],
        actor: ActorRef,
        *,
        content: ContentRef | None = None,
        world_id: str | None = None,
        learner_id: str | None = None,
        agent_profile_id: str | None = None,
    ) -> _LaunchAuthority:
        cursor = await connection.execute(
            """
            SELECT authority_id,learner_id,agent_profile_id,world_id,task_id,
                   content_unit_id,content_version,content_hash,versions_json,snapshot_sha256
            FROM yaya_launch_authorities
            WHERE tenant_id=%s AND actor_id=%s AND active=TRUE
            ORDER BY authority_id
            """,
            (actor.tenant_id, actor.actor_id),
        )
        rows = await cursor.fetchall()
        if content is not None:
            rows = [
                row
                for row in rows
                if (
                    row["content_unit_id"],
                    row["content_version"],
                    row["content_hash"],
                )
                == (content.unit_id, content.version, content.content_hash)
            ]
        if world_id is not None:
            rows = [row for row in rows if row["world_id"] == world_id]
        if learner_id is not None:
            rows = [row for row in rows if row["learner_id"] == learner_id]
        if agent_profile_id is not None:
            rows = [row for row in rows if row["agent_profile_id"] == agent_profile_id]
        if len(rows) != 1:
            code = "NOT_FOUND" if not rows else "INVARIANT_VIOLATION"
            raise _error(code, "VALIDATE", "Launch authority must resolve exactly once")
        row = rows[0]
        projection = {
            "authority_id": row["authority_id"],
            "learner_id": row["learner_id"],
            "agent_profile_id": row["agent_profile_id"],
            "world_id": row["world_id"],
            "task_id": row["task_id"],
            "content_unit_id": row["content_unit_id"],
            "content_version": row["content_version"],
            "content_hash": row["content_hash"],
            "versions": row["versions_json"],
        }
        if canonical_json_sha256(projection) != row["snapshot_sha256"]:
            raise _error("INVARIANT_VIOLATION", "VALIDATE", "Launch authority hash drifted")
        content = ContentRef(
            unit_id=cast(str, row["content_unit_id"]),
            version=cast(str, row["content_version"]),
            content_hash=cast(str, row["content_hash"]),
        )
        versions = decode_as(row["versions_json"], VersionSet)
        if versions != self._versions:
            raise _error(
                "INVARIANT_VIOLATION",
                "VALIDATE",
                "Launch authority versions differ from the production composition",
            )
        return _LaunchAuthority(
            authority_id=cast(str, row["authority_id"]),
            learner_id=cast(str, row["learner_id"]),
            agent_profile_id=cast(str, row["agent_profile_id"]),
            world_id=cast(str, row["world_id"]),
            task_id=cast(str, row["task_id"]),
            content=content,
            versions=versions,
        )

    async def _insert_accepted_build(
        self,
        connection: AsyncConnection[dict[str, object]],
        context: OperationContext,
        authority: _LaunchAuthority,
        command_id: str,
        build_id: str,
        body: Mapping[str, object],
        accepted_at: datetime,
    ) -> None:
        source_bundle = _mapping(body["source_bundle"], "Build source_bundle")
        try:
            # Frozen Game semantics deliberately hash the ordered
            # ``[path, content_sha256]`` projection, not the whole JSON object.
            source_sha256 = canonical_source_bundle_sha256(source_bundle)
        except SourceBundleValidationError as error:
            raise _error("INVALID_REQUEST", "VALIDATE", str(error)) from error
        build_policy_id = await self._resolve_build_policy(
            connection,
            context,
            compiler_profile=cast(str, body["compiler_profile"]),
            test_suite_version=cast(str, body["test_suite_version"]),
            requested_capabilities=cast(list[object], body.get("requested_capabilities", [])),
        )
        phases: list[dict[str, object]] = [
            {
                "name": name,
                "status": "PENDING",
                "started_at": None,
                "finished_at": None,
                "diagnostic_codes": [],
            }
            for name in ("VALIDATE_SOURCE", "COMPILE", "PUBLIC_TEST", "HIDDEN_TEST", "CERTIFY")
        ]
        resource: dict[str, object] = {
            "request_context": _context_wire(context),
            "build_id": build_id,
            "skill_id": body["skill_id"],
            "skill_version_id": None,
            "status": "ACCEPTED",
            "terminal": False,
            "created_at": _iso(accepted_at),
            "updated_at": _iso(accepted_at),
            "artifact": None,
            "certification": None,
            "phases": phases,
            "failure": None,
            "evidence_refs": [],
            "versions": _version_wire(self._versions),
        }
        self._validator.validate("schemas/game/skill-build.schema.json", resource)
        resource_sha256 = canonical_json_sha256(resource)
        capabilities = body.get("requested_capabilities", [])
        await connection.execute(
            """
            INSERT INTO yaya_skill_builds(
                tenant_id,build_id,authority_id,skill_id,actor_id,content_hash,
                client_draft_revision,
                source_bundle_json,source_bundle_sha256,compiler_profile,test_suite_version,
                requested_capabilities_json,build_policy_id,command_id,status,terminal,
                resource_json,resource_sha256,
                created_at,updated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'ACCEPTED',FALSE,%s,%s,%s,%s)
            """,
            (
                context.actor.tenant_id,
                build_id,
                authority.authority_id,
                body["skill_id"],
                context.actor.actor_id,
                context.content_ref.content_hash,
                body["client_draft_revision"],
                Jsonb(source_bundle),
                source_sha256,
                body["compiler_profile"],
                body["test_suite_version"],
                Jsonb(capabilities),
                build_policy_id,
                command_id,
                Jsonb(resource),
                resource_sha256,
                accepted_at,
                accepted_at,
            ),
        )
        await connection.execute(
            """
            INSERT INTO yaya_skill_build_history(
                tenant_id,build_id,sequence,status,record_json,record_sha256,recorded_at
            ) VALUES (%s,%s,1,'ACCEPTED',%s,%s,%s)
            """,
            (
                context.actor.tenant_id,
                build_id,
                Jsonb(resource),
                resource_sha256,
                accepted_at,
            ),
        )

    async def _resolve_build_policy(
        self,
        connection: AsyncConnection[dict[str, object]],
        context: OperationContext,
        *,
        compiler_profile: str,
        test_suite_version: str,
        requested_capabilities: list[object],
    ) -> str:
        cursor = await connection.execute(
            """
            SELECT build_policy_id,compiler_image,compiler_version,compile_flags_json,
                   public_tests_json,hidden_tests_json,approved_capabilities_json,
                   limits_json,parameter_schema_json,semantic_version_major,
                   semantic_version_minor,runtime_abi_version,policy_sha256
            FROM yaya_build_policies
            WHERE tenant_id=%s AND actor_id=%s AND content_hash=%s
              AND compiler_profile=%s AND test_suite_version=%s AND active=TRUE
            ORDER BY build_policy_id
            """,
            (
                context.actor.tenant_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
                compiler_profile,
                test_suite_version,
            ),
        )
        rows = await cursor.fetchall()
        if len(rows) != 1:
            code = "NOT_FOUND" if not rows else "INVARIANT_VIOLATION"
            raise _error(code, "POLICY", "Build policy must resolve exactly once")
        row = rows[0]
        raw_approved = row["approved_capabilities_json"]
        if not isinstance(raw_approved, list):
            raise _error("INVARIANT_VIOLATION", "POLICY", "Build policy capabilities drifted")
        approved_items = cast(list[object], raw_approved)
        if any(not isinstance(item, str) for item in approved_items):
            raise _error("INVARIANT_VIOLATION", "POLICY", "Build policy capabilities drifted")
        approved = [cast(str, item) for item in approved_items]
        if any(item not in approved for item in requested_capabilities):
            raise _error("INVALID_REQUEST", "POLICY", "Requested capability is not approved")
        projection = {
            "build_policy_id": row["build_policy_id"],
            "actor_id": context.actor.actor_id,
            "content_hash": context.content_ref.content_hash,
            "compiler_profile": compiler_profile,
            "test_suite_version": test_suite_version,
            "compiler_image": row["compiler_image"],
            "compiler_version": row["compiler_version"],
            "compile_flags": row["compile_flags_json"],
            "public_tests": row["public_tests_json"],
            "hidden_tests": row["hidden_tests_json"],
            "approved_capabilities": approved,
            "limits": row["limits_json"],
            "parameter_schema": row["parameter_schema_json"],
            "semantic_version_major": row["semantic_version_major"],
            "semantic_version_minor": row["semantic_version_minor"],
            "runtime_abi_version": row["runtime_abi_version"],
        }
        if canonical_json_sha256(projection) != row["policy_sha256"]:
            raise _error("INVARIANT_VIOLATION", "POLICY", "Build policy hash drifted")
        return cast(str, row["build_policy_id"])

    async def get_session(self, session_id: str, actor: ActorRef) -> ResourceResult:
        if _RESOURCE_ID.fullmatch(session_id) is None:
            raise _error("INVALID_REQUEST", "VALIDATE", "session_id is invalid")
        row = await self._read_one(
            """
            SELECT p.actor_id,p.content_hash,p.resource_json,p.resource_sha256,
                   s.client_turn_sequence
            FROM yaya_public_agent_sessions p
            JOIN yaya_agent_sessions s
              ON s.tenant_id=p.tenant_id AND s.session_id=p.session_id
             AND s.actor_id=p.actor_id AND s.content_hash=p.content_hash
            WHERE p.tenant_id=%s AND p.session_id=%s AND p.actor_id=%s
            """,
            (actor.tenant_id, session_id, actor.actor_id),
        )
        stored = _mapping(row["resource_json"], "Session resource")
        if canonical_json_sha256(stored) != row["resource_sha256"]:
            raise _error("INVARIANT_VIOLATION", "VALIDATE", "Session resource hash drifted")
        payload = dict(stored)
        payload["last_turn_sequence"] = row["client_turn_sequence"]
        context = _mapping(payload.get("request_context"), "Session request_context")
        origin_actor = _mapping(context.get("actor"), "Session actor")
        content = _mapping(payload.get("content"), "Session content")
        if (
            payload.get("session_id") != session_id
            or origin_actor.get("tenant_id") != actor.tenant_id
            or origin_actor.get("actor_id") != actor.actor_id
            or content.get("content_hash") != row["content_hash"]
        ):
            raise _error("INVARIANT_VIOLATION", "VALIDATE", "Session identity drifted")
        self._validator.validate("schemas/game/agent-session.schema.json", payload)
        return ResourceResult(payload, {})

    async def get_build(self, build_id: str, actor: ActorRef) -> ResourceResult:
        if _RESOURCE_ID.fullmatch(build_id) is None:
            raise _error("INVALID_REQUEST", "VALIDATE", "build_id is invalid")
        row = await self._read_one(
            """
            SELECT actor_id,content_hash,skill_id,status,source_bundle_json,
                   source_bundle_sha256,resource_json,resource_sha256,
                   COALESCE((
                       SELECT jsonb_agg(jsonb_build_object(
                           'sequence',h.sequence,'status',h.status,
                           'record_sha256',h.record_sha256,'record_json',h.record_json
                       ) ORDER BY h.sequence)
                       FROM yaya_skill_build_history h
                       WHERE h.tenant_id=b.tenant_id AND h.build_id=b.build_id
                   ),'[]'::jsonb) AS history_json
            FROM yaya_skill_builds b
            WHERE tenant_id=%s AND build_id=%s AND actor_id=%s
            """,
            (actor.tenant_id, build_id, actor.actor_id),
        )
        source = _mapping(row["source_bundle_json"], "Build source bundle")
        payload = _mapping(row["resource_json"], "Build resource")
        try:
            source_sha256 = canonical_source_bundle_sha256(source)
        except SourceBundleValidationError as error:
            raise _error(
                "INVARIANT_VIOLATION", "VALIDATE", "Build source bundle drifted"
            ) from error
        if (
            source_sha256 != row["source_bundle_sha256"]
            or canonical_json_sha256(payload) != row["resource_sha256"]
            or payload.get("build_id") != build_id
            or payload.get("skill_id") != row["skill_id"]
            or payload.get("status") != row["status"]
        ):
            raise _error("INVARIANT_VIOLATION", "VALIDATE", "Build authority drifted")
        self._validator.validate("schemas/game/skill-build.schema.json", payload)
        self._validate_build_history(
            row["history_json"], payload, cast(str, row["resource_sha256"])
        )
        if payload.get("status") == "CERTIFIED":
            await self._validate_certified_build(actor, row, payload)
        return ResourceResult(payload, {})

    def _validate_build_history(
        self,
        raw_history: object,
        current: Mapping[str, object],
        current_sha256: str,
    ) -> None:
        if not isinstance(raw_history, list):
            raise _error("INVARIANT_VIOLATION", "VALIDATE", "Build history is invalid")
        rows = cast(list[object], raw_history)
        status = current.get("status")
        if not isinstance(status, str):
            raise _error("INVARIANT_VIOLATION", "VALIDATE", "Build history status drifted")
        failure = current.get("failure")
        validation_failed = (
            status == "FAILED"
            and isinstance(failure, Mapping)
            and cast(Mapping[object, object], failure).get("stage") == "VALIDATE_SOURCE"
        )
        direct_validation_failure = validation_failed and len(rows) == 2
        expected = {
            "ACCEPTED": ("ACCEPTED",),
            "COMPILING": ("ACCEPTED", "COMPILING"),
            "CERTIFIED": ("ACCEPTED", "COMPILING", "CERTIFIED"),
            "REJECTED": ("ACCEPTED", "COMPILING", "REJECTED"),
            "FAILED": (
                ("ACCEPTED", "FAILED")
                if direct_validation_failure
                else ("ACCEPTED", "COMPILING", "FAILED")
            ),
        }.get(status)
        if expected is None or len(rows) != len(expected):
            raise _error("INVARIANT_VIOLATION", "VALIDATE", "Build history length drifted")
        last_record: Mapping[str, object] | None = None
        last_sha256: object = None
        for sequence, (raw_row, expected_status) in enumerate(
            zip(rows, expected, strict=True), start=1
        ):
            history = _mapping(raw_row, "Build history row")
            record = _mapping(history.get("record_json"), "Build history resource")
            record_sha256 = canonical_json_sha256(record)
            if (
                history.get("sequence") != sequence
                or history.get("status") != expected_status
                or record.get("status") != expected_status
                or history.get("record_sha256") != record_sha256
            ):
                raise _error("INVARIANT_VIOLATION", "VALIDATE", "Build history drifted")
            self._validator.validate("schemas/game/skill-build.schema.json", record)
            last_record = record
            last_sha256 = history.get("record_sha256")
        if last_record != current or last_sha256 != current_sha256:
            raise _error("INVARIANT_VIOLATION", "VALIDATE", "Build head/history drifted")

    async def _validate_certified_build(
        self,
        actor: ActorRef,
        row: Mapping[str, object],
        payload: Mapping[str, object],
    ) -> None:
        artifact = _mapping(payload.get("artifact"), "Build artifact")
        certification = _mapping(payload.get("certification"), "Build certification")
        skill_version_id = cast(str, payload["skill_version_id"])
        connection: AsyncConnection[dict[str, object]] | None = None
        try:
            connection = await self._database.connect(autocommit=True)
            await self.validate_certification_closure(
                connection,
                actor,
                content_hash=cast(str, row["content_hash"]),
                build_id=cast(str, payload["build_id"]),
                skill_id=cast(str, row["skill_id"]),
                skill_version_id=skill_version_id,
                certification_id=cast(str, certification["certification_id"]),
                artifact_sha256=cast(str, artifact["artifact_sha256"]),
                build_payload=payload,
                require_unrevoked=False,
            )
        except BackendApplicationError:
            raise
        except (ValueError, TypeError, KeyError, SourceBundleValidationError) as error:
            raise _error(
                "INVARIANT_VIOLATION", "VALIDATE", "Certified Build closure drifted"
            ) from error
        except psycopg.Error as error:
            raise _error("DEPENDENCY_UNAVAILABLE", "VALIDATE", "PostgreSQL query failed") from error
        finally:
            if connection is not None:
                await connection.close()

    async def validate_certification_closure(
        self,
        connection: AsyncConnection[dict[str, object]],
        actor: ActorRef,
        *,
        content_hash: str,
        build_id: str,
        skill_id: str,
        skill_version_id: str,
        certification_id: str,
        artifact_sha256: str,
        build_payload: Mapping[str, object] | None,
        require_unrevoked: bool,
    ) -> _ValidatedCertification:
        cursor = await connection.execute(
            """
            SELECT c.actor_id,c.content_hash,c.skill_id,c.artifact_sha256,c.build_id,
                   c.skill_version_id,c.certification_id,c.certification_sha256,
                   c.record_json,c.issued_at,a.source_sha256,a.artifact_uri,
                   a.metadata_json,s.snapshot_json AS skill_json,
                   lc.record_json AS legacy_certification_json,lc.rejected,
                   cr.snapshot_json AS compile_json,e.evidence_id,e.evidence_type,
                   e.payload_sha256,e.evidence_json,r.revocation_id,
                   b.command_id,b.status AS build_status,b.terminal AS build_terminal,
                   b.resource_json AS build_json,b.resource_sha256 AS build_resource_sha256,
                   b.source_bundle_json,b.source_bundle_sha256,b.build_policy_id,
                   b.client_draft_revision,b.compiler_profile AS build_compiler_profile,
                   b.test_suite_version AS build_test_suite_version,
                   b.requested_capabilities_json,
                   p.compiler_profile AS policy_compiler_profile,
                   p.test_suite_version AS policy_test_suite_version,
                   p.compiler_image,p.compiler_version,p.compile_flags_json,
                   p.public_tests_json,p.hidden_tests_json,p.approved_capabilities_json,
                   p.limits_json,p.parameter_schema_json,p.semantic_version_major,
                   p.semantic_version_minor,p.runtime_abi_version,p.policy_sha256,
                   COALESCE((
                       SELECT jsonb_agg(jsonb_build_object(
                           'sequence',h.sequence,'status',h.status,
                           'record_sha256',h.record_sha256,'record_json',h.record_json
                       ) ORDER BY h.sequence)
                       FROM yaya_skill_build_history h
                       WHERE h.tenant_id=b.tenant_id AND h.build_id=b.build_id
                   ),'[]'::jsonb) AS history_json,
                   COALESCE((
                       SELECT jsonb_agg(jsonb_build_object(
                           'step',sr.step,'attempt',sr.attempt,
                           'input_sha256',sr.input_sha256,
                           'output_sha256',sr.output_sha256,
                           'outcome',sr.outcome,'receipt_json',sr.receipt_json,
                           'completed_at',sr.completed_at
                       ) ORDER BY sr.attempt,CASE sr.step
                           WHEN 'VALIDATE_SOURCE' THEN 1 WHEN 'COMPILE' THEN 2
                           WHEN 'PUBLIC_TEST' THEN 3 WHEN 'HIDDEN_TEST' THEN 4
                           WHEN 'CERTIFY' THEN 5 END)
                       FROM yaya_build_step_receipts sr
                       WHERE sr.tenant_id=b.tenant_id AND sr.build_id=b.build_id
                   ),'[]'::jsonb) AS receipts_json
            FROM yaya_skill_certifications c
            JOIN yaya_skill_builds b
              ON b.tenant_id=c.tenant_id AND b.build_id=c.build_id
             AND b.skill_id=c.skill_id AND b.actor_id=c.actor_id
             AND b.content_hash=c.content_hash
            JOIN yaya_build_policies p
              ON p.tenant_id=b.tenant_id AND p.build_policy_id=b.build_policy_id
             AND p.actor_id=b.actor_id AND p.content_hash=b.content_hash
            JOIN yaya_artifacts a
              ON a.tenant_id=c.tenant_id AND a.artifact_sha256=c.artifact_sha256
             AND a.build_id=c.build_id AND a.skill_id=c.skill_id
             AND a.actor_id=c.actor_id AND a.content_hash=c.content_hash
            JOIN yaya_skills s
              ON s.tenant_id=c.tenant_id AND s.skill_id=c.skill_id
             AND s.skill_version_id=c.skill_version_id
             AND s.certification_id=c.certification_id
             AND s.artifact_sha256=c.artifact_sha256
             AND s.actor_id=c.actor_id AND s.content_hash=c.content_hash
            JOIN yaya_registry_certifications lc
              ON lc.tenant_id=c.tenant_id AND lc.certification_id=c.certification_id
             AND lc.skill_id=c.skill_id AND lc.skill_version_id=c.skill_version_id
             AND lc.artifact_sha256=c.artifact_sha256
            JOIN yaya_compile_results cr
              ON cr.tenant_id=c.tenant_id AND cr.build_id=c.build_id
             AND cr.actor_id=c.actor_id AND cr.content_hash=c.content_hash
            JOIN yaya_evidence e
              ON e.tenant_id=c.tenant_id
             AND e.evidence_id=c.record_json #>> '{evidence_ref,evidence_id}'
             AND e.actor_id=c.actor_id AND e.content_hash=c.content_hash
            LEFT JOIN yaya_certification_revocations r
              ON r.tenant_id=c.tenant_id AND r.certification_id=c.certification_id
            WHERE c.tenant_id=%s AND c.actor_id=%s AND c.content_hash=%s
              AND c.build_id=%s AND c.skill_id=%s AND c.skill_version_id=%s
              AND c.certification_id=%s AND c.artifact_sha256=%s
            """,
            (
                actor.tenant_id,
                actor.actor_id,
                content_hash,
                build_id,
                skill_id,
                skill_version_id,
                certification_id,
                artifact_sha256,
            ),
        )
        result = await cursor.fetchone()
        if result is None:
            raise _error("SKILL_NOT_CERTIFIED", "REGISTRY", "Certification closure was not found")
        certification_record = _mapping(result["record_json"], "Certification record")
        metadata = _mapping(result["metadata_json"], "Artifact metadata")
        stored_build = _mapping(result["build_json"], "Build resource")
        source_bundle = _mapping(result["source_bundle_json"], "Build source bundle")
        evidence_document = _mapping(result["evidence_json"], "Build Evidence")
        evidence_payload = _mapping(evidence_document.get("payload"), "Build Evidence payload")
        evidence_ref = _mapping(evidence_document.get("evidence_ref"), "Build Evidence ref")
        certified = decode_as(result["legacy_certification_json"], CertifiedSkill)
        skill = decode_as(result["skill_json"], SkillSnapshot)
        compile_result = decode_as(result["compile_json"], CompileResultSnapshot)
        source_sha256 = canonical_source_bundle_sha256(source_bundle)
        policy_projection = {
            "build_policy_id": result["build_policy_id"],
            "actor_id": result["actor_id"],
            "content_hash": result["content_hash"],
            "compiler_profile": result["policy_compiler_profile"],
            "test_suite_version": result["policy_test_suite_version"],
            "compiler_image": result["compiler_image"],
            "compiler_version": result["compiler_version"],
            "compile_flags": result["compile_flags_json"],
            "public_tests": result["public_tests_json"],
            "hidden_tests": result["hidden_tests_json"],
            "approved_capabilities": result["approved_capabilities_json"],
            "limits": result["limits_json"],
            "parameter_schema": result["parameter_schema_json"],
            "semantic_version_major": result["semantic_version_major"],
            "semantic_version_minor": result["semantic_version_minor"],
            "runtime_abi_version": result["runtime_abi_version"],
        }
        policy_sha256 = canonical_json_sha256(policy_projection)
        requested_capabilities = _sequence(
            result["requested_capabilities_json"], "requested capabilities"
        )
        approved_capabilities = _sequence(
            result["approved_capabilities_json"], "approved capabilities"
        )
        expected_tests: list[dict[str, object]] = []
        for visibility, raw_tests in (
            ("PUBLIC", result["public_tests_json"]),
            ("HIDDEN", result["hidden_tests_json"]),
        ):
            for raw_test in _sequence(raw_tests, f"{visibility} policy tests"):
                test = _mapping(raw_test, f"{visibility} policy test")
                if test.get("visibility") != visibility:
                    raise ValueError("policy test visibility drifted")
                expected_tests.append(
                    {
                        "test_case_id": test.get("test_case_id"),
                        "visibility": visibility,
                        "status": "PASSED",
                        "diagnostic_codes": [],
                    }
                )
        build_artifact = _mapping(stored_build.get("artifact"), "Build artifact")
        build_certification = _mapping(stored_build.get("certification"), "Build certification")
        if build_payload is not None and stored_build != build_payload:
            raise ValueError("Build query and Certification closure disagree")
        self._validate_build_history(
            result["history_json"], stored_build, cast(str, result["build_resource_sha256"])
        )
        entrypoint = source_bundle.get("entrypoint")
        entrypoint_files = [
            _mapping(item, "Build source file")
            for item in _sequence(source_bundle.get("files"), "Build source files")
            if _mapping(item, "Build source file").get("path") == entrypoint
        ]
        if len(entrypoint_files) != 1:
            raise ValueError("Build entrypoint closure drifted")
        entrypoint_file = entrypoint_files[0]
        expected_parameter_schema = _mapping(
            certification_record.get("parameter_schema"), "Certified parameter schema"
        )
        certified_at = certification_record.get("certified_at")
        try:
            build_context = _mapping(stored_build.get("request_context"), "Build request context")
            certification_context = _mapping(
                certification_record.get("request_context"),
                "Certification request context",
            )
            certification_evidence_ref = _mapping(
                certification_record.get("evidence_ref"),
                "Certification Evidence ref",
            )
            build_evidence_refs = [
                _mapping(item, "Build Evidence ref")
                for item in _sequence(stored_build.get("evidence_refs"), "Build Evidence refs")
            ]
            build_versions = _mapping(stored_build.get("versions"), "Build versions")
            certification_versions = _mapping(
                certification_record.get("versions"),
                "Certification versions",
            )
            evidence_versions = _mapping(
                evidence_document.get("versions"),
                "Build Evidence versions",
            )
            expected_versions = _version_wire(
                replace(
                    self._versions,
                    skill_version=skill_version_id,
                    artifact_sha256=artifact_sha256,
                    compiler_version=cast(str, result["compiler_version"]),
                    sandbox_image_digest=cast(str, result["compiler_image"]),
                    test_suite_version=cast(str, result["build_test_suite_version"]),
                )
            )
            build_identity = metadata.get("build_identity")
            certified_phases = self._validate_certified_phases(
                stored_build,
                certification_record,
            )
        except BackendApplicationError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise _error(
                "INVARIANT_VIOLATION",
                "VALIDATE",
                "Certified Build authority projection drifted",
            ) from error
        expected_metadata = {
            "build_id": build_id,
            "client_draft_revision": result["client_draft_revision"],
            "display_name": certification_record.get("display_name"),
            "evidence_id": result["evidence_id"],
            "source_bundle_sha256": source_sha256,
            "build_policy_id": result["build_policy_id"],
            "policy_sha256": policy_sha256,
        }
        expected_evidence_payload = {
            "evidence_kind": "BUILD_CERTIFICATION",
            "build_id": build_id,
            "skill_id": skill_id,
            "skill_version_id": skill_version_id,
            "artifact_sha256": artifact_sha256,
            "test_suite_version": result["build_test_suite_version"],
            "outcome": "CERTIFIED",
        }
        if (
            result["actor_id"] != actor.actor_id
            or result["content_hash"] != content_hash
            or result["skill_id"] != skill_id
            or result["artifact_sha256"] != artifact_sha256
            or result["build_id"] != build_id
            or result["skill_version_id"] != skill_version_id
            or result["certification_id"] != certification_id
            or result["build_status"] != "CERTIFIED"
            or result["build_terminal"] is not True
            or canonical_json_sha256(stored_build) != result["build_resource_sha256"]
            or stored_build.get("status") != "CERTIFIED"
            or stored_build.get("build_id") != build_id
            or stored_build.get("skill_id") != skill_id
            or stored_build.get("skill_version_id") != skill_version_id
            or build_context != certification_context
            or len(build_evidence_refs) != 1
            or build_evidence_refs[0] != certification_evidence_ref
            or certification_evidence_ref != evidence_ref
            or build_versions != certification_versions
            or certification_versions != evidence_versions
            or evidence_versions != expected_versions
            or source_sha256 != result["source_bundle_sha256"]
            or source_sha256 != result["source_sha256"]
            or build_artifact.get("artifact_sha256") != artifact_sha256
            or build_artifact.get("source_sha256") != source_sha256
            or build_certification.get("certification_id") != certification_id
            or canonical_json_sha256(certification_record) != result["certification_sha256"]
            or certification_record.get("build_id") != build_id
            or certification_record.get("command_id") != result["command_id"]
            or certification_record.get("skill_id") != skill_id
            or certification_record.get("skill_version_id") != skill_version_id
            or certification_record.get("artifact_sha256") != artifact_sha256
            or certification_record.get("source_bundle_sha256") != source_sha256
            or certification_record.get("build_policy_id") != result["build_policy_id"]
            or certification_record.get("policy_sha256") != policy_sha256
            or certification_record.get("client_draft_revision") != result["client_draft_revision"]
            or certification_record.get("compiler_profile") != result["build_compiler_profile"]
            or certification_record.get("compiler_version") != result["compiler_version"]
            or certification_record.get("compiler_image") != result["compiler_image"]
            or certification_record.get("test_suite_version") != result["build_test_suite_version"]
            or certification_record.get("runtime_abi_version") != result["runtime_abi_version"]
            or certification_record.get("tests") != expected_tests
            or certification_record.get("requested_capabilities") != requested_capabilities
            or certification_record.get("approved_capabilities") != approved_capabilities
            or certification_record.get("certified_at") != _iso(cast(datetime, result["issued_at"]))
            or build_certification.get("issued_at") != certified_at
            or build_certification.get("capabilities") != requested_capabilities
            or result["build_compiler_profile"] != result["policy_compiler_profile"]
            or result["build_test_suite_version"] != result["policy_test_suite_version"]
            or policy_sha256 != result["policy_sha256"]
            or evidence_ref.get("evidence_id") != result["evidence_id"]
            or evidence_ref.get("evidence_type") != result["evidence_type"]
            or evidence_ref.get("sha256") != result["payload_sha256"]
            or canonical_json_sha256(evidence_payload) != result["payload_sha256"]
            or evidence_payload != expected_evidence_payload
            or evidence_document.get("request_context")
            != certification_record.get("request_context")
            or evidence_document.get("versions") != certification_record.get("versions")
            or _mapping(evidence_document.get("source"), "Build Evidence source")
            != {
                "source_type": "SKILL_BUILD",
                "source_id": build_id,
                "command_id": result["command_id"],
                "world_id": certification_record.get("world_id"),
            }
            or _mapping(evidence_document.get("subject"), "Build Evidence subject").get(
                "learner_id"
            )
            != certification_record.get("learner_id")
            or _mapping(evidence_document.get("integrity"), "Build Evidence integrity")
            != {"payload_sha256": result["payload_sha256"], "previous_evidence_sha256": None}
            or evidence_document.get("related_evidence") != []
            or result["rejected"] is not False
            or (require_unrevoked and result["revocation_id"] is not None)
            or certified.certification_id != certification_id
            or certified.skill_id != skill_id
            or certified.skill_version_id != skill_version_id
            or certified.semantic_version != certification_record.get("semantic_version")
            or certified.artifact.artifact_sha256 != artifact_sha256
            or certified.artifact.source_sha256 != source_sha256
            or certified.artifact.compiler_profile != result["build_compiler_profile"]
            or certified.artifact.compiler_version != result["compiler_version"]
            or certified.artifact.sandbox_image_digest != result["compiler_image"]
            or certified.artifact.test_suite_version != result["build_test_suite_version"]
            or certified.artifact.artifact_uri != result["artifact_uri"]
            or list(certified.capabilities) != requested_capabilities
            or _iso(certified.certified_at) != certified_at
            or certified.revoked_at is not None
            or dict(certified.metadata) != expected_metadata
            or skill.ref.skill_id != skill_id
            or skill.ref.skill_version_id != skill_version_id
            or skill.ref.certification_id != certification_id
            or skill.ref.artifact_sha256 != artifact_sha256
            or skill.entrypoint != entrypoint
            or skill.source_code != entrypoint_file.get("content")
            or skill.source_sha256 != entrypoint_file.get("content_sha256")
            or plain(skill.parameter_schema) != expected_parameter_schema
            or _context_wire(skill.request_context) != certification_context
            or not isinstance(skill.request_context, OperationContext)
            or skill.request_context.command_id != result["command_id"]
            or skill.request_context.actor.tenant_id != actor.tenant_id
            or skill.request_context.actor.actor_id != actor.actor_id
            or skill.request_context.content_ref.content_hash != content_hash
            or compile_result.build_id != build_id
            or compile_result.skill_ref != skill.ref
            or not compile_result.succeeded
            or compile_result.diagnostics
            or len(compile_result.evidence_refs) != 1
            or _evidence_ref_wire(compile_result.evidence_refs[0]) != evidence_ref
            or compile_result.request_context != skill.request_context
            or metadata.get("artifact_sha256") != artifact_sha256
            or metadata.get("artifact_uri") != result["artifact_uri"]
            or metadata.get("source_sha256") != source_sha256
            or metadata.get("build_policy_id") != result["build_policy_id"]
            or metadata.get("policy_sha256") != policy_sha256
            or metadata.get("compiler_profile") != result["build_compiler_profile"]
            or metadata.get("compiler_version") != result["compiler_version"]
            or metadata.get("compiler_image") != result["compiler_image"]
            or metadata.get("test_suite_version") != result["build_test_suite_version"]
            or not isinstance(build_identity, str)
            or _SHA256.fullmatch(build_identity) is None
        ):
            raise _error("INVARIANT_VIOLATION", "VALIDATE", "Certified Build closure drifted")
        self._validator.validate("schemas/game/evidence.schema.json", evidence_document)
        self._validate_certification_receipts(
            result["receipts_json"],
            result,
            expected_tests,
            certified_phases,
            build_identity,
        )
        if self._artifact_root is None:
            raise _error("INVARIANT_VIOLATION", "VALIDATE", "Artifact verifier is not configured")
        await self._verify_published_artifact(
            self._artifact_root,
            artifact_sha256,
            cast(int, metadata.get("size_bytes")),
        )
        return _ValidatedCertification(certified, skill, compile_result, certification_record)

    @staticmethod
    def _validate_certified_phases(
        stored_build: Mapping[str, object],
        certification_record: Mapping[str, object],
    ) -> list[dict[str, object]]:
        try:
            phases = [
                _mapping(item, "Certified Build phase")
                for item in _sequence(stored_build.get("phases"), "Certified Build phases")
            ]
            if len(phases) != len(_BUILD_PHASES) or [item.get("name") for item in phases] != list(
                _BUILD_PHASES
            ):
                raise ValueError("Certified Build phase sequence drifted")
            created_at = _utc_timestamp(stored_build.get("created_at"), "Build created_at")
            updated_at = _utc_timestamp(stored_build.get("updated_at"), "Build updated_at")
            certified_at = _utc_timestamp(
                certification_record.get("certified_at"),
                "Certification certified_at",
            )
            if updated_at != certified_at:
                raise ValueError("Build completion timestamp drifted")
            for phase in phases:
                started_at = _utc_timestamp(
                    phase.get("started_at"),
                    f"{phase.get('name')} started_at",
                )
                finished_at = _utc_timestamp(
                    phase.get("finished_at"),
                    f"{phase.get('name')} finished_at",
                )
                if (
                    phase.get("status") != "PASSED"
                    or phase.get("diagnostic_codes") != []
                    or not created_at <= started_at <= finished_at
                    or finished_at != certified_at
                ):
                    raise ValueError("Certified Build phase authority drifted")
            return phases
        except BackendApplicationError:
            raise
        except (TypeError, ValueError) as error:
            raise _error(
                "INVARIANT_VIOLATION",
                "VALIDATE",
                "Certified Build phases drifted",
            ) from error

    @staticmethod
    def _validate_certification_receipts(
        raw_receipts: object,
        row: Mapping[str, object],
        expected_tests: list[dict[str, object]],
        build_phases: list[dict[str, object]],
        build_identity: str,
    ) -> None:
        try:
            receipts = [
                _mapping(item, "Build step receipt")
                for item in _sequence(raw_receipts, "Build step receipts")
            ]
            if len(receipts) != len(_BUILD_PHASES) or [
                item.get("step") for item in receipts
            ] != list(_BUILD_PHASES):
                raise ValueError("Certification receipt sequence drifted")
            attempts = {item.get("attempt") for item in receipts}
            if len(attempts) != 1:
                raise ValueError("Certification receipts span multiple attempts")
            public_tests = [item for item in expected_tests if item["visibility"] == "PUBLIC"]
            hidden_tests = [item for item in expected_tests if item["visibility"] == "HIDDEN"]
            for stored, phase, build_phase in zip(
                receipts,
                _BUILD_PHASES,
                build_phases,
                strict=True,
            ):
                receipt = _mapping(stored.get("receipt_json"), "Build step receipt payload")
                expected_phase_tests = (
                    public_tests
                    if phase == "PUBLIC_TEST"
                    else hidden_tests
                    if phase == "HIDDEN_TEST"
                    else []
                )
                if (
                    stored.get("outcome") != "PASSED"
                    or canonical_json_sha256(receipt) != stored.get("output_sha256")
                    or receipt.get("build_id") != row["build_id"]
                    or receipt.get("build_identity") != build_identity
                    or receipt.get("step") != phase
                    or receipt.get("attempt") != stored.get("attempt")
                    or receipt.get("source_sha256") != row["source_bundle_sha256"]
                    or receipt.get("build_policy_id") != row["build_policy_id"]
                    or receipt.get("policy_sha256") != row["policy_sha256"]
                    or receipt.get("outcome") != "PASSED"
                    or receipt.get("pipeline_status") != "SUCCEEDED"
                    or receipt.get("terminal_failure_code") is not None
                    or receipt.get("artifact_sha256") != row["artifact_sha256"]
                    or receipt.get("test_results") != expected_phase_tests
                    or build_phase.get("name") != phase
                    or build_phase.get("status") != stored.get("outcome")
                    or _utc_timestamp(
                        stored.get("completed_at"),
                        f"{phase} receipt completed_at",
                    )
                    != _utc_timestamp(
                        build_phase.get("finished_at"),
                        f"{phase} finished_at",
                    )
                    or stored.get("input_sha256")
                    != canonical_json_sha256(
                        {
                            "build_id": row["build_id"],
                            "step": phase,
                            "source_sha256": row["source_bundle_sha256"],
                            "build_policy_id": row["build_policy_id"],
                            "policy_sha256": row["policy_sha256"],
                        }
                    )
                ):
                    raise ValueError("Certification receipt closure drifted")
        except BackendApplicationError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise _error(
                "INVARIANT_VIOLATION",
                "VALIDATE",
                "Certification receipt closure drifted",
            ) from error

    @staticmethod
    async def _verify_published_artifact(root: Path, digest: str, size_bytes: int) -> None:
        path = root / digest[:2] / digest
        try:
            resolved = path.resolve(strict=True)
            metadata = path.lstat()
            if (
                resolved != path.absolute()
                or resolved.parent != root / digest[:2]
                or path.is_symlink()
                or not path.is_file()
                or metadata.st_mode & 0o222
                or metadata.st_size != size_bytes
            ):
                raise ValueError("Artifact path is not immutable")
            actual = await asyncio.to_thread(lambda: hashlib.sha256(path.read_bytes()).hexdigest())
        except (OSError, ValueError) as error:
            raise _error(
                "INVARIANT_VIOLATION", "VALIDATE", "Certified Artifact is unavailable"
            ) from error
        if actual != digest:
            raise _error("INVARIANT_VIOLATION", "VALIDATE", "Certified Artifact bytes drifted")

    async def get_activation(self, activation_id: str, actor: ActorRef) -> ResourceResult:
        if _RESOURCE_ID.fullmatch(activation_id) is None:
            raise _error("INVALID_REQUEST", "VALIDATE", "activation_id is invalid")
        row = await self._read_one(
            """
            SELECT actor_id,content_hash,skill_id,skill_version_id,certification_id,
                   artifact_sha256,world_id,agent_profile_id,previous_registry_revision,
                   registry_revision,record_json,activation_sha256
            FROM yaya_skill_activations
            WHERE tenant_id=%s AND activation_id=%s AND actor_id=%s
            """,
            (actor.tenant_id, activation_id, actor.actor_id),
        )
        payload = _mapping(row["record_json"], "Activation resource")
        scope = _mapping(payload.get("activation_scope"), "Activation scope")
        if (
            canonical_json_sha256(payload) != row["activation_sha256"]
            or payload.get("activation_id") != activation_id
            or payload.get("skill_id") != row["skill_id"]
            or payload.get("skill_version_id") != row["skill_version_id"]
            or payload.get("certification_id") != row["certification_id"]
            or payload.get("artifact_sha256") != row["artifact_sha256"]
            or scope.get("world_id") != row["world_id"]
            or scope.get("agent_profile_id") != row["agent_profile_id"]
            or payload.get("previous_registry_revision") != row["previous_registry_revision"]
            or payload.get("registry_revision") != row["registry_revision"]
        ):
            raise _error("INVARIANT_VIOLATION", "VALIDATE", "Activation authority drifted")
        self._validator.validate("schemas/game/skill-activation.schema.json", payload)
        return ResourceResult(payload, {})

    async def _read_one(
        self,
        sql: str,
        parameters: tuple[object, ...],
    ) -> dict[str, object]:
        connection: AsyncConnection[dict[str, object]] | None = None
        try:
            connection = await self._database.connect(autocommit=True)
            cursor = await connection.execute(cast(LiteralString, sql), parameters)
            row = await cursor.fetchone()
            if row is None:
                raise _error("NOT_FOUND", "VALIDATE", "Resource was not found")
            return row
        except BackendApplicationError:
            raise
        except psycopg.Error as error:
            raise _error("DEPENDENCY_UNAVAILABLE", "VALIDATE", "PostgreSQL query failed") from error
        finally:
            if connection is not None:
                await connection.close()


@dataclass(frozen=True, slots=True)
class _ClaimedControlJob:
    tenant_id: str
    job_id: str
    command_id: str
    authority_id: str
    actor_id: str
    content_hash: str
    operation: Literal["CREATE_AGENT_SESSION", "CREATE_SKILL_BUILD", "ACTIVATE_SKILL_VERSION"]
    subject_id: str
    resource_id: str
    request_target: str
    request_body: bytes
    request_json: Mapping[str, object]
    context: OperationContext
    worker_id: str
    lease_id: str
    fencing_token: int
    attempt: int


# Public integration type for the separately testable Build executor.  The
# worker remains the sole producer of these fenced claims.
BuildJobClaim = _ClaimedControlJob


def _contract_error(code: str, stage: str, message: str) -> ContractError:
    metadata = {
        "INVALID_REQUEST": (ErrorCategory.VALIDATION, False, "request.invalid"),
        "CONTENT_VERSION_MISMATCH": (
            ErrorCategory.VALIDATION,
            False,
            "content.version_mismatch",
        ),
        "NOT_FOUND": (ErrorCategory.VALIDATION, False, "resource.not_found"),
        "WORLD_REVISION_CONFLICT": (
            ErrorCategory.CONCURRENCY,
            True,
            "world.changed_retry",
        ),
        "SKILL_NOT_CERTIFIED": (ErrorCategory.SKILL, False, "skill.not_certified"),
        "SKILL_VERSION_MISMATCH": (
            ErrorCategory.SKILL,
            False,
            "skill.version_mismatch",
        ),
        "DEPENDENCY_UNAVAILABLE": (
            ErrorCategory.DEPENDENCY,
            True,
            "dependency.temporarily_unavailable",
        ),
        "INVARIANT_VIOLATION": (
            ErrorCategory.INVARIANT,
            False,
            "system.invariant_violation",
        ),
        "INTERNAL_ERROR": (ErrorCategory.INTERNAL, False, "system.internal_error"),
    }
    category, retryable, key = metadata.get(
        code,
        (ErrorCategory.INTERNAL, False, "system.internal_error"),
    )
    stable_code = code if code in metadata else "INTERNAL_ERROR"
    stable_stage = stage if re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", stage) else "VALIDATE"
    return ContractError(
        code=stable_code,
        category=category,
        retryable=retryable,
        user_message_key=key,
        stage=stable_stage,
        message=message[:512] or "Control job failed",
        details=cast(FrozenJsonObject, {}),
    )


class StudentSkillChainWorker:
    """Lease and fence the three turn-free public control operations."""

    def __init__(
        self,
        *,
        database: PostgresDatabase,
        application: StudentSkillChainApplication,
        validator: ContractSchemaValidator,
        worker_id: str,
        artifact_root: Path,
        lease_seconds: int = 120,
        poll_ms: int = 100,
        build_executor: object | None = None,
    ) -> None:
        if not worker_id.strip() or len(worker_id) > 128:
            raise ValueError("control worker_id must contain 1..128 characters")
        if not 2 <= lease_seconds <= 3600:
            raise ValueError("control lease_seconds must be between 2 and 3600")
        if not 10 <= poll_ms <= 60_000:
            raise ValueError("control poll_ms must be between 10 and 60000")
        root = artifact_root.expanduser().resolve()
        if not root.is_dir():
            raise ValueError("artifact_root must be an existing directory")
        self._database = database
        self._application = application
        self._validator = validator
        self._worker_id = worker_id
        self._artifact_root = root
        self._lease_seconds = lease_seconds
        self._poll_seconds = poll_ms / 1000
        self._build_executor = build_executor

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                handled = await self.run_once()
            except (PostgresCommitStateUnknown, psycopg.Error):
                # Claim acquisition has no external side effect before its
                # transaction commits.  A transient database outage or an
                # unknown COMMIT acknowledgement must not kill the durable
                # worker loop; the next poll observes either the committed
                # lease or the still-claimable job.
                handled = False
            if handled:
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                pass

    async def run_once(self) -> bool:
        claim = await self._claim_one()
        if claim is None:
            return False
        try:
            if claim.operation == "CREATE_AGENT_SESSION":
                await self._create_session(claim)
            elif claim.operation == "ACTIVATE_SKILL_VERSION":
                await self._activate_skill(claim)
            else:
                await self._build_skill(claim)
        except PostgresCommitStateUnknown:
            # The next process observes the terminal job or an expired lease.
            # Never repeat an external build step merely because COMMIT's
            # acknowledgement disappeared.
            return True
        except psycopg.Error:
            # Transaction-body database failures are recovery uncertainty,
            # not a student Build outcome.  Retain the fenced lease and let a
            # later poll/takeover reconcile it after expiry instead of marking
            # the Command failed while its Build remains in progress.
            return True
        except BackendApplicationError as error:
            await self._fail_claim(claim, error)
        except Exception as error:
            await self._fail_claim(
                claim,
                _error(
                    "INTERNAL_ERROR",
                    "VALIDATE",
                    "Control worker encountered an unexpected failure",
                    {"exception_type": type(error).__name__},
                ),
            )
        return True

    async def _claim_one(self) -> _ClaimedControlJob | None:
        async with self._database.transaction_with_commit_boundary() as connection:
            cursor = await connection.execute(
                """
                SELECT j.*,c.record_json AS command_json,c.revision AS command_revision,
                       c.status AS command_status
                FROM yaya_control_jobs j
                JOIN yaya_commands c
                  ON c.tenant_id=j.tenant_id AND c.command_id=j.command_id
                WHERE (j.state='READY' AND j.available_at<=clock_timestamp())
                   OR (j.state='LEASED' AND j.lease_expires_at<=clock_timestamp())
                ORDER BY j.available_at,j.tenant_id,j.job_id
                FOR UPDATE OF j,c SKIP LOCKED LIMIT 1
                """
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            attempt = cast(int, row["attempt"]) + 1
            lease_id = _identifier("lease", self._worker_id, cast(str, row["job_id"]), str(attempt))
            command = decode_as(row["command_json"], CommandRecord)
            claimed_at = max(datetime.now(UTC), command.updated_at)
            lease_expires = claimed_at + timedelta(seconds=self._lease_seconds)
            if command.status is CommandStatus.ACCEPTED:
                command = replace(
                    command,
                    status=CommandStatus.VALIDATING,
                    stage="VALIDATE",
                    revision=command.revision + 1,
                    updated_at=claimed_at,
                )
                await connection.execute(
                    """
                    UPDATE yaya_commands
                    SET revision=%s,status=%s,updated_at=%s,record_json=%s
                    WHERE tenant_id=%s AND command_id=%s AND revision=%s
                      AND status='ACCEPTED'
                    """,
                    (
                        command.revision,
                        command.status.value,
                        command.updated_at,
                        Jsonb(encode(command)),
                        row["tenant_id"],
                        row["command_id"],
                        row["command_revision"],
                    ),
                )
            elif command.status.is_terminal:
                raise _error(
                    "INVARIANT_VIOLATION",
                    "VALIDATE",
                    "A terminal Command retained a claimable control job",
                )
            updated = await connection.execute(
                """
                UPDATE yaya_control_jobs
                SET state='LEASED',attempt=%s,fencing_token=%s,worker_id=%s,
                    lease_id=%s,claimed_at=%s,heartbeat_at=%s,lease_expires_at=%s,
                    updated_at=%s
                WHERE tenant_id=%s AND job_id=%s AND attempt=%s
                """,
                (
                    attempt,
                    attempt,
                    self._worker_id,
                    lease_id,
                    claimed_at,
                    claimed_at,
                    lease_expires,
                    claimed_at,
                    row["tenant_id"],
                    row["job_id"],
                    row["attempt"],
                ),
            )
            if updated.rowcount != 1:
                raise _error("INVARIANT_VIOLATION", "VALIDATE", "Control claim CAS was lost")
            request_body = row["request_body"]
            if not isinstance(request_body, bytes):
                raise _error("INVARIANT_VIOLATION", "VALIDATE", "Control request bytes drifted")
            request_json = _mapping(row["request_json"], "control request")
            if (
                hashlib.sha256(request_body).hexdigest() != row["request_sha256"]
                or _strict_object(request_body) != request_json
            ):
                raise _error("INVARIANT_VIOLATION", "VALIDATE", "Control request integrity drifted")
            operation = cast(str, row["operation"])
            if operation not in _JOB_TYPES:
                raise _error("INVARIANT_VIOLATION", "VALIDATE", "Control operation drifted")
            context = decode_as(row["operation_context_json"], OperationContext)
            if (
                context.command_id != row["command_id"]
                or context.actor.actor_id != row["actor_id"]
                or context.content_ref.content_hash != row["content_hash"]
            ):
                raise _error("INVARIANT_VIOLATION", "VALIDATE", "Control context drifted")
            return _ClaimedControlJob(
                tenant_id=cast(str, row["tenant_id"]),
                job_id=cast(str, row["job_id"]),
                command_id=cast(str, row["command_id"]),
                authority_id=cast(str, row["authority_id"]),
                actor_id=cast(str, row["actor_id"]),
                content_hash=cast(str, row["content_hash"]),
                operation=cast(
                    Literal[
                        "CREATE_AGENT_SESSION",
                        "CREATE_SKILL_BUILD",
                        "ACTIVATE_SKILL_VERSION",
                    ],
                    operation,
                ),
                subject_id=cast(str, row["subject_id"]),
                resource_id=cast(str, row["resource_id"]),
                request_target=cast(str, row["request_target"]),
                request_body=request_body,
                request_json=request_json,
                context=context,
                worker_id=self._worker_id,
                lease_id=lease_id,
                fencing_token=attempt,
                attempt=attempt,
            )

    async def heartbeat(self, claim: _ClaimedControlJob) -> None:
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=self._lease_seconds)
        async with self._database.transaction_with_commit_boundary() as connection:
            updated = await connection.execute(
                """
                UPDATE yaya_control_jobs
                SET heartbeat_at=%s,lease_expires_at=%s,updated_at=%s
                WHERE tenant_id=%s AND job_id=%s AND state='LEASED'
                  AND worker_id=%s AND lease_id=%s AND fencing_token=%s
                  AND lease_expires_at>clock_timestamp()
                """,
                (
                    now,
                    expires,
                    now,
                    claim.tenant_id,
                    claim.job_id,
                    claim.worker_id,
                    claim.lease_id,
                    claim.fencing_token,
                ),
            )
            if updated.rowcount != 1:
                raise _error("INVARIANT_VIOLATION", "VALIDATE", "Control lease was lost")

    async def _lock_claim(
        self,
        connection: AsyncConnection[dict[str, object]],
        claim: _ClaimedControlJob,
    ) -> dict[str, object]:
        cursor = await connection.execute(
            """
            SELECT j.*,c.record_json AS command_json
            FROM yaya_control_jobs j
            JOIN yaya_commands c
              ON c.tenant_id=j.tenant_id AND c.command_id=j.command_id
            WHERE j.tenant_id=%s AND j.job_id=%s AND j.state='LEASED'
              AND j.worker_id=%s AND j.lease_id=%s AND j.fencing_token=%s
              AND j.lease_expires_at>clock_timestamp()
            FOR UPDATE OF j,c
            """,
            (
                claim.tenant_id,
                claim.job_id,
                claim.worker_id,
                claim.lease_id,
                claim.fencing_token,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            raise _error("INVARIANT_VIOLATION", "VALIDATE", "Control lease was lost")
        return row

    async def _create_session(self, claim: _ClaimedControlJob) -> None:
        async with self._database.transaction_with_commit_boundary() as connection:
            row = await self._lock_claim(connection, claim)
            body = claim.request_json
            actor = claim.context.actor
            content_value = _mapping(body.get("content"), "Session content")
            content = ContentRef(
                unit_id=cast(str, content_value["unit_id"]),
                version=cast(str, content_value["version"]),
                content_hash=cast(str, content_value["content_hash"]),
            )
            authority = await self._application.resolve_launch_authority(
                connection,
                actor,
                content=content,
                world_id=cast(str, body["world_id"]),
                learner_id=cast(str, body["learner_id"]),
                agent_profile_id=cast(str, body["agent_profile_id"]),
            )
            if authority.authority_id != claim.authority_id:
                raise _error("INVARIANT_VIOLATION", "VALIDATE", "Session launch authority changed")
            cursor = await connection.execute(
                """
                SELECT revision,actor_id,content_hash FROM yaya_worlds
                WHERE tenant_id=%s AND world_id=%s AND actor_id=%s AND content_hash=%s
                FOR KEY SHARE
                """,
                (
                    claim.tenant_id,
                    authority.world_id,
                    claim.actor_id,
                    claim.content_hash,
                ),
            )
            world = await cursor.fetchone()
            if world is None:
                raise _error("NOT_FOUND", "VALIDATE", "Session World was not found")
            expected = body.get("expected_world_revision")
            if expected is not None and expected != world["revision"]:
                raise _error(
                    "WORLD_REVISION_CONFLICT",
                    "WORLD_VALIDATE",
                    "Session expected_world_revision is stale",
                )
            await self._validate_person_authorities(connection, authority, claim)
            created_at = datetime.now(UTC)
            request_context = RequestContext(
                request_id=claim.context.request_id,
                correlation_id=claim.context.correlation_id,
                trace_id=claim.context.trace_id,
                requested_at=claim.context.requested_at,
                actor=actor,
                content_ref=content,
                schema_version=claim.context.schema_version,
            )
            snapshot = SessionSnapshot(
                session_id=claim.resource_id,
                student_id=claim.actor_id,
                task_id=authority.task_id,
                world_id=authority.world_id,
                request_context=request_context,
            )
            resource: dict[str, object] = {
                "request_context": _context_wire(request_context),
                "session_id": claim.resource_id,
                "world_id": authority.world_id,
                "learner_id": authority.learner_id,
                "agent_profile_id": authority.agent_profile_id,
                "channel": body["channel"],
                "status": "ACTIVE",
                "created_at": _iso(created_at),
                "updated_at": _iso(created_at),
                "last_turn_sequence": 0,
                "content": _content_wire(content),
                "versions": _version_wire(authority.versions),
                "links": {
                    "self": f"/v1/agent-sessions/{claim.resource_id}",
                    "turns": f"/v1/agent-sessions/{claim.resource_id}/turns",
                    "world_snapshot": f"/v1/worlds/{authority.world_id}/snapshot",
                },
            }
            self._validator.validate("schemas/game/agent-session.schema.json", resource)
            resource_sha256 = canonical_json_sha256(resource)
            await connection.execute(
                """
                INSERT INTO yaya_agent_sessions(
                    tenant_id,session_id,actor_id,task_id,world_id,content_hash,
                    snapshot_json,client_turn_sequence
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,0)
                """,
                (
                    claim.tenant_id,
                    claim.resource_id,
                    claim.actor_id,
                    authority.task_id,
                    authority.world_id,
                    claim.content_hash,
                    Jsonb(encode(snapshot)),
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_public_agent_sessions(
                    tenant_id,session_id,authority_id,actor_id,content_hash,task_id,
                    world_id,learner_id,agent_profile_id,status,resource_sha256,
                    resource_json,created_at,updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'ACTIVE',%s,%s,%s,%s)
                """,
                (
                    claim.tenant_id,
                    claim.resource_id,
                    authority.authority_id,
                    claim.actor_id,
                    claim.content_hash,
                    authority.task_id,
                    authority.world_id,
                    authority.learner_id,
                    authority.agent_profile_id,
                    resource_sha256,
                    Jsonb(resource),
                    created_at,
                    created_at,
                ),
            )
            await self._succeed_claim(
                connection,
                claim,
                row,
                resource_type="AGENT_SESSION",
                resource_url=f"/v1/agent-sessions/{claim.resource_id}",
            )

    async def _validate_person_authorities(
        self,
        connection: AsyncConnection[dict[str, object]],
        authority: _LaunchAuthority,
        claim: _ClaimedControlJob,
    ) -> None:
        cursor = await connection.execute(
            """
            SELECT l.record_json AS learner_json,l.record_sha256 AS learner_sha256,
                   p.record_json AS profile_json,p.record_sha256 AS profile_sha256
            FROM yaya_learners l
            JOIN yaya_agent_profiles p
              ON p.tenant_id=l.tenant_id AND p.actor_id=l.actor_id
             AND p.content_hash=l.content_hash
            WHERE l.tenant_id=%s AND l.learner_id=%s AND l.actor_id=%s
              AND l.content_hash=%s AND p.agent_profile_id=%s
            """,
            (
                claim.tenant_id,
                authority.learner_id,
                claim.actor_id,
                claim.content_hash,
                authority.agent_profile_id,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            raise _error("NOT_FOUND", "VALIDATE", "Learner or Agent Profile was not found")
        learner = _mapping(row["learner_json"], "Learner authority")
        profile = _mapping(row["profile_json"], "Agent Profile authority")
        if (
            canonical_json_sha256(learner) != row["learner_sha256"]
            or canonical_json_sha256(profile) != row["profile_sha256"]
        ):
            raise _error(
                "INVARIANT_VIOLATION", "VALIDATE", "Learner/Profile authority hash drifted"
            )

    async def _activate_skill(self, claim: _ClaimedControlJob) -> None:
        async with self._database.transaction_with_commit_boundary() as connection:
            row = await self._lock_claim(connection, claim)
            body = claim.request_json
            scope = _mapping(body.get("activation_scope"), "Activation scope")
            cursor = await connection.execute(
                """
                SELECT c.certification_id,c.build_id,c.skill_id,c.skill_version_id,
                       c.artifact_sha256,c.actor_id,c.content_hash,c.certification_sha256,
                       c.record_json,c.issued_at,b.status AS build_status,
                       s.snapshot_json AS skill_json,lc.record_json AS legacy_certification_json,
                       lc.rejected,a.artifact_uri,a.metadata_json
                FROM yaya_skill_certifications c
                JOIN yaya_skill_builds b
                  ON b.tenant_id=c.tenant_id AND b.build_id=c.build_id
                 AND b.skill_id=c.skill_id AND b.actor_id=c.actor_id
                 AND b.content_hash=c.content_hash
                JOIN yaya_skills s
                  ON s.tenant_id=c.tenant_id AND s.skill_id=c.skill_id
                 AND s.skill_version_id=c.skill_version_id
                 AND s.certification_id=c.certification_id
                 AND s.artifact_sha256=c.artifact_sha256
                JOIN yaya_registry_certifications lc
                  ON lc.tenant_id=c.tenant_id AND lc.certification_id=c.certification_id
                 AND lc.skill_id=c.skill_id AND lc.skill_version_id=c.skill_version_id
                 AND lc.artifact_sha256=c.artifact_sha256
                JOIN yaya_artifacts a
                  ON a.tenant_id=c.tenant_id AND a.artifact_sha256=c.artifact_sha256
                 AND a.build_id=c.build_id AND a.skill_id=c.skill_id
                 AND a.actor_id=c.actor_id AND a.content_hash=c.content_hash
                LEFT JOIN yaya_certification_revocations r
                  ON r.tenant_id=c.tenant_id AND r.certification_id=c.certification_id
                WHERE c.tenant_id=%s AND c.actor_id=%s AND c.skill_version_id=%s
                  AND r.certification_id IS NULL
                FOR KEY SHARE OF c,b,s,lc,a
                """,
                (claim.tenant_id, claim.actor_id, claim.subject_id),
            )
            certification_row = await cursor.fetchone()
            if certification_row is None:
                raise _error("SKILL_NOT_CERTIFIED", "REGISTRY", "SkillVersion is not certified")
            certification_record = _mapping(
                certification_row["record_json"], "Certification record"
            )
            if (
                certification_row["build_status"] != "CERTIFIED"
                or certification_row["rejected"] is not False
                or canonical_json_sha256(certification_record)
                != certification_row["certification_sha256"]
                or certification_record.get("build_id") != certification_row["build_id"]
                or certification_record.get("skill_id") != certification_row["skill_id"]
                or certification_record.get("skill_version_id")
                != certification_row["skill_version_id"]
                or certification_record.get("artifact_sha256")
                != certification_row["artifact_sha256"]
            ):
                raise _error("INVARIANT_VIOLATION", "REGISTRY", "Certification closure drifted")
            validated = await self._application.validate_certification_closure(
                connection,
                claim.context.actor,
                content_hash=claim.content_hash,
                build_id=cast(str, certification_row["build_id"]),
                skill_id=cast(str, certification_row["skill_id"]),
                skill_version_id=claim.subject_id,
                certification_id=cast(str, certification_row["certification_id"]),
                artifact_sha256=cast(str, certification_row["artifact_sha256"]),
                build_payload=None,
                require_unrevoked=True,
            )
            authority = await self._application.resolve_launch_authority(
                connection,
                claim.context.actor,
                content=claim.context.content_ref,
                world_id=cast(str, scope["world_id"]),
                agent_profile_id=cast(str, scope["agent_profile_id"]),
            )
            if authority.authority_id != claim.authority_id:
                raise _error(
                    "INVARIANT_VIOLATION", "REGISTRY", "Activation launch authority changed"
                )
            certified = validated.certified
            skill = validated.skill
            if (
                certified.certification_id != certification_row["certification_id"]
                or certified.skill_version_id != claim.subject_id
                or certified.revoked_at is not None
                or skill.ref.skill_version_id != claim.subject_id
                or skill.ref.certification_id != certified.certification_id
                or skill.ref.artifact_sha256 != certified.artifact.artifact_sha256
            ):
                raise _error(
                    "SKILL_VERSION_MISMATCH", "REGISTRY", "SkillVersion closure mismatched"
                )
            skill_id = cast(str, certification_row["skill_id"])
            await connection.execute(
                """
                INSERT INTO yaya_registry_heads(
                    tenant_id,actor_id,content_hash,world_id,agent_profile_id,skill_id,revision
                ) VALUES (%s,%s,%s,%s,%s,%s,0)
                ON CONFLICT DO NOTHING
                """,
                (
                    claim.tenant_id,
                    claim.actor_id,
                    claim.content_hash,
                    authority.world_id,
                    authority.agent_profile_id,
                    skill_id,
                ),
            )
            head_cursor = await connection.execute(
                """
                SELECT revision FROM yaya_registry_heads
                WHERE tenant_id=%s AND actor_id=%s AND content_hash=%s
                  AND world_id=%s AND agent_profile_id=%s AND skill_id=%s
                FOR UPDATE
                """,
                (
                    claim.tenant_id,
                    claim.actor_id,
                    claim.content_hash,
                    authority.world_id,
                    authority.agent_profile_id,
                    skill_id,
                ),
            )
            head = await head_cursor.fetchone()
            if head is None:
                raise _error("INVARIANT_VIOLATION", "REGISTRY", "Registry head disappeared")
            previous_revision = cast(int, body["expected_registry_revision"])
            if head["revision"] != previous_revision:
                raise _error(
                    "CONTENT_VERSION_MISMATCH",
                    "REGISTRY",
                    "Registry expected revision is stale",
                )
            registry_revision = previous_revision + 1
            activated_at = datetime.now(UTC)
            active = ActiveSkill(
                skill=certified,
                registry_revision=registry_revision,
                activated_at=activated_at,
            )
            active_json = _mapping(plain(active), "ActiveSkill")
            entry_sha256 = canonical_json_sha256(active_json)
            await connection.execute(
                """
                INSERT INTO yaya_registry_entries(
                    tenant_id,actor_id,content_hash,world_id,agent_profile_id,skill_id,
                    revision,skill_version_id,certification_id,artifact_sha256,
                    previous_revision,entry_sha256,record_json,activated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    claim.tenant_id,
                    claim.actor_id,
                    claim.content_hash,
                    authority.world_id,
                    authority.agent_profile_id,
                    skill_id,
                    registry_revision,
                    claim.subject_id,
                    certified.certification_id,
                    certified.artifact.artifact_sha256,
                    previous_revision,
                    entry_sha256,
                    Jsonb(active_json),
                    activated_at,
                ),
            )
            advanced = await connection.execute(
                """
                UPDATE yaya_registry_heads SET revision=%s,updated_at=%s
                WHERE tenant_id=%s AND actor_id=%s AND content_hash=%s
                  AND world_id=%s AND agent_profile_id=%s AND skill_id=%s
                  AND revision=%s
                """,
                (
                    registry_revision,
                    activated_at,
                    claim.tenant_id,
                    claim.actor_id,
                    claim.content_hash,
                    authority.world_id,
                    authority.agent_profile_id,
                    skill_id,
                    previous_revision,
                ),
            )
            if advanced.rowcount != 1:
                raise _error("CONTENT_VERSION_MISMATCH", "REGISTRY", "Registry CAS was lost")
            resource: dict[str, object] = {
                "request_context": _context_wire(claim.context),
                "activation_id": claim.resource_id,
                "skill_id": skill_id,
                "skill_version_id": claim.subject_id,
                "certification_id": certified.certification_id,
                "artifact_sha256": certified.artifact.artifact_sha256,
                "activation_scope": {
                    "world_id": authority.world_id,
                    "agent_profile_id": authority.agent_profile_id,
                },
                "previous_registry_revision": previous_revision,
                "registry_revision": registry_revision,
                "activated_at": _iso(activated_at),
            }
            self._validator.validate("schemas/game/skill-activation.schema.json", resource)
            activation_sha256 = canonical_json_sha256(resource)
            await connection.execute(
                """
                INSERT INTO yaya_skill_activations(
                    tenant_id,activation_id,actor_id,content_hash,world_id,
                    agent_profile_id,skill_id,skill_version_id,certification_id,
                    artifact_sha256,previous_registry_revision,registry_revision,
                    activation_sha256,record_json,activated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    claim.tenant_id,
                    claim.resource_id,
                    claim.actor_id,
                    claim.content_hash,
                    authority.world_id,
                    authority.agent_profile_id,
                    skill_id,
                    claim.subject_id,
                    certified.certification_id,
                    certified.artifact.artifact_sha256,
                    previous_revision,
                    registry_revision,
                    activation_sha256,
                    Jsonb(resource),
                    activated_at,
                ),
            )
            await self._succeed_claim(
                connection,
                claim,
                row,
                resource_type="SKILL_ACTIVATION",
                resource_url=f"/v1/skill-activations/{claim.resource_id}",
            )

    async def _verify_artifact(self, digest: str) -> Path:
        if _SHA256.fullmatch(digest) is None:
            raise _error("INVARIANT_VIOLATION", "REGISTRY", "Artifact digest is invalid")
        candidates = (self._artifact_root / digest[:2] / digest, self._artifact_root / digest)
        existing = [candidate for candidate in candidates if candidate.exists()]
        if len(existing) != 1:
            raise _error("SKILL_VERSION_MISMATCH", "REGISTRY", "Artifact path is ambiguous")
        path = existing[0]
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self._artifact_root)
            stat_result = path.lstat()
            if path.is_symlink() or not path.is_file() or stat_result.st_mode & 0o222:
                raise ValueError("artifact is not an immutable regular file")
            actual = await asyncio.to_thread(lambda: hashlib.sha256(path.read_bytes()).hexdigest())
        except (OSError, ValueError) as error:
            raise _error("SKILL_VERSION_MISMATCH", "REGISTRY", "Artifact is unavailable") from error
        if resolved != path or actual != digest:
            raise _error("SKILL_VERSION_MISMATCH", "REGISTRY", "Artifact bytes drifted")
        return path

    @property
    def build_heartbeat_seconds(self) -> float:
        """Safe heartbeat cadence for a synchronous Docker build running in a thread."""

        return max(0.5, min(30.0, self._lease_seconds / 3))

    async def lock_build_claim(
        self,
        connection: AsyncConnection[dict[str, object]],
        claim: BuildJobClaim,
    ) -> dict[str, object]:
        """Re-lock and fence a Build claim inside its finalization transaction."""

        if claim.operation != "CREATE_SKILL_BUILD":
            raise _error("INVARIANT_VIOLATION", "VALIDATE", "Claim is not a Build job")
        return await self._lock_claim(connection, claim)

    async def complete_build_claim(
        self,
        connection: AsyncConnection[dict[str, object]],
        claim: BuildJobClaim,
        locked_row: Mapping[str, object],
        *,
        evidence_refs: tuple[EvidenceRef, ...] = (),
    ) -> None:
        """Atomically close the public Command and its fenced Build job."""

        await self._succeed_claim(
            connection,
            claim,
            locked_row,
            resource_type="SKILL_BUILD",
            resource_url=f"/v1/skill-builds/{claim.resource_id}",
            evidence_refs=evidence_refs,
        )

    async def _build_skill(self, claim: _ClaimedControlJob) -> None:
        if self._build_executor is None:
            raise _error(
                "DEPENDENCY_UNAVAILABLE",
                "SANDBOX",
                "Production Build executor is not configured",
            )
        execute = getattr(self._build_executor, "execute", None)
        if execute is None:
            raise _error(
                "INVARIANT_VIOLATION", "SANDBOX", "Build executor has no execute operation"
            )
        await execute(claim, self)

    async def _succeed_claim(
        self,
        connection: AsyncConnection[dict[str, object]],
        claim: _ClaimedControlJob,
        locked_row: Mapping[str, object],
        *,
        resource_type: Literal["AGENT_SESSION", "SKILL_BUILD", "SKILL_ACTIVATION"],
        resource_url: str,
        evidence_refs: tuple[EvidenceRef, ...] = (),
    ) -> None:
        command = decode_as(locked_row["command_json"], CommandRecord)
        now = datetime.now(UTC)
        result = cast(
            FrozenJsonObject,
            {
                "result_type": "RESOURCE_CREATED",
                "resource_type": resource_type,
                "resource_id": claim.resource_id,
                "resource_url": resource_url,
            },
        )
        terminal = replace(
            command,
            status=CommandStatus.APPLIED,
            stage="COMPLETE",
            terminal=True,
            result=result,
            error=None,
            evidence_refs=evidence_refs,
            revision=command.revision + 1,
            updated_at=now,
        )
        self._validator.validate("schemas/game/command.schema.json", _command_wire(terminal))
        command_update = await connection.execute(
            """
            UPDATE yaya_commands
            SET revision=%s,status='APPLIED',updated_at=%s,record_json=%s
            WHERE tenant_id=%s AND command_id=%s AND revision=%s
              AND status NOT IN ('APPLIED','REJECTED','FAILED','UNKNOWN','CANCELLED')
            """,
            (
                terminal.revision,
                terminal.updated_at,
                Jsonb(encode(terminal)),
                claim.tenant_id,
                claim.command_id,
                command.revision,
            ),
        )
        job_update = await connection.execute(
            """
            UPDATE yaya_control_jobs
            SET state='SUCCEEDED',phase='COMPLETE',worker_id=NULL,lease_id=NULL,
                claimed_at=NULL,heartbeat_at=NULL,lease_expires_at=NULL,
                result_json=%s,last_error_code=NULL,updated_at=%s
            WHERE tenant_id=%s AND job_id=%s AND state='LEASED'
              AND worker_id=%s AND lease_id=%s AND fencing_token=%s
            """,
            (
                Jsonb(result),
                now,
                claim.tenant_id,
                claim.job_id,
                claim.worker_id,
                claim.lease_id,
                claim.fencing_token,
            ),
        )
        if command_update.rowcount != 1 or job_update.rowcount != 1:
            raise _error("INVARIANT_VIOLATION", "COMPLETE", "Control finalization CAS was lost")

    async def _fail_claim(
        self,
        claim: _ClaimedControlJob,
        source: BackendApplicationError,
    ) -> None:
        try:
            async with self._database.transaction_with_commit_boundary() as connection:
                row = await self._lock_claim(connection, claim)
                command = decode_as(row["command_json"], CommandRecord)
                now = datetime.now(UTC)
                contract = _contract_error(source.code, source.stage, str(source))
                status = (
                    CommandStatus.FAILED if source.http_status >= 500 else CommandStatus.REJECTED
                )
                terminal = replace(
                    command,
                    status=status,
                    stage=contract.stage,
                    terminal=True,
                    result=None,
                    error=contract,
                    revision=command.revision + 1,
                    updated_at=now,
                )
                self._validator.validate(
                    "schemas/game/command.schema.json", _command_wire(terminal)
                )
                command_update = await connection.execute(
                    """
                    UPDATE yaya_commands
                    SET revision=%s,status=%s,updated_at=%s,record_json=%s
                    WHERE tenant_id=%s AND command_id=%s AND revision=%s
                      AND status NOT IN ('APPLIED','REJECTED','FAILED','UNKNOWN','CANCELLED')
                    """,
                    (
                        terminal.revision,
                        terminal.status.value,
                        terminal.updated_at,
                        Jsonb(encode(terminal)),
                        claim.tenant_id,
                        claim.command_id,
                        command.revision,
                    ),
                )
                job_update = await connection.execute(
                    """
                    UPDATE yaya_control_jobs
                    SET state='FAILED',phase=%s,worker_id=NULL,lease_id=NULL,
                        claimed_at=NULL,heartbeat_at=NULL,lease_expires_at=NULL,
                        result_json=NULL,last_error_code=%s,updated_at=%s
                    WHERE tenant_id=%s AND job_id=%s AND state='LEASED'
                      AND worker_id=%s AND lease_id=%s AND fencing_token=%s
                    """,
                    (
                        contract.stage,
                        contract.code,
                        now,
                        claim.tenant_id,
                        claim.job_id,
                        claim.worker_id,
                        claim.lease_id,
                        claim.fencing_token,
                    ),
                )
                if command_update.rowcount != 1 or job_update.rowcount != 1:
                    raise _error("INVARIANT_VIOLATION", "COMPLETE", "Control failure CAS was lost")
        except (PostgresCommitStateUnknown, BackendApplicationError, psycopg.Error):
            # A lost/expired fenced claim now belongs to a takeover worker.
            # Failure reporting must not terminate this worker loop or mutate
            # the successor's lease.
            return


__all__ = [
    "AcceptedControlJob",
    "BuildJobClaim",
    "StudentSkillChainApplication",
    "StudentSkillChainWorker",
]
