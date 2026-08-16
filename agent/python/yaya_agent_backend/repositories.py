"""Production PostgreSQL repositories for the Agent application boundary.

Every lookup is closed over the authenticated tenant, actor and pinned content
hash.  The runtime-facing repositories deliberately raise on missing or stale
authority, while the public ``WorldPort`` preserves the contracts ``Result``
surface.  Agent turn publication is a single PostgreSQL transaction and uses a
lease token as a fencing value.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString, cast

from jsonschema import FormatChecker
from jsonschema.validators import validator_for
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012, Schema
from yaya_agent_contracts import (
    CommandRecord,
    CommandStatus,
    ContentRef,
    ContractError,
    ErrorCategory,
    Failure,
    FrozenJsonObject,
    LearnerModelSnapshot,
    OperationContext,
    RequestContext,
    Result,
    RuntimeEvent,
    RuntimeEventType,
    SkillRef,
    Success,
    WorldSnapshot,
    canonical_json_sha256,
    learner_inference_sha256,
)
from yaya_agent_runtime.domain import (
    AgentDecision,
    AgentTraceEvent,
    AgentTurnClaimReceipt,
    AgentTurnCommitReceipt,
    CommittedAgentTurn,
    CompileResultSnapshot,
    CounterexampleSnapshot,
    GameEvent,
    LearnerProfileSnapshot,
    MessageSnapshot,
    RoleRoute,
    RunResultSnapshot,
    SessionSnapshot,
    SkillSnapshot,
    SkillVersionSummary,
    TaskSnapshot,
)
from yaya_agent_runtime.errors import AgentPersistenceError
from yaya_agent_runtime.learner_projection_policy import (
    LEARNER_PROJECTION_POLICY_VERSION,
)
from yaya_agent_runtime.pedagogy_policy import TeachingPhase

from .codec import (
    agent_turn_commit_sha256,
    decode,
    decode_as,
    encode,
    internal_record_sha256,
    plain,
)
from .database import PostgresCommitStateUnknown, PostgresDatabase
from .learner_model_integrity import validate_persisted_learner_snapshot

type _Connection = AsyncConnection[dict[str, object]]

_RUNTIME_EVENT_ID = re.compile(r"^evt_[A-Za-z0-9_-]{8,128}$")


class RepositoryNotFoundError(LookupError):
    """The requested scoped resource does not exist."""


class RepositoryAuthorityError(PermissionError):
    """A durable value does not match the authenticated authority."""


class AgentTurnLeaseConflict(RuntimeError):
    """Another worker owns a live Agent turn lease."""


class AgentTurnFenceError(RuntimeError):
    """A stale or expired claim attempted a fenced mutation."""


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


def _require_authority(stored: RequestContext, context: OperationContext) -> None:
    stored_actor = stored.actor
    current_actor = context.actor
    if (
        stored_actor.tenant_id,
        stored_actor.actor_id,
        stored_actor.actor_type,
    ) != (
        current_actor.tenant_id,
        current_actor.actor_id,
        current_actor.actor_type,
    ) or stored.content_ref != context.content_ref:
        raise RepositoryAuthorityError("persisted resource authority does not match context")


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a JSON object")
    mapping = cast(Mapping[object, object], value)
    if any(not isinstance(key, str) for key in mapping):
        raise TypeError(f"{field_name} must use string keys")
    return cast(Mapping[str, object], mapping)


def _plain_runtime_event(value: object, field_name: str) -> RuntimeEvent:
    wire = _mapping(value, field_name)
    content_ref = _mapping(wire.get("content_ref"), f"{field_name}.content_ref")
    payload = _mapping(wire.get("payload"), f"{field_name}.payload")
    occurred_at_wire = wire.get("occurred_at")
    if not isinstance(occurred_at_wire, str):
        raise TypeError(f"{field_name}.occurred_at must be a timestamp")
    occurred_at = datetime.fromisoformat(occurred_at_wire.replace("Z", "+00:00"))
    if occurred_at.tzinfo is None:
        raise ValueError(f"{field_name}.occurred_at must include an offset")
    event = RuntimeEvent(
        event_id=cast(str, wire.get("event_id")),
        event_type=RuntimeEventType(cast(str, wire.get("event_type"))),
        event_version=cast(int, wire.get("event_version")),
        stream_id=cast(str, wire.get("stream_id")),
        sequence=cast(int, wire.get("sequence")),
        occurred_at=occurred_at,
        producer=cast(str, wire.get("producer")),
        trace_id=cast(str, wire.get("trace_id")),
        command_id=cast(str, wire.get("command_id")),
        correlation_id=cast(str, wire.get("correlation_id")),
        causation_id=cast(str | None, wire.get("causation_id")),
        content_ref=ContentRef(
            unit_id=cast(str, content_ref.get("unit_id")),
            version=cast(str, content_ref.get("version")),
            content_hash=cast(str, content_ref.get("content_hash")),
        ),
        payload=payload,
        schema_version=cast(str, wire.get("schema_version")),
    )
    if plain(event) != dict(wire):
        raise ValueError(f"{field_name} is not a canonical plain RuntimeEvent")
    return event


def _event_sha256(event: GameEvent) -> str:
    return canonical_json_sha256(_mapping(plain(event), "event"))


def _identifier(prefix: str, seed: str) -> str:
    return f"{prefix}_{seed[:32]}"


def _scoped_identifier(prefix: str, *parts: str) -> str:
    framed = "".join(f"{len(part)}:{part}" for part in parts)
    return f"{prefix}_{hashlib.sha256(framed.encode('utf-8')).hexdigest()[:24]}"


def _contract_error(code: str, stage: str, message: str) -> ContractError:
    catalog: dict[str, tuple[ErrorCategory, bool, str]] = {
        "NOT_FOUND": (ErrorCategory.VALIDATION, False, "resource.not_found"),
        "AUTHORIZATION_DENIED": (
            ErrorCategory.AUTHORIZATION,
            False,
            "auth.permission_denied",
        ),
        "INVARIANT_VIOLATION": (
            ErrorCategory.INVARIANT,
            False,
            "system.invariant_violation",
        ),
        "DEPENDENCY_UNAVAILABLE": (
            ErrorCategory.DEPENDENCY,
            True,
            "dependency.temporarily_unavailable",
        ),
    }
    category, retryable, message_key = catalog[code]
    return ContractError(
        code=code,
        category=category,
        retryable=retryable,
        user_message_key=message_key,
        stage=stage,
        message=message,
    )


async def _fetch_one(
    database: PostgresDatabase,
    query: str,
    parameters: tuple[object, ...],
) -> dict[str, object] | None:
    connection = await database.connect(autocommit=True)
    try:
        result = await connection.execute(cast(LiteralString, query), parameters)
        return await result.fetchone()
    finally:
        await connection.close()


async def _fetch_all(
    database: PostgresDatabase,
    query: str,
    parameters: tuple[object, ...],
) -> list[dict[str, object]]:
    connection = await database.connect(autocommit=True)
    try:
        result = await connection.execute(cast(LiteralString, query), parameters)
        return list(await result.fetchall())
    finally:
        await connection.close()


def _scoped_snapshot[T](
    row: dict[str, object] | None,
    expected: type[T],
    context: OperationContext,
) -> T:
    if row is None:
        raise RepositoryNotFoundError(f"{expected.__name__} was not found")
    snapshot = decode_as(row["snapshot_json"], expected)
    request_context = getattr(snapshot, "request_context", None)
    if not isinstance(request_context, RequestContext):
        raise RepositoryAuthorityError("persisted snapshot has no request authority")
    _require_authority(request_context, context)
    return snapshot


class PostgresTaskRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    async def get_task(self, task_id: str, context: OperationContext) -> TaskSnapshot:
        row = await _fetch_one(
            self._database,
            """
            SELECT snapshot_json FROM yaya_tasks
            WHERE tenant_id=%s AND task_id=%s AND actor_id=%s AND content_hash=%s
            """,
            (
                context.actor.tenant_id,
                task_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
            ),
        )
        return _scoped_snapshot(row, TaskSnapshot, context)


class PostgresSessionRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    async def get_session(self, session_id: str, context: OperationContext) -> SessionSnapshot:
        row = await _fetch_one(
            self._database,
            """
            SELECT snapshot_json FROM yaya_agent_sessions
            WHERE tenant_id=%s AND session_id=%s AND actor_id=%s AND content_hash=%s
            """,
            (
                context.actor.tenant_id,
                session_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
            ),
        )
        return _scoped_snapshot(row, SessionSnapshot, context)


class PostgresSkillRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    async def get_bound_skill(
        self,
        skill_ref: SkillRef,
        context: OperationContext,
    ) -> SkillSnapshot:
        scope = await _fetch_one(
            self._database,
            """
            SELECT c.session_id,c.record_json AS command_json,
                   s.actor_id AS session_actor_id,
                   s.content_hash AS session_content_hash,
                   s.task_id AS session_task_id,s.world_id AS session_world_id,
                   p.session_id AS public_session_id,
                   p.actor_id AS public_actor_id,
                   p.content_hash AS public_content_hash,
                   p.task_id AS public_task_id,p.world_id AS public_world_id,
                   p.status AS public_status,
                   EXISTS (
                     SELECT 1 FROM yaya_session_skill_versions b
                     WHERE b.tenant_id=c.tenant_id AND b.session_id=c.session_id
                   ) AS has_public_binding
            FROM yaya_commands c
            LEFT JOIN yaya_agent_sessions s
              ON s.tenant_id=c.tenant_id AND s.session_id=c.session_id
            LEFT JOIN yaya_public_agent_sessions p
              ON p.tenant_id=c.tenant_id AND p.session_id=c.session_id
            WHERE c.tenant_id=%s AND c.command_id=%s
              AND c.actor_id=%s AND c.content_hash=%s
            """,
            (
                context.actor.tenant_id,
                context.command_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
            ),
        )
        if scope is None:
            raise RepositoryAuthorityError("Command did not resolve one canonical Session")
        command = decode_as(scope["command_json"], CommandRecord)
        _require_authority(command.request_context, context)
        if (
            scope["session_id"] is None
            or scope["session_actor_id"] != context.actor.actor_id
            or scope["session_content_hash"] != context.content_ref.content_hash
        ):
            raise RepositoryAuthorityError("Command Session authority drifted")

        public_scope = scope["public_session_id"] is not None
        if scope["has_public_binding"] and not public_scope:
            raise RepositoryAuthorityError("public Session extension disappeared")
        if public_scope:
            if (
                scope["public_actor_id"] != context.actor.actor_id
                or scope["public_content_hash"] != context.content_ref.content_hash
                or scope["public_task_id"] != scope["session_task_id"]
                or scope["public_world_id"] != scope["session_world_id"]
                or command.versions.skill_version != skill_ref.skill_version_id
                or command.versions.artifact_sha256 != skill_ref.artifact_sha256
            ):
                raise RepositoryAuthorityError("public Session Skill authority drifted")
            row = await _fetch_one(
                self._database,
                """
                SELECT s.snapshot_json,b.binding_id,
                       b.session_id AS binding_session_id,
                       b.skill_id AS binding_skill_id,
                       b.skill_version_id AS binding_skill_version_id,
                       b.certification_id AS binding_certification_id,
                       b.artifact_sha256 AS binding_artifact_sha256,
                       b.actor_id AS binding_actor_id,
                       b.content_hash AS binding_content_hash,b.binding_sha256
                FROM yaya_session_skill_versions b
                LEFT JOIN yaya_skills s
                  ON s.tenant_id=b.tenant_id AND s.skill_id=b.skill_id
                 AND s.skill_version_id=b.skill_version_id
                 AND s.certification_id=b.certification_id
                 AND s.artifact_sha256=b.artifact_sha256
                 AND s.actor_id=b.actor_id AND s.content_hash=b.content_hash
                WHERE b.tenant_id=%s AND b.session_id=%s
                  AND b.skill_id=%s AND b.skill_version_id=%s
                  AND b.certification_id=%s AND b.artifact_sha256=%s
                """,
                (
                    context.actor.tenant_id,
                    scope["session_id"],
                    skill_ref.skill_id,
                    skill_ref.skill_version_id,
                    skill_ref.certification_id,
                    skill_ref.artifact_sha256,
                ),
            )
            if row is None or row["snapshot_json"] is None:
                raise RepositoryAuthorityError("public Session SkillVersion binding is missing")
            expected_binding_id = _scoped_identifier(
                "binding",
                context.actor.tenant_id,
                cast(str, scope["session_id"]),
                skill_ref.skill_id,
                skill_ref.skill_version_id,
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
                or row["binding_session_id"] != scope["session_id"]
                or row["binding_skill_id"] != skill_ref.skill_id
                or row["binding_skill_version_id"] != skill_ref.skill_version_id
                or row["binding_certification_id"] != skill_ref.certification_id
                or row["binding_artifact_sha256"] != skill_ref.artifact_sha256
                or row["binding_actor_id"] != context.actor.actor_id
                or row["binding_content_hash"] != context.content_ref.content_hash
                or row["binding_sha256"] != canonical_json_sha256(binding_projection)
            ):
                raise RepositoryAuthorityError("public Session SkillVersion binding drifted")
        else:
            row = await _fetch_one(
                self._database,
                """
                SELECT snapshot_json FROM yaya_skills
                WHERE tenant_id=%s AND skill_id=%s AND skill_version_id=%s
                  AND certification_id=%s AND artifact_sha256=%s
                  AND actor_id=%s AND content_hash=%s AND session_id=%s
                """,
                (
                    context.actor.tenant_id,
                    skill_ref.skill_id,
                    skill_ref.skill_version_id,
                    skill_ref.certification_id,
                    skill_ref.artifact_sha256,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    scope["session_id"],
                ),
            )
        snapshot = _scoped_snapshot(row, SkillSnapshot, context)
        if snapshot.ref != skill_ref:
            raise RepositoryAuthorityError("certified Skill binding drifted in storage")
        return snapshot

    async def list_active_skills(
        self,
        student_id: str,
        context: OperationContext,
    ) -> tuple[SkillSnapshot, ...]:
        if student_id != context.actor.actor_id:
            raise RepositoryAuthorityError("student does not match authenticated actor")
        public_scopes = await _fetch_all(
            self._database,
            """
            SELECT p.session_id AS public_session_id,
                   p.actor_id AS public_actor_id,
                   p.content_hash AS public_content_hash,
                   p.world_id,p.agent_profile_id,p.task_id,p.status,
                   s.world_id AS session_world_id,s.task_id AS session_task_id,
                   EXISTS (
                     SELECT 1 FROM yaya_session_skill_versions b
                     WHERE b.tenant_id=s.tenant_id AND b.session_id=s.session_id
                   ) AS has_public_binding
            FROM yaya_commands c
            LEFT JOIN yaya_agent_sessions s
              ON s.tenant_id=c.tenant_id AND s.session_id=c.session_id
             AND s.actor_id=c.actor_id AND s.content_hash=c.content_hash
            LEFT JOIN yaya_public_agent_sessions p
              ON p.tenant_id=c.tenant_id AND p.session_id=c.session_id
            WHERE c.tenant_id=%s AND c.command_id=%s AND c.actor_id=%s
              AND c.content_hash=%s
            """,
            (
                context.actor.tenant_id,
                context.command_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
            ),
        )
        if len(public_scopes) > 1:
            raise RepositoryAuthorityError("Command resolved multiple public Session scopes")
        scope = public_scopes[0] if public_scopes else None
        public_scope = scope is not None and scope["public_session_id"] is not None
        if scope is not None and scope["has_public_binding"] and not public_scope:
            raise RepositoryAuthorityError("public Session extension disappeared")
        if public_scope:
            assert scope is not None
            if (
                scope["public_actor_id"] != context.actor.actor_id
                or scope["public_content_hash"] != context.content_ref.content_hash
                or scope["world_id"] != scope["session_world_id"]
                or scope["task_id"] != scope["session_task_id"]
                or scope["status"] != "ACTIVE"
            ):
                raise RepositoryAuthorityError(
                    "public Session scope drifted from the canonical Session"
                )
            active_rows = await _fetch_all(
                self._database,
                """
                SELECT e.record_json AS active_json,e.entry_sha256,
                       e.revision AS active_revision,
                       e.activated_at AS active_activated_at,
                       e.skill_id,e.skill_version_id,e.certification_id,e.artifact_sha256
                FROM yaya_registry_heads h
                JOIN yaya_registry_entries e
                  ON e.tenant_id=h.tenant_id AND e.actor_id=h.actor_id
                 AND e.content_hash=h.content_hash AND e.world_id=h.world_id
                 AND e.agent_profile_id=h.agent_profile_id AND e.skill_id=h.skill_id
                 AND e.revision=h.revision
                WHERE h.tenant_id=%s AND h.actor_id=%s AND h.content_hash=%s
                  AND h.world_id=%s AND h.agent_profile_id=%s
                  AND NOT EXISTS (
                    SELECT 1 FROM yaya_certification_revocations r
                    WHERE r.tenant_id=e.tenant_id
                      AND r.certification_id=e.certification_id
                  )
                ORDER BY h.skill_id
                """,
                (
                    context.actor.tenant_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    scope["world_id"],
                    scope["agent_profile_id"],
                ),
            )
            for active_row in active_rows:
                active_json = _mapping(active_row["active_json"], "active Registry entry")
                if canonical_json_sha256(active_json) != active_row["entry_sha256"]:
                    raise RepositoryAuthorityError("active Registry entry hash drifted")
        else:
            # Explicit legacy path for A6 fixtures without a public Session
            # extension.  Public Sessions never fall back to actor-only scope.
            active_rows = await _fetch_all(
                self._database,
                """
                SELECT record_json AS active_json FROM yaya_registry_active
                WHERE tenant_id=%s AND actor_id=%s ORDER BY skill_id
                """,
                (
                    context.actor.tenant_id,
                    context.actor.actor_id,
                ),
            )
        from yaya_agent_contracts import ActiveSkill, CertifiedSkill

        snapshots: list[SkillSnapshot] = []
        for active_row in active_rows:
            legacy_certified: CertifiedSkill | None = None
            if public_scope:
                skill_id = active_row["skill_id"]
                skill_version_id = active_row["skill_version_id"]
                certification_id = active_row["certification_id"]
                artifact_sha256 = active_row["artifact_sha256"]
            else:
                active = decode_as(active_row["active_json"], ActiveSkill)
                legacy_certified = active.skill
                skill_id = legacy_certified.skill_id
                skill_version_id = legacy_certified.skill_version_id
                certification_id = legacy_certified.certification_id
                artifact_sha256 = legacy_certified.artifact.artifact_sha256
            row = await _fetch_one(
                self._database,
                """
                SELECT s.snapshot_json, c.record_json AS certification_json
                FROM yaya_skills s
                JOIN yaya_registry_certifications c
                  ON c.tenant_id=s.tenant_id
                 AND c.certification_id=s.certification_id
                 AND c.skill_id=s.skill_id
                 AND c.skill_version_id=s.skill_version_id
                 AND c.artifact_sha256=s.artifact_sha256
                 AND c.rejected=FALSE
                WHERE s.tenant_id=%s AND s.actor_id=%s AND s.content_hash=%s
                  AND s.skill_id=%s AND s.skill_version_id=%s
                  AND s.certification_id=%s AND s.artifact_sha256=%s
                """,
                (
                    context.actor.tenant_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    skill_id,
                    skill_version_id,
                    certification_id,
                    artifact_sha256,
                ),
            )
            if row is None:
                raise RepositoryAuthorityError("active Registry has no closed Skill binding")
            snapshot = _scoped_snapshot(row, SkillSnapshot, context)
            stored_certified = decode_as(row["certification_json"], CertifiedSkill)
            if public_scope:
                active_revision = active_row["active_revision"]
                active_activated_at = active_row["active_activated_at"]
                if (
                    isinstance(active_revision, bool)
                    or not isinstance(active_revision, int)
                    or not isinstance(active_activated_at, datetime)
                    or active_activated_at.tzinfo is None
                    or active_activated_at.utcoffset() is None
                ):
                    raise RepositoryAuthorityError("active Registry authority is invalid")
                try:
                    active = ActiveSkill(
                        skill=stored_certified,
                        registry_revision=active_revision,
                        activated_at=active_activated_at.astimezone(UTC),
                    )
                except (TypeError, ValueError) as error:
                    raise RepositoryAuthorityError(
                        "active Registry authority is invalid"
                    ) from error
                active_json = _mapping(active_row["active_json"], "active Registry entry")
                expected_active_json = _mapping(plain(active), "expected active Registry entry")
                if active_json != expected_active_json or (
                    active_row["skill_id"],
                    active_row["skill_version_id"],
                    active_row["certification_id"],
                    active_row["artifact_sha256"],
                ) != (
                    stored_certified.skill_id,
                    stored_certified.skill_version_id,
                    stored_certified.certification_id,
                    stored_certified.artifact.artifact_sha256,
                ):
                    raise RepositoryAuthorityError("active Registry projection drifted")
            if legacy_certified is not None and legacy_certified != stored_certified:
                raise RepositoryAuthorityError("active Registry and Skill binding drifted")
            if (
                snapshot.ref.skill_id,
                snapshot.ref.skill_version_id,
                snapshot.ref.certification_id,
                snapshot.ref.artifact_sha256,
            ) != (
                stored_certified.skill_id,
                stored_certified.skill_version_id,
                stored_certified.certification_id,
                stored_certified.artifact.artifact_sha256,
            ):
                raise RepositoryAuthorityError("active Registry and Skill binding drifted")
            snapshots.append(snapshot)
        return tuple(snapshots)

    async def list_skill_history(
        self,
        skill_id: str,
        session_id: str,
        context: OperationContext,
    ) -> tuple[SkillVersionSummary, ...]:
        public_rows = await _fetch_all(
            self._database,
            """
            SELECT p.actor_id AS public_actor_id,
                   p.content_hash AS public_content_hash,
                   p.task_id AS public_task_id,p.world_id AS public_world_id,
                   s.actor_id AS session_actor_id,
                   s.content_hash AS session_content_hash,
                   s.task_id AS session_task_id,s.world_id AS session_world_id
            FROM yaya_public_agent_sessions p
            LEFT JOIN yaya_agent_sessions s
              ON s.tenant_id=p.tenant_id AND s.session_id=p.session_id
            WHERE p.tenant_id=%s AND p.session_id=%s
            """,
            (
                context.actor.tenant_id,
                session_id,
            ),
        )
        if len(public_rows) > 1:
            raise RepositoryAuthorityError("Session resolved multiple public authorities")
        if public_rows:
            public_row = public_rows[0]
            if (
                public_row["public_actor_id"] != context.actor.actor_id
                or public_row["public_content_hash"] != context.content_ref.content_hash
                or public_row["session_actor_id"] != context.actor.actor_id
                or public_row["session_content_hash"] != context.content_ref.content_hash
                or public_row["public_task_id"] != public_row["session_task_id"]
                or public_row["public_world_id"] != public_row["session_world_id"]
            ):
                raise RepositoryAuthorityError(
                    "public Session scope drifted from the canonical Session"
                )
        else:
            public_bindings = await _fetch_all(
                self._database,
                """
                SELECT 1 FROM yaya_session_skill_versions
                WHERE tenant_id=%s AND session_id=%s LIMIT 1
                """,
                (context.actor.tenant_id, session_id),
            )
            if public_bindings:
                raise RepositoryAuthorityError("public Session extension disappeared")
        if public_rows:
            rows = await _fetch_all(
                self._database,
                """
                SELECT s.snapshot_json,
                       s.skill_version_id AS snapshot_skill_version_id,
                       b.binding_id,b.session_id AS binding_session_id,
                       b.skill_id AS binding_skill_id,
                       b.skill_version_id AS binding_skill_version_id,
                       b.certification_id AS binding_certification_id,
                       b.artifact_sha256 AS binding_artifact_sha256,
                       b.actor_id AS binding_actor_id,
                       b.content_hash AS binding_content_hash,
                       b.binding_sha256
                FROM yaya_session_skill_versions b
                LEFT JOIN yaya_skills s
                  ON s.tenant_id=b.tenant_id AND s.skill_id=b.skill_id
                 AND s.skill_version_id=b.skill_version_id
                 AND s.certification_id=b.certification_id
                 AND s.artifact_sha256=b.artifact_sha256
                 AND s.actor_id=b.actor_id AND s.content_hash=b.content_hash
                WHERE b.tenant_id=%s AND b.skill_id=%s AND b.session_id=%s
                ORDER BY b.bound_at,b.skill_version_id
                """,
                (
                    context.actor.tenant_id,
                    skill_id,
                    session_id,
                ),
            )
        else:
            rows = await _fetch_all(
                self._database,
                """
                SELECT snapshot_json, skill_version_id FROM yaya_skills
                WHERE tenant_id=%s AND actor_id=%s AND content_hash=%s
                  AND skill_id=%s AND session_id=%s
                ORDER BY created_at, skill_version_id
                """,
                (
                    context.actor.tenant_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    skill_id,
                    session_id,
                ),
            )
        summaries: list[SkillVersionSummary] = []
        for row in rows:
            if public_rows:
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
                    row["snapshot_json"] is None
                    or row["binding_session_id"] != session_id
                    or row["binding_skill_id"] != skill_id
                    or row["binding_actor_id"] != context.actor.actor_id
                    or row["binding_content_hash"] != context.content_ref.content_hash
                    or row["binding_sha256"] != canonical_json_sha256(binding_projection)
                ):
                    raise RepositoryAuthorityError("Session SkillVersion binding drifted")
            value = decode(row["snapshot_json"])
            if public_rows and (
                not isinstance(value, SkillSnapshot)
                or value.ref.skill_id != row["binding_skill_id"]
                or value.ref.skill_version_id != row["binding_skill_version_id"]
                or value.ref.certification_id != row["binding_certification_id"]
                or value.ref.artifact_sha256 != row["binding_artifact_sha256"]
                or row["snapshot_skill_version_id"] != row["binding_skill_version_id"]
            ):
                raise RepositoryAuthorityError("Session SkillVersion snapshot drifted")
            if isinstance(value, SkillVersionSummary):
                _require_authority(value.request_context, context)
                summaries.append(value)
                continue
            if not isinstance(value, SkillSnapshot):
                raise TypeError("persisted Skill history has an unsupported record type")
            _require_authority(value.request_context, context)
            summaries.append(
                SkillVersionSummary(
                    session_id=session_id,
                    skill_id=value.ref.skill_id,
                    skill_version_id=value.ref.skill_version_id,
                    source_sha256=value.source_sha256,
                    change_summary=f"Certified Skill version {value.ref.skill_version_id}",
                    request_context=value.request_context,
                )
            )
        return tuple(summaries)


class PostgresRunRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    async def get_compile_result(
        self,
        build_id: str,
        context: OperationContext,
    ) -> CompileResultSnapshot:
        row = await _fetch_one(
            self._database,
            """
            SELECT snapshot_json FROM yaya_compile_results
            WHERE tenant_id=%s AND build_id=%s AND actor_id=%s AND content_hash=%s
            """,
            (
                context.actor.tenant_id,
                build_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
            ),
        )
        return _scoped_snapshot(row, CompileResultSnapshot, context)

    async def get_run(self, run_id: str, context: OperationContext) -> RunResultSnapshot:
        row = await _fetch_one(
            self._database,
            """
            SELECT snapshot_json FROM yaya_runs
            WHERE tenant_id=%s AND run_id=%s AND actor_id=%s AND content_hash=%s
            """,
            (
                context.actor.tenant_id,
                run_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
            ),
        )
        return _scoped_snapshot(row, RunResultSnapshot, context)

    async def list_same_failure_runs(
        self,
        session_id: str,
        failure_key: str,
        through_run_id: str,
        limit: int,
        context: OperationContext,
    ) -> tuple[RunResultSnapshot, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        rows = await _fetch_all(
            self._database,
            """
            WITH boundary AS (
              SELECT c.client_turn_sequence
              FROM yaya_runs r
              JOIN yaya_commands c
                ON c.tenant_id=r.tenant_id AND c.command_id=r.command_id
               AND c.actor_id=r.actor_id AND c.content_hash=r.content_hash
               AND c.session_id=r.session_id AND c.turn_id=r.turn_id
              WHERE r.tenant_id=%s AND r.run_id=%s AND r.session_id=%s
                AND r.failure_key=%s AND r.actor_id=%s AND r.content_hash=%s
            )
            SELECT r.snapshot_json,c.client_turn_sequence
            FROM yaya_commands c
            CROSS JOIN boundary b
            LEFT JOIN yaya_runs r
              ON r.tenant_id=c.tenant_id AND r.command_id=c.command_id
             AND r.actor_id=c.actor_id AND r.content_hash=c.content_hash
             AND r.session_id=c.session_id AND r.turn_id=c.turn_id
            WHERE c.tenant_id=%s AND c.session_id=%s
              AND c.actor_id=%s AND c.content_hash=%s
              AND c.client_turn_sequence<=b.client_turn_sequence
            ORDER BY c.client_turn_sequence DESC,c.command_id DESC LIMIT %s
            """,
            (
                context.actor.tenant_id,
                through_run_id,
                session_id,
                failure_key,
                context.actor.actor_id,
                context.content_ref.content_hash,
                context.actor.tenant_id,
                session_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
                limit,
            ),
        )
        if not rows:
            raise RepositoryNotFoundError("through_run_id is outside the scoped failure history")
        values: list[RunResultSnapshot] = []
        for row in rows:
            if row["snapshot_json"] is None:
                break
            run = _scoped_snapshot(row, RunResultSnapshot, context)
            if run.task_success or run.failure_key != failure_key:
                break
            values.append(run)
        if not values or values[0].run_id != through_run_id:
            raise RepositoryNotFoundError("through_run_id is not the final scoped history item")
        values.reverse()
        return tuple(values)

    async def list_session_runs(
        self,
        session_id: str,
        through_run_id: str,
        context: OperationContext,
    ) -> tuple[RunResultSnapshot, ...]:
        rows = await _fetch_all(
            self._database,
            """
            WITH boundary AS (
              SELECT created_at, run_id FROM yaya_runs
              WHERE tenant_id=%s AND run_id=%s AND session_id=%s
                AND actor_id=%s AND content_hash=%s
            )
            SELECT r.snapshot_json FROM yaya_runs r, boundary b
            WHERE r.tenant_id=%s AND r.session_id=%s
              AND r.actor_id=%s AND r.content_hash=%s
              AND (r.created_at, r.run_id) <= (b.created_at, b.run_id)
            ORDER BY r.created_at, r.run_id
            """,
            (
                context.actor.tenant_id,
                through_run_id,
                session_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
                context.actor.tenant_id,
                session_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
            ),
        )
        if not rows:
            raise RepositoryNotFoundError("through_run_id is outside the scoped session history")
        values = tuple(_scoped_snapshot(row, RunResultSnapshot, context) for row in rows)
        if values[-1].run_id != through_run_id:
            raise RepositoryNotFoundError("through_run_id is not the final scoped history item")
        return values


class PostgresCounterexampleRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    async def list_counterexamples(
        self,
        task_id: str,
        failure_key: str,
        context: OperationContext,
    ) -> tuple[CounterexampleSnapshot, ...]:
        rows = await _fetch_all(
            self._database,
            """
            SELECT snapshot_json FROM yaya_counterexamples
            WHERE tenant_id=%s AND task_id=%s AND failure_key=%s
              AND actor_id=%s AND content_hash=%s ORDER BY case_id
            """,
            (
                context.actor.tenant_id,
                task_id,
                failure_key,
                context.actor.actor_id,
                context.content_ref.content_hash,
            ),
        )
        return tuple(_scoped_snapshot(row, CounterexampleSnapshot, context) for row in rows)


class PostgresLearnerRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    async def get_profile(
        self,
        student_id: str,
        knowledge_points: tuple[str, ...],
        context: OperationContext,
    ) -> LearnerProfileSnapshot:
        if student_id != context.actor.actor_id:
            raise RepositoryAuthorityError("learner does not match authenticated actor")
        row = await _fetch_one(
            self._database,
            """
            SELECT revision,projected_through_sequence,snapshot_json,
                   snapshot_sha256,request_context_json,
                   projection_policy_version,updated_at
            FROM yaya_learner_models
            WHERE tenant_id=%s AND learner_id=%s AND actor_id=%s AND content_hash=%s
            """,
            (
                context.actor.tenant_id,
                student_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
            ),
        )
        if row is None:
            return LearnerProfileSnapshot(
                student_id=student_id,
                revision=0,
                competencies={},
                request_context=_request_context(context),
                evidence_refs=(),
            )
        stored_context_json = row["request_context_json"]
        policy_version = row["projection_policy_version"]
        snapshot_sha256 = row["snapshot_sha256"]
        if stored_context_json is None or policy_version is None or snapshot_sha256 is None:
            raise RepositoryAuthorityError(
                "Learner model is missing durable projection authority or policy provenance"
            )
        stored_context = decode_as(stored_context_json, RequestContext)
        _require_authority(stored_context, context)
        if policy_version != LEARNER_PROJECTION_POLICY_VERSION:
            raise RepositoryAuthorityError("Learner model projection policy version is unsupported")
        try:
            value = decode(row["snapshot_json"])
            if isinstance(value, LearnerProfileSnapshot):
                raise ValueError("legacy learner profile requires deterministic rebuild")
            if not isinstance(value, LearnerModelSnapshot):
                raise TypeError("persisted learner record has an unsupported type")
            parsed_competencies, normalized_competencies = validate_persisted_learner_snapshot(
                value,
                learner_id=student_id,
                revision=row["revision"],
                projected_through_sequence=row["projected_through_sequence"],
                model_version=LEARNER_PROJECTION_POLICY_VERSION,
                snapshot_sha256=snapshot_sha256,
                updated_at=row["updated_at"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RepositoryAuthorityError("Learner model integrity verification failed") from error
        competencies = {
            key: normalized_competencies[key]
            for key in knowledge_points
            if key in normalized_competencies
        }
        selected_evidence_ids = {
            evidence_id
            for key in competencies
            for evidence_id in parsed_competencies[key].evidence_ids
        }
        return LearnerProfileSnapshot(
            student_id=value.learner_id,
            revision=value.revision,
            competencies=competencies,
            request_context=stored_context,
            evidence_refs=tuple(
                evidence
                for evidence in value.evidence_refs
                if evidence.evidence_id in selected_evidence_ids
            ),
        )


class PostgresMessageRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    async def list_recent(
        self,
        session_id: str,
        limit: int,
        context: OperationContext,
    ) -> tuple[MessageSnapshot, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        rows = await _fetch_all(
            self._database,
            """
            SELECT snapshot_json FROM (
              SELECT snapshot_json, created_at, message_id FROM yaya_agent_messages
              WHERE tenant_id=%s AND session_id=%s AND actor_id=%s AND content_hash=%s
              ORDER BY created_at DESC, message_id DESC LIMIT %s
            ) recent ORDER BY created_at, message_id
            """,
            (
                context.actor.tenant_id,
                session_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
                limit,
            ),
        )
        return tuple(_scoped_snapshot(row, MessageSnapshot, context) for row in rows)


class PostgresWorldRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    async def get_snapshot(
        self,
        world_id: str,
        context: OperationContext,
    ) -> Result[WorldSnapshot]:
        try:
            row = await _fetch_one(
                self._database,
                """
                SELECT revision, last_event_sequence, state_hash, world_rules_version,
                       state_json, request_context_json, updated_at
                FROM yaya_worlds
                WHERE tenant_id=%s AND world_id=%s AND actor_id=%s AND content_hash=%s
                """,
                (
                    context.actor.tenant_id,
                    world_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                ),
            )
            if row is None:
                return Failure(_contract_error("NOT_FOUND", "WORLD_VALIDATE", "World not found"))
            stored_context = decode_as(row["request_context_json"], RequestContext)
            _require_authority(stored_context, context)
            state = cast(FrozenJsonObject, _mapping(row["state_json"], "state_json"))
            if canonical_json_sha256(state) != row["state_hash"]:
                return Failure(
                    _contract_error(
                        "INVARIANT_VIOLATION",
                        "WORLD_VALIDATE",
                        "World state hash does not match persisted state",
                    )
                )
            return Success(
                WorldSnapshot(
                    request_context=stored_context,
                    world_id=world_id,
                    revision=cast(int, row["revision"]),
                    last_event_sequence=cast(int, row["last_event_sequence"]),
                    state_hash=cast(str, row["state_hash"]),
                    generated_at=cast(datetime, row["updated_at"]),
                    world_rules_version=cast(str, row["world_rules_version"]),
                    state=state,
                )
            )
        except RepositoryAuthorityError:
            return Failure(
                _contract_error(
                    "AUTHORIZATION_DENIED", "WORLD_VALIDATE", "World authority mismatch"
                )
            )
        except Exception as error:
            return Failure(
                _contract_error(
                    "DEPENDENCY_UNAVAILABLE", "WORLD_VALIDATE", f"World read failed: {error}"
                )
            )


class PostgresAgentTraceRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    async def record(self, event: AgentTraceEvent, context: OperationContext) -> None:
        async with self._database.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO yaya_agent_traces(
                    tenant_id, actor_id, content_hash, turn_id, trace_json
                ) VALUES (%s,%s,%s,%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    event.turn_id,
                    Jsonb(encode(event)),
                ),
            )


class _ProjectionValidator:
    def __init__(self, contracts_root: Path) -> None:
        schema_root = contracts_root / "schemas"
        if not schema_root.is_dir():
            raise RuntimeError(f"contract schema directory is unavailable: {schema_root}")
        registry: Registry[Schema] = Registry()
        schemas: dict[Path, Schema] = {}
        for path in sorted(schema_root.rglob("*.schema.json")):
            document = cast(
                Schema,
                _mapping(json.loads(path.read_text(encoding="utf-8")), str(path)),
            )
            schemas[path.resolve()] = document
            resource = Resource(contents=document, specification=DRAFT202012)
            registry = registry.with_resource(path.resolve().as_uri(), resource)
            schema_id = document.get("$id") if isinstance(document, Mapping) else None
            if isinstance(schema_id, str):
                registry = registry.with_resource(schema_id, resource)
        self._schemas = schemas
        self._registry = registry

    def validate(self, relative_path: str, value: Mapping[str, object]) -> None:
        path = next(iter(self._schemas)).parents[2] / relative_path
        schema = self._schemas.get(path.resolve())
        if schema is None:
            raise RuntimeError(f"required contract schema is unavailable: {relative_path}")
        schema_object = cast(dict[str, Any], schema)
        validator_type = validator_for(schema_object)
        validator_type.check_schema(schema_object)
        validator = validator_type(
            schema_object,
            registry=self._registry,
            format_checker=FormatChecker(),
        )
        errors = sorted(
            validator.iter_errors(cast(Any, value)),
            key=lambda item: list(item.absolute_path),
        )
        if errors:
            paths = ["/".join(str(part) for part in error.absolute_path) for error in errors]
            raise ValueError(
                f"projection violates {relative_path}: {list(zip(paths, errors, strict=True))}"
            )


def _evidence_wire(decision: AgentDecision) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    for evidence in decision.evidence_refs:
        item: dict[str, object] = {
            "evidence_id": evidence.evidence_id,
            "evidence_type": evidence.evidence_type.value,
            "created_at": plain(evidence.created_at),
        }
        if evidence.sha256 is not None:
            item["sha256"] = evidence.sha256
        if evidence.uri is not None:
            item["uri"] = evidence.uri
        values.append(item)
    return values


class PostgresAgentTurnRepository:
    def __init__(
        self,
        database: PostgresDatabase,
        contracts_root: Path,
        *,
        claim_ttl_ms: int = 30_000,
        internalize_root_execution: bool = False,
    ) -> None:
        if claim_ttl_ms < 1:
            raise ValueError("claim_ttl_ms must be positive")
        self._database = database
        self._claim_ttl_ms = claim_ttl_ms
        self._internalize_root_execution = internalize_root_execution
        if not contracts_root.is_absolute():
            raise ValueError("contracts_root must be an absolute path")
        self._validator = _ProjectionValidator(contracts_root)

    @staticmethod
    def _require_event_authority(event: GameEvent, context: OperationContext) -> None:
        if event.student_id != context.actor.actor_id or event.command_id != context.command_id:
            raise RepositoryAuthorityError("Agent event is not owned by the operation context")

    def _decode_record(
        self,
        row: dict[str, object],
        context: OperationContext,
    ) -> CommittedAgentTurn:
        record = decode_as(row["record_json"], CommittedAgentTurn)
        actor = record.actor
        current = context.actor
        if (
            actor.tenant_id,
            actor.actor_id,
            actor.actor_type,
        ) != (
            current.tenant_id,
            current.actor_id,
            current.actor_type,
        ) or record.content_ref != context.content_ref:
            raise RepositoryAuthorityError("committed Agent turn authority mismatch")
        self._validate_final_role_decision(record.event, record.decision)
        return record

    async def get_committed(
        self,
        event: GameEvent,
        context: OperationContext,
    ) -> CommittedAgentTurn | None:
        self._require_event_authority(event, context)
        row = await _fetch_one(
            self._database,
            """
            SELECT actor_id, content_hash, event_sha256, record_json
            FROM yaya_agent_turns WHERE tenant_id=%s AND event_id=%s
            """,
            (context.actor.tenant_id, event.event_id),
        )
        if row is None:
            return None
        if (
            row["actor_id"] != context.actor.actor_id
            or row["content_hash"] != context.content_ref.content_hash
            or row["event_sha256"] != _event_sha256(event)
        ):
            raise RepositoryAuthorityError("Agent event identity was reused across authority")
        if row["record_json"] is None:
            return None
        return self._decode_record(row, context)

    async def claim(
        self,
        event: GameEvent,
        context: OperationContext,
    ) -> AgentTurnClaimReceipt:
        self._require_event_authority(event, context)
        event_sha256 = _event_sha256(event)
        claim_id = f"claim_{uuid.uuid4().hex}"
        async with self._database.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO yaya_agent_turns(
                    tenant_id,event_id,actor_id,content_hash,event_sha256,
                    claim_id,claim_expires_at
                ) VALUES (%s,%s,%s,%s,%s,%s,
                    clock_timestamp() + %s * interval '1 millisecond')
                ON CONFLICT (tenant_id,event_id) DO NOTHING
                """,
                (
                    context.actor.tenant_id,
                    event.event_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    event_sha256,
                    claim_id,
                    self._claim_ttl_ms,
                ),
            )
            cursor = await connection.execute(
                """
                SELECT actor_id,content_hash,event_sha256,claim_id,claim_expires_at,
                       record_json, clock_timestamp() AS database_now
                FROM yaya_agent_turns WHERE tenant_id=%s AND event_id=%s FOR UPDATE
                """,
                (context.actor.tenant_id, event.event_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("Agent turn claim disappeared inside its transaction")
            self._validate_turn_row(row, event_sha256, context)
            if row["record_json"] is not None:
                return AgentTurnClaimReceipt(None, None, self._decode_record(row, context))
            if row["claim_id"] == claim_id:
                return AgentTurnClaimReceipt(
                    claim_id,
                    cast(datetime, row["claim_expires_at"]),
                    None,
                )
            expires_at = cast(datetime | None, row["claim_expires_at"])
            database_now = cast(datetime, row["database_now"])
            if expires_at is not None and expires_at > database_now:
                raise AgentTurnLeaseConflict("Agent turn already has a live worker claim")
            result = await connection.execute(
                """
                UPDATE yaya_agent_turns
                SET claim_id=%s,
                    claim_expires_at=clock_timestamp() + %s * interval '1 millisecond'
                WHERE tenant_id=%s AND event_id=%s AND record_json IS NULL
                  AND claim_id IS NOT DISTINCT FROM %s
                RETURNING claim_expires_at
                """,
                (
                    claim_id,
                    self._claim_ttl_ms,
                    context.actor.tenant_id,
                    event.event_id,
                    row["claim_id"],
                ),
            )
            updated = await result.fetchone()
            if updated is None:
                raise AgentTurnFenceError("expired Agent turn claim takeover lost its CAS")
            return AgentTurnClaimReceipt(
                claim_id,
                cast(datetime, updated["claim_expires_at"]),
                None,
            )

    @staticmethod
    def _validate_turn_row(
        row: dict[str, object],
        event_sha256: str,
        context: OperationContext,
    ) -> None:
        if (
            row["actor_id"] != context.actor.actor_id
            or row["content_hash"] != context.content_ref.content_hash
            or row["event_sha256"] != event_sha256
        ):
            raise RepositoryAuthorityError("Agent event identity conflicts with durable authority")

    async def abandon(
        self,
        event: GameEvent,
        claim_id: str,
        context: OperationContext,
    ) -> None:
        self._require_event_authority(event, context)
        async with self._database.transaction() as connection:
            result = await connection.execute(
                """
                UPDATE yaya_agent_turns SET claim_id=NULL, claim_expires_at=NULL
                WHERE tenant_id=%s AND event_id=%s AND actor_id=%s AND content_hash=%s
                  AND event_sha256=%s AND record_json IS NULL AND claim_id=%s
                  AND claim_expires_at > clock_timestamp()
                """,
                (
                    context.actor.tenant_id,
                    event.event_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    _event_sha256(event),
                    claim_id,
                ),
            )
            if result.rowcount != 1:
                raise AgentTurnFenceError("Agent turn abandon rejected a stale claim")

    async def renew(
        self,
        event: GameEvent,
        claim_id: str,
        minimum_ttl_ms: int,
        context: OperationContext,
    ) -> AgentTurnClaimReceipt:
        if minimum_ttl_ms < 1:
            raise ValueError("minimum_ttl_ms must be positive")
        self._require_event_authority(event, context)
        async with self._database.transaction() as connection:
            result = await connection.execute(
                """
                UPDATE yaya_agent_turns
                SET claim_expires_at=GREATEST(
                    claim_expires_at,
                    clock_timestamp() + %s * interval '1 millisecond'
                )
                WHERE tenant_id=%s AND event_id=%s AND actor_id=%s AND content_hash=%s
                  AND event_sha256=%s AND record_json IS NULL AND claim_id=%s
                  AND claim_expires_at > clock_timestamp()
                RETURNING claim_expires_at
                """,
                (
                    minimum_ttl_ms,
                    context.actor.tenant_id,
                    event.event_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    _event_sha256(event),
                    claim_id,
                ),
            )
            row = await result.fetchone()
            if row is not None:
                return AgentTurnClaimReceipt(
                    claim_id,
                    cast(datetime, row["claim_expires_at"]),
                    None,
                )
            replay = await connection.execute(
                """
                SELECT actor_id,content_hash,event_sha256,record_json
                FROM yaya_agent_turns WHERE tenant_id=%s AND event_id=%s
                """,
                (context.actor.tenant_id, event.event_id),
            )
            replay_row = await replay.fetchone()
            if replay_row is not None:
                self._validate_turn_row(replay_row, _event_sha256(event), context)
                if replay_row["record_json"] is not None:
                    return AgentTurnClaimReceipt(
                        None,
                        None,
                        self._decode_record(replay_row, context),
                    )
            raise AgentTurnFenceError("Agent turn renew rejected a stale or expired claim")

    async def commit(
        self,
        event: GameEvent,
        route: RoleRoute,
        decision: AgentDecision,
        claim_id: str,
        context: OperationContext,
    ) -> AgentTurnCommitReceipt:
        try:
            return await self._commit_once(
                event,
                route,
                decision,
                claim_id,
                context,
            )
        except PostgresCommitStateUnknown as unknown:
            try:
                replay = await self.get_committed(event, context)
            except Exception:
                raise unknown
            if replay is None:
                raise
            return AgentTurnCommitReceipt(replay, False)

    async def _commit_once(
        self,
        event: GameEvent,
        route: RoleRoute,
        decision: AgentDecision,
        claim_id: str,
        context: OperationContext,
    ) -> AgentTurnCommitReceipt:
        self._require_event_authority(event, context)
        if (
            not route.should_run
            or route.event_type != event.event_type
            or route.role != decision.role
        ):
            raise ValueError("Agent turn route and decision are not identity-closed")
        if decision.response_type == "skill_patch" or decision.draft.skill_patch is not None:
            raise ValueError("Skill Patch publication is outside this vertical slice")
        self._validate_final_role_decision(event, decision)
        if (
            decision.draft.learner_inference is not None
            and _RUNTIME_EVENT_ID.fullmatch(event.event_id) is None
        ):
            raise RepositoryAuthorityError(
                "Learner inference source event_id is not a canonical event identifier"
            )
        event_sha256 = _event_sha256(event)
        async with self._database.transaction_with_commit_boundary() as connection:
            cursor = await connection.execute(
                """
                SELECT actor_id,content_hash,event_sha256,claim_id,claim_expires_at,
                       record_json,clock_timestamp() AS database_now
                FROM yaya_agent_turns WHERE tenant_id=%s AND event_id=%s FOR UPDATE
                """,
                (context.actor.tenant_id, event.event_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise AgentTurnFenceError("Agent turn must be claimed before commit")
            self._validate_turn_row(row, event_sha256, context)
            if row["record_json"] is not None:
                return AgentTurnCommitReceipt(self._decode_record(row, context), False)
            if (
                row["claim_id"] != claim_id
                or cast(datetime | None, row["claim_expires_at"]) is None
                or cast(datetime, row["claim_expires_at"]) <= cast(datetime, row["database_now"])
            ):
                raise AgentTurnFenceError("Agent turn commit rejected a stale or expired claim")

            session = await self._lock_and_validate_session(connection, event, context)
            task = await self._lock_and_validate_task(connection, event, context)
            command = await self._lock_and_validate_command(connection, event, context)
            inference = decision.draft.learner_inference
            if inference is not None:
                directive = decision.teaching_directive
                if directive is None:
                    raise RepositoryAuthorityError(
                        "Learner inference has no committed TeachingDirective"
                    )
                if decision.role not in {
                    "teaching_agent",
                    "bug_agent",
                    "book_agent",
                }:
                    raise RepositoryAuthorityError("Agent role cannot persist a LearnerInference")
                if (
                    inference.concept != directive.target_concept
                    or inference.concept not in task.knowledge_points
                    or set(inference.evidence_ids) != set(directive.required_evidence_ids)
                    or directive.teaching_spec_version != command.versions.teaching_spec_version
                ):
                    raise RepositoryAuthorityError(
                        "Learner inference exceeds its Task or TeachingDirective boundary"
                    )
            run_id, run = await self._resolve_run(connection, event, decision, context)
            feedback_causation_id = await self._resolve_feedback_causation(
                connection,
                event=event,
                decision=decision,
                context=context,
                run=run,
            )
            committed_at = await self._database_time(
                connection,
                not_before=decision.completed_at,
            )
            record = CommittedAgentTurn(
                event=event,
                actor=context.actor,
                content_ref=context.content_ref,
                route=route,
                decision=decision,
            )
            if self._is_internal_root_execution(event, decision):
                fenced = await connection.execute(
                    """
                    UPDATE yaya_agent_turns
                    SET claim_id=NULL,claim_expires_at=NULL,record_json=%s,committed_at=%s
                    WHERE tenant_id=%s AND event_id=%s AND claim_id=%s
                      AND record_json IS NULL AND claim_expires_at > clock_timestamp()
                    """,
                    (
                        Jsonb(encode(record)),
                        committed_at,
                        context.actor.tenant_id,
                        event.event_id,
                        claim_id,
                    ),
                )
                if fenced.rowcount != 1:
                    raise AgentTurnFenceError("Agent turn commit lost its fencing token")
                return AgentTurnCommitReceipt(record, True)
            feedback = self._feedback(event, decision, run_id)
            feedback_sha256 = canonical_json_sha256(feedback)
            seed = canonical_json_sha256(
                {
                    "tenant_id": context.actor.tenant_id,
                    "event_id": event.event_id,
                    "event_sha256": event_sha256,
                }
            )
            interaction_id = _identifier("interaction", seed)
            receipt_id = _identifier("projection", seed)
            feedback_event_id = _identifier("evt_feedback", seed)
            message_id = _identifier("message", seed)
            projection_message_id = _identifier("projection_msg", seed)
            learner_projection_message_id = _identifier("learner_projection_msg", seed)
            learner_projection_job_id = _identifier("learner_job", seed)
            learner_inference_event_id = _identifier("evt_inference", seed)
            interaction_sequence = await self._next_interaction_sequence(
                connection, event, context.actor.tenant_id
            )
            event_sequence = await self._next_event_sequence(
                connection, event, context.actor.tenant_id
            )
            feedback_event = RuntimeEvent(
                event_id=feedback_event_id,
                event_type=RuntimeEventType.AGENT_TURN_FEEDBACK_READY,
                event_version=1,
                stream_id=f"agent-session:{event.session_id}",
                sequence=event_sequence,
                occurred_at=decision.completed_at,
                producer="agent_hub",
                trace_id=context.trace_id,
                command_id=event.command_id,
                correlation_id=context.correlation_id,
                causation_id=feedback_causation_id,
                content_ref=context.content_ref,
                payload=feedback,
            )
            inference_event: RuntimeEvent | None = None
            projection_context: OperationContext | None = None
            if inference is not None:
                directive = decision.teaching_directive
                if directive is None:
                    raise RepositoryAuthorityError(
                        "Learner inference has no committed TeachingDirective"
                    )
                inference_ids = set(inference.evidence_ids)
                inference_evidence = sorted(
                    (
                        item
                        for item in _evidence_wire(decision)
                        if item["evidence_id"] in inference_ids
                    ),
                    key=lambda item: cast(str, item["evidence_id"]),
                )
                if {
                    cast(str, item["evidence_id"]) for item in inference_evidence
                } != inference_ids or any("sha256" not in item for item in inference_evidence):
                    raise RepositoryAuthorityError(
                        "Learner inference Evidence is missing a durable content hash"
                    )
                learner_sequence = await self._next_learner_event_sequence(
                    connection,
                    learner_id=context.actor.actor_id,
                    tenant_id=context.actor.tenant_id,
                )
                inference_payload: dict[str, object] = {
                    "actor": plain(context.actor),
                    "learner_id": context.actor.actor_id,
                    "session_id": event.session_id,
                    "turn_id": event.turn_id,
                    "command_id": event.command_id,
                    "run_id": run_id,
                    "source_event_id": event.event_id,
                    "source_event_sha256": event_sha256,
                    "turn_commit_sha256": agent_turn_commit_sha256(record),
                    "task_id": event.task_id,
                    "teaching_spec_version": directive.teaching_spec_version,
                    "role": decision.role,
                    "concept": inference.concept,
                    "score_delta": inference.score_delta,
                    "confidence": inference.confidence,
                    "reason": inference.reason,
                    "evidence_refs": inference_evidence,
                    "inferred_at": plain(decision.completed_at),
                }
                inference_payload["inference_sha256"] = learner_inference_sha256(inference_payload)
                inference_event = RuntimeEvent(
                    event_id=learner_inference_event_id,
                    event_type=RuntimeEventType.LEARNER_INFERENCE_RECORDED,
                    event_version=1,
                    schema_version="2.0.0",
                    stream_id=f"learner:{context.actor.actor_id}",
                    sequence=learner_sequence,
                    occurred_at=decision.completed_at,
                    producer="agent_hub",
                    trace_id=context.trace_id,
                    command_id=event.command_id,
                    correlation_id=context.correlation_id,
                    causation_id=event.event_id,
                    content_ref=context.content_ref,
                    payload=inference_payload,
                )
                inference_envelope = _mapping(plain(inference_event), "learner inference event")
                self._validator.validate(
                    "schemas/learner/learner-inference-recorded-event.schema.json",
                    inference_envelope,
                )
                projection_context = OperationContext(
                    request_id=context.request_id,
                    correlation_id=context.correlation_id,
                    trace_id=context.trace_id,
                    requested_at=context.requested_at,
                    actor=context.actor,
                    content_ref=context.content_ref,
                    schema_version=context.schema_version,
                    command_id=context.command_id,
                    causation_id=event.event_id,
                    deadline_at=None,
                )
            source = {
                "receipt_id": receipt_id,
                "source_type": "AGENT_TURN_PRODUCT_PROJECTION",
                "source_revision": 1,
                "actor": plain(context.actor),
                "content_ref": plain(context.content_ref),
                "interaction_id": interaction_id,
                "session_id": event.session_id,
                "turn_id": event.turn_id,
                "sequence": interaction_sequence,
                "command_id": event.command_id,
                "feedback_event_id": feedback_event_id,
                "feedback_sha256": feedback_sha256,
                "role": decision.role,
                "response_type": decision.response_type,
                "question": decision.draft.question,
                "hint_level": decision.draft.hint_level,
                "skill_patch_sha256": None,
                "committed_at": plain(committed_at),
            }
            source["source_sha256"] = canonical_json_sha256(source)
            feedback_envelope = _mapping(plain(feedback_event), "feedback event")
            feedback_summary = dict(feedback_envelope)
            feedback_summary.pop("payload")
            feedback_summary["feedback_sha256"] = feedback_sha256
            request_context = _request_context(context)
            interaction: dict[str, object] = {
                "request_context": plain(request_context),
                "interaction_id": interaction_id,
                "session_id": event.session_id,
                "turn_id": event.turn_id,
                "sequence": interaction_sequence,
                "interaction_revision": 1,
                "projection_source": source,
                "role": decision.role,
                "response_type": decision.response_type,
                "question": decision.draft.question,
                "hint_level": decision.draft.hint_level,
                "feedback": feedback,
                "feedback_event": feedback_summary,
                "skill_patch": None,
                "patch_decision": None,
                "created_at": plain(committed_at),
                "updated_at": plain(committed_at),
                "links": {
                    "self": (
                        f"/product-experience/v1/sessions/{event.session_id}/"
                        f"agent-interactions/{interaction_id}"
                    ),
                    "session_workspace": (
                        f"/product-experience/v1/sessions/{event.session_id}/workspace"
                    ),
                    "skill_draft": None,
                },
            }
            self._validator.validate(
                "schemas/game/agent-turn-feedback-ready-event.schema.json", feedback_envelope
            )
            self._validator.validate(
                "schemas/product-experience/agent-interaction.schema.json", interaction
            )
            await self._publish(
                connection,
                record=record,
                interaction=interaction,
                feedback_event=feedback_event,
                feedback=feedback,
                run_id=run_id,
                message_id=message_id,
                projection_message_id=projection_message_id,
                seed=seed,
                committed_at=committed_at,
                session=session,
                context=context,
                inference_event=inference_event,
                projection_context=projection_context,
                learner_projection_job_id=learner_projection_job_id,
                learner_projection_message_id=learner_projection_message_id,
            )
            fenced = await connection.execute(
                """
                UPDATE yaya_agent_turns
                SET claim_id=NULL,claim_expires_at=NULL,record_json=%s,committed_at=%s
                WHERE tenant_id=%s AND event_id=%s AND claim_id=%s
                  AND record_json IS NULL AND claim_expires_at > clock_timestamp()
                """,
                (
                    Jsonb(encode(record)),
                    committed_at,
                    context.actor.tenant_id,
                    event.event_id,
                    claim_id,
                ),
            )
            if fenced.rowcount != 1:
                raise AgentTurnFenceError("Agent turn commit lost its fencing token")
            return AgentTurnCommitReceipt(record, True)

    def _is_internal_root_execution(
        self,
        event: GameEvent,
        decision: AgentDecision,
    ) -> bool:
        return (
            self._internalize_root_execution
            and event.event_type == "run_skill_requested"
            and decision.role == "xiaohutao"
        )

    @staticmethod
    def _validate_final_role_decision(event: GameEvent, decision: AgentDecision) -> None:
        if decision.role not in {"bug_agent", "book_agent"}:
            return
        directive = decision.teaching_directive
        if (
            decision.source != "provider"
            or decision.degraded
            or decision.fallback_reason is not None
            or directive is None
            or directive.patch_eligible
            or directive.full_solution_eligible
            or decision.draft.skill_patch is not None
            or decision.draft.requires_student_confirmation
        ):
            raise AgentPersistenceError(
                "AGENT_FINAL_ROLE_INVARIANT_VIOLATION",
                "Final Bug/Book publication is not a non-degraded provider decision",
            )
        if decision.role == "bug_agent":
            valid = (
                event.event_type in {"run_failed", "hint_requested"}
                and event.failure_count >= 3
                and decision.response_type == "question"
                and directive.phase is TeachingPhase.RECTIFICATION
                and directive.allowed_response_types == ("question",)
            )
        else:
            valid = (
                event.event_type == "task_completed"
                and decision.response_type == "growth_summary"
                and directive.phase is TeachingPhase.SUMMARIZATION
                and directive.allowed_response_types == ("growth_summary",)
            )
        if not valid:
            raise AgentPersistenceError(
                "AGENT_FINAL_ROLE_INVARIANT_VIOLATION",
                "Final Bug/Book publication violates its frozen teaching phase",
            )

    async def _lock_and_validate_session(
        self,
        connection: _Connection,
        event: GameEvent,
        context: OperationContext,
    ) -> SessionSnapshot:
        cursor = await connection.execute(
            """
            SELECT snapshot_json FROM yaya_agent_sessions
            WHERE tenant_id=%s AND session_id=%s AND actor_id=%s AND content_hash=%s
            FOR UPDATE
            """,
            (
                context.actor.tenant_id,
                event.session_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
            ),
        )
        row = await cursor.fetchone()
        session = _scoped_snapshot(row, SessionSnapshot, context)
        if (
            session.student_id != event.student_id
            or session.task_id != event.task_id
            or session.session_id != event.session_id
        ):
            raise RepositoryAuthorityError("Agent event does not match its locked Session")
        return session

    @staticmethod
    async def _lock_and_validate_task(
        connection: _Connection,
        event: GameEvent,
        context: OperationContext,
    ) -> TaskSnapshot:
        cursor = await connection.execute(
            """
            SELECT snapshot_json FROM yaya_tasks
            WHERE tenant_id=%s AND task_id=%s AND actor_id=%s AND content_hash=%s
            FOR KEY SHARE
            """,
            (
                context.actor.tenant_id,
                event.task_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
            ),
        )
        row = await cursor.fetchone()
        task = _scoped_snapshot(row, TaskSnapshot, context)
        if task.task_id != event.task_id:
            raise RepositoryAuthorityError("Agent event does not match its locked Task")
        return task

    @staticmethod
    async def _lock_and_validate_command(
        connection: _Connection,
        event: GameEvent,
        context: OperationContext,
    ) -> CommandRecord:
        cursor = await connection.execute(
            """
            SELECT revision,status,record_json FROM yaya_commands
            WHERE tenant_id=%s AND command_id=%s AND actor_id=%s AND content_hash=%s
              AND session_id=%s AND turn_id=%s FOR KEY SHARE
            """,
            (
                context.actor.tenant_id,
                event.command_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
                event.session_id,
                event.turn_id,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RepositoryAuthorityError("Agent turn has no closed Command identity")
        record = decode_as(row["record_json"], CommandRecord)
        _require_authority(record.request_context, context)
        if (
            record.command_id != event.command_id
            or record.command_type != "EXECUTE_AGENT_TURN"
            or row["revision"] != record.revision
            or row["status"] != record.status.value
            or record.status
            not in {
                CommandStatus.ACCEPTED,
                CommandStatus.VALIDATING,
                CommandStatus.RUNNING_SANDBOX,
                CommandStatus.APPLYING_WORLD,
            }
        ):
            raise RepositoryAuthorityError("Command record identity drifted")
        return record

    @staticmethod
    async def _resolve_run(
        connection: _Connection,
        event: GameEvent,
        decision: AgentDecision,
        context: OperationContext,
    ) -> tuple[str | None, RunResultSnapshot | None]:
        cursor = await connection.execute(
            """
            SELECT run_id,command_id,snapshot_json FROM yaya_runs
            WHERE tenant_id=%s AND session_id=%s AND turn_id=%s
              AND actor_id=%s AND content_hash=%s
            FOR KEY SHARE
            """,
            (
                context.actor.tenant_id,
                event.session_id,
                event.turn_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            dispatched_skill = any(call.name == "invoke_skill" for call in decision.tool_calls)
            if event.run_id is not None or dispatched_skill:
                raise RepositoryNotFoundError("Agent turn Run is not durably available")
            if decision.evidence_refs:
                if event.event_type != "compile_failed" or event.build_id is None:
                    raise RepositoryAuthorityError(
                        "Run-free Agent fallback cannot cite unbound Evidence"
                    )
                compile_cursor = await connection.execute(
                    """
                    SELECT actor_id,content_hash,snapshot_json
                    FROM yaya_compile_results
                    WHERE tenant_id=%s AND build_id=%s
                      AND actor_id=%s AND content_hash=%s
                    FOR KEY SHARE
                    """,
                    (
                        context.actor.tenant_id,
                        event.build_id,
                        context.actor.actor_id,
                        context.content_ref.content_hash,
                    ),
                )
                compile_row = await compile_cursor.fetchone()
                if compile_row is None:
                    raise RepositoryNotFoundError(
                        "Agent turn Compile result is not durably available"
                    )
                compile_result = decode_as(compile_row["snapshot_json"], CompileResultSnapshot)
                _require_authority(compile_result.request_context, context)
                if (
                    compile_result.build_id != event.build_id
                    or compile_result.skill_ref != event.skill_ref
                    or compile_result.succeeded
                    or compile_result.evidence_refs != event.evidence_refs
                    or compile_result.evidence_refs != decision.evidence_refs
                ):
                    raise RepositoryAuthorityError(
                        "Agent turn Compile Evidence is not the exact failed build result"
                    )
            return None, None
        invoked_skill = any(call.name == "invoke_skill" for call in decision.tool_calls)
        if decision.role == "xiaohutao" and not invoked_skill:
            raise RepositoryAuthorityError(
                "xiaohutao decision did not execute the durable Run found for this turn"
            )
        if decision.role != "xiaohutao" and invoked_skill:
            raise RepositoryAuthorityError(
                "directive-bearing role cannot execute the durable Run it is evaluating"
            )
        run_id = cast(str, row["run_id"])
        if row["command_id"] != event.command_id or (
            event.run_id is not None and event.run_id != run_id
        ):
            raise RepositoryAuthorityError("Run identity does not match the Agent event")
        run = decode_as(row["snapshot_json"], RunResultSnapshot)
        _require_authority(run.request_context, context)
        if (
            run.run_id,
            run.session_id,
            run.turn_id,
            run.command_id,
        ) != (
            run_id,
            event.session_id,
            event.turn_id,
            event.command_id,
        ):
            raise RepositoryAuthorityError("Run snapshot identity is not closed")
        if set(run.evidence_refs) != set(decision.evidence_refs):
            raise RepositoryAuthorityError("Agent decision Evidence differs from the Run")
        if decision.role == "book_agent" and not run.task_success:
            raise AgentPersistenceError(
                "AGENT_FINAL_ROLE_INVARIANT_VIOLATION",
                "book_agent cannot publish an unsuccessful Run",
            )
        if decision.role == "bug_agent" and (
            run.task_success or run.failure_key is None or run.failure_key != event.failure_key
        ):
            raise AgentPersistenceError(
                "AGENT_FINAL_ROLE_INVARIANT_VIOLATION",
                "bug_agent cannot publish outside its canonical failed Run",
            )
        return run_id, run

    @staticmethod
    async def _resolve_feedback_causation(
        connection: _Connection,
        *,
        event: GameEvent,
        decision: AgentDecision,
        context: OperationContext,
        run: RunResultSnapshot | None,
    ) -> str:
        fallback = context.causation_id or event.command_id
        if run is None or run.world_commit is None:
            return fallback

        receipt = run.world_commit
        cursor = await connection.execute(
            """
            SELECT event_id,stream_id,sequence,event_type,event_json,occurred_at
            FROM yaya_events
            WHERE tenant_id=%s AND stream_id=%s
              AND sequence BETWEEN %s AND %s
            ORDER BY sequence,event_id
            FOR KEY SHARE
            """,
            (
                context.actor.tenant_id,
                f"world:{run.world_id}",
                receipt.first_event_sequence,
                receipt.last_event_sequence,
            ),
        )
        matches: list[RuntimeEvent] = []
        for row in await cursor.fetchall():
            try:
                candidate = _plain_runtime_event(
                    row["event_json"],
                    "Run World receipt event",
                )
            except (TypeError, ValueError) as error:
                raise RepositoryAuthorityError(
                    "Run World receipt range contains an invalid canonical event"
                ) from error
            payload = candidate.payload
            if (
                row["event_id"] == candidate.event_id
                and row["stream_id"] == candidate.stream_id
                and row["sequence"] == candidate.sequence
                and row["event_type"] == candidate.event_type
                and isinstance(row["occurred_at"], datetime)
                and row["occurred_at"] == candidate.occurred_at
                and candidate.event_type is RuntimeEventType.WORLD_COMMITTED
                and candidate.command_id == event.command_id
                and candidate.causation_id == event.command_id
                and candidate.stream_id == f"world:{run.world_id}"
                and receipt.first_event_sequence
                <= candidate.sequence
                <= receipt.last_event_sequence
                and candidate.trace_id == context.trace_id
                and candidate.correlation_id == context.correlation_id
                and candidate.content_ref == context.content_ref
                and candidate.occurred_at <= decision.completed_at
                and payload.get("run_id") == run.run_id
                and payload.get("world_id") == run.world_id
                and payload.get("previous_world_revision") == receipt.previous_revision
                and payload.get("world_revision") == receipt.world_revision
                and payload.get("state_hash") == receipt.state_hash
                and payload.get("committed_at") == plain(candidate.occurred_at)
            ):
                matches.append(candidate)
        if len(matches) != 1:
            raise RepositoryAuthorityError(
                "Committed Run does not resolve exactly one canonical World event"
            )
        return matches[0].event_id

    @staticmethod
    async def _database_time(
        connection: _Connection,
        *,
        not_before: datetime,
    ) -> datetime:
        cursor = await connection.execute(
            "SELECT GREATEST(clock_timestamp(), %s::timestamptz) AS value",
            (not_before,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("PostgreSQL clock query returned no row")
        return cast(datetime, row["value"])

    @staticmethod
    async def _next_interaction_sequence(
        connection: _Connection,
        event: GameEvent,
        tenant_id: str,
    ) -> int:
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"{tenant_id}:product-agent-interactions:{event.session_id}",),
        )
        cursor = await connection.execute(
            """
            SELECT COALESCE(MAX(sequence),0)+1 AS value FROM yaya_agent_interactions
            WHERE tenant_id=%s AND session_id=%s
            """,
            (tenant_id, event.session_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("interaction sequence query returned no row")
        return cast(int, row["value"])

    @staticmethod
    async def _next_event_sequence(
        connection: _Connection,
        event: GameEvent,
        tenant_id: str,
    ) -> int:
        stream_id = f"agent-session:{event.session_id}"
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"{tenant_id}:{stream_id}",),
        )
        cursor = await connection.execute(
            """
            SELECT COALESCE(MAX(sequence),0)+1 AS value FROM yaya_events
            WHERE tenant_id=%s AND stream_id=%s
            """,
            (tenant_id, stream_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("event sequence query returned no row")
        return cast(int, row["value"])

    @staticmethod
    async def _next_learner_event_sequence(
        connection: _Connection,
        *,
        learner_id: str,
        tenant_id: str,
    ) -> int:
        stream_id = f"learner:{learner_id}"
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"{tenant_id}:{stream_id}",),
        )
        cursor = await connection.execute(
            """
            SELECT COALESCE(MAX(sequence),0)+1 AS value FROM yaya_events
            WHERE tenant_id=%s AND stream_id=%s
            """,
            (tenant_id, stream_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("learner event sequence query returned no row")
        return cast(int, row["value"])

    @staticmethod
    def _feedback(
        event: GameEvent,
        decision: AgentDecision,
        run_id: str | None,
    ) -> dict[str, object]:
        return {
            "session_id": event.session_id,
            "turn_id": event.turn_id,
            "command_id": event.command_id,
            "run_id": run_id,
            "message_key": decision.message_key,
            "message": decision.message,
            "source": decision.source,
            "degraded": decision.degraded,
            "fallback_reason": decision.fallback_reason,
            "evidence_refs": _evidence_wire(decision),
            "completed_at": plain(decision.completed_at),
        }

    @staticmethod
    async def _publish_learner_inference(
        connection: _Connection,
        *,
        record: CommittedAgentTurn,
        inference_event: RuntimeEvent,
        projection_context: OperationContext,
        job_id: str,
        projection_message_id: str,
    ) -> None:
        payload = inference_event.payload
        event_record = _mapping(encode(inference_event), "learner inference record")
        event_sha256 = internal_record_sha256(inference_event)
        turn_sha256 = agent_turn_commit_sha256(record)
        source_event_sha256 = _event_sha256(record.event)
        if (
            inference_event.event_type is not RuntimeEventType.LEARNER_INFERENCE_RECORDED
            or inference_event.causation_id != record.event.event_id
            or projection_context.causation_id != record.event.event_id
            or projection_context.command_id != record.event.command_id
            or payload["source_event_id"] != record.event.event_id
            or payload["source_event_sha256"] != source_event_sha256
            or payload["turn_commit_sha256"] != turn_sha256
            or payload["learner_id"] != projection_context.actor.actor_id
            or plain(payload["actor"]) != plain(projection_context.actor)
            or payload["inference_sha256"] != learner_inference_sha256(payload)
        ):
            raise RepositoryAuthorityError(
                "Learner inference publication identity or hash is not closed"
            )
        await connection.execute(
            """
            INSERT INTO yaya_events(
                tenant_id,event_id,stream_id,sequence,event_type,event_json,occurred_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                projection_context.actor.tenant_id,
                inference_event.event_id,
                inference_event.stream_id,
                inference_event.sequence,
                inference_event.event_type,
                Jsonb(event_record),
                inference_event.occurred_at,
            ),
        )
        await connection.execute(
            """
            INSERT INTO yaya_learner_projection_jobs(
                tenant_id,job_id,event_id,source_event_id,learner_id,actor_id,
                content_hash,task_id,session_id,turn_id,command_id,run_id,
                source_stream_id,source_stream_sequence,event_sha256,
                source_event_sha256,turn_commit_sha256,inference_sha256,
                teaching_spec_version,role,event_json,operation_context_json
            ) VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s
            )
            """,
            (
                projection_context.actor.tenant_id,
                job_id,
                inference_event.event_id,
                record.event.event_id,
                payload["learner_id"],
                projection_context.actor.actor_id,
                projection_context.content_ref.content_hash,
                payload["task_id"],
                payload["session_id"],
                payload["turn_id"],
                payload["command_id"],
                payload["run_id"],
                inference_event.stream_id,
                inference_event.sequence,
                event_sha256,
                source_event_sha256,
                turn_sha256,
                payload["inference_sha256"],
                payload["teaching_spec_version"],
                payload["role"],
                Jsonb(event_record),
                Jsonb(encode(projection_context)),
            ),
        )
        raw_evidence = payload["evidence_refs"]
        if isinstance(raw_evidence, (str, bytes, bytearray)) or not isinstance(
            raw_evidence, Sequence
        ):
            raise RepositoryAuthorityError(
                "Learner inference Evidence must be an immutable sequence"
            )
        for ordinal, raw_reference in enumerate(cast(Sequence[object], raw_evidence)):
            reference = _mapping(raw_reference, "learner inference EvidenceRef")
            evidence_id = reference.get("evidence_id")
            evidence_sha256 = reference.get("sha256")
            if not isinstance(evidence_id, str) or not isinstance(evidence_sha256, str):
                raise RepositoryAuthorityError(
                    "Learner inference Evidence lacks an immutable identity hash"
                )
            await connection.execute(
                """
                INSERT INTO yaya_learner_projection_job_evidence(
                    tenant_id,job_id,event_id,source_event_id,learner_id,actor_id,
                    content_hash,source_stream_id,source_stream_sequence,ordinal,
                    evidence_id,evidence_sha256
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    projection_context.actor.tenant_id,
                    job_id,
                    inference_event.event_id,
                    record.event.event_id,
                    payload["learner_id"],
                    projection_context.actor.actor_id,
                    projection_context.content_ref.content_hash,
                    inference_event.stream_id,
                    inference_event.sequence,
                    ordinal,
                    evidence_id,
                    evidence_sha256,
                ),
            )
        inference_wire = _mapping(plain(inference_event), "learner inference outbox payload")
        await connection.execute(
            """
            INSERT INTO yaya_projection_outbox(
                tenant_id,message_id,destination,idempotency_key,
                payload_sha256,payload_json
            ) VALUES (%s,%s,'learner_projection_events',%s,%s,%s)
            """,
            (
                projection_context.actor.tenant_id,
                projection_message_id,
                f"learner-inference:{inference_event.event_id}",
                internal_record_sha256(inference_wire),
                Jsonb(inference_wire),
            ),
        )

    @staticmethod
    async def _publish(
        connection: _Connection,
        *,
        record: CommittedAgentTurn,
        interaction: Mapping[str, object],
        feedback_event: RuntimeEvent,
        feedback: Mapping[str, object],
        run_id: str | None,
        message_id: str,
        projection_message_id: str,
        seed: str,
        committed_at: datetime,
        session: SessionSnapshot,
        context: OperationContext,
        inference_event: RuntimeEvent | None,
        projection_context: OperationContext | None,
        learner_projection_job_id: str,
        learner_projection_message_id: str,
    ) -> None:
        event = record.event
        await connection.execute(
            """
            INSERT INTO yaya_events(
                tenant_id,event_id,stream_id,sequence,event_type,event_json,occurred_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                context.actor.tenant_id,
                feedback_event.event_id,
                feedback_event.stream_id,
                feedback_event.sequence,
                feedback_event.event_type,
                Jsonb(encode(feedback_event)),
                feedback_event.occurred_at,
            ),
        )
        await connection.execute(
            """
            INSERT INTO yaya_agent_interactions(
                tenant_id,interaction_id,actor_id,content_hash,
                session_id,turn_id,command_id,
                run_id,sequence,projection_json,created_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                context.actor.tenant_id,
                interaction["interaction_id"],
                context.actor.actor_id,
                context.content_ref.content_hash,
                event.session_id,
                event.turn_id,
                event.command_id,
                run_id,
                interaction["sequence"],
                Jsonb(interaction),
                committed_at,
            ),
        )
        if inference_event is not None:
            if projection_context is None:
                raise AssertionError(
                    "Learner inference publication requires its derived OperationContext"
                )
            await PostgresAgentTurnRepository._publish_learner_inference(
                connection,
                record=record,
                inference_event=inference_event,
                projection_context=projection_context,
                job_id=learner_projection_job_id,
                projection_message_id=learner_projection_message_id,
            )
        elif projection_context is not None:
            raise AssertionError(
                "projection OperationContext cannot exist without learner inference"
            )
        feedback_event_wire = _mapping(plain(feedback_event), "feedback event outbox payload")
        feedback_event_message_id = _identifier("feedback_msg", seed)
        feedback_event_sha256 = canonical_json_sha256(feedback_event_wire)
        await connection.execute(
            """
            INSERT INTO yaya_projection_outbox(
                tenant_id,message_id,destination,idempotency_key,payload_sha256,payload_json
            ) VALUES (%s,%s,'agent_feedback_events',%s,%s,%s)
            """,
            (
                context.actor.tenant_id,
                feedback_event_message_id,
                f"agent-feedback-event:{seed}",
                feedback_event_sha256,
                Jsonb(feedback_event_wire),
            ),
        )
        payload_sha256 = canonical_json_sha256(interaction)
        await connection.execute(
            """
            INSERT INTO yaya_projection_outbox(
                tenant_id,message_id,destination,idempotency_key,payload_sha256,payload_json
            ) VALUES (%s,%s,'product_agent_interactions',%s,%s,%s)
            """,
            (
                context.actor.tenant_id,
                projection_message_id,
                f"agent-turn-product:{seed}",
                payload_sha256,
                Jsonb(interaction),
            ),
        )
        message = MessageSnapshot(
            message_id=message_id,
            session_id=event.session_id,
            role=record.decision.role,
            message=record.decision.message,
            request_context=session.request_context,
        )
        await connection.execute(
            """
            INSERT INTO yaya_agent_messages(
                tenant_id,message_id,actor_id,content_hash,session_id,snapshot_json,created_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                context.actor.tenant_id,
                message_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
                event.session_id,
                Jsonb(encode(message)),
                committed_at,
            ),
        )
        if run_id is not None:
            updated = await connection.execute(
                """
                UPDATE yaya_runs
                SET wire_json=jsonb_set(wire_json,'{agent_feedback}',%s::jsonb,true)
                WHERE tenant_id=%s AND run_id=%s AND actor_id=%s AND content_hash=%s
                  AND session_id=%s AND turn_id=%s AND command_id=%s
                RETURNING wire_json
                """,
                (
                    Jsonb(feedback),
                    context.actor.tenant_id,
                    run_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    event.session_id,
                    event.turn_id,
                    event.command_id,
                ),
            )
            updated_row = await updated.fetchone()
            if updated_row is None:
                raise RepositoryAuthorityError("Run feedback update lost its closed identity")
            wire_json = _mapping(updated_row["wire_json"], "Run wire_json")
            stored_feedback = wire_json.get("agent_feedback")
            if stored_feedback != feedback:
                raise RepositoryAuthorityError("Run feedback update did not persist exact feedback")


__all__ = [
    "AgentTurnFenceError",
    "AgentTurnLeaseConflict",
    "PostgresAgentTraceRepository",
    "PostgresAgentTurnRepository",
    "PostgresCounterexampleRepository",
    "PostgresLearnerRepository",
    "PostgresMessageRepository",
    "PostgresRunRepository",
    "PostgresSessionRepository",
    "PostgresSkillRepository",
    "PostgresTaskRepository",
    "PostgresWorldRepository",
    "RepositoryAuthorityError",
    "RepositoryNotFoundError",
]
